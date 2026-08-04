#!/usr/bin/env python3
"""Drive a genuine max-block-size (xfield) change: the currently-active federation
(signer-set-b, after scripts/simulate_federation_change.py's rotation) signs off on a
new max-block-size via tapyrus-setup's --xfield sign/computesig flow, federations.toml
gains a new entry for it, and the script waits for the scheduled height to confirm the
new value took effect via getblockchaininfo.

Encodes doc/weekly-integration-test-plan.md section 3 step 10 / section 4a's
"Max block size change" note. Same shape as simulate_federation_change.py, but
simpler: no new signer identities and no membership change -- signer-set-b's existing
3 members (node-0, signer-b-1, signer-b-2) sign off on and remain full members of this
new entry too, so only their already-running containers' federations.toml needs a new
entry, live-reloaded in place exactly like the rotation.

Two things worth knowing:

1. **--xfield encoding for MaxBlockSize**: verified against rust-tapyrus v0.4.8's
   src/blockdata/block.rs Encodable/Decodable impls and its own
   xfield_max_block_size_test (`"0200010000"` decodes to `XField::MaxBlockSize(0x100)`).
   Unlike AggregatePublicKey's CompactSize-prefixed variable-length payload,
   MaxBlockSize(u32) has a fixed-size payload -- a 1-byte type tag (0x02) followed by
   the raw 4-byte little-endian u32, no length prefix at all. See
   _maxblocksize_xfield_hex below.

2. **Valid range, confirmed from tapyrus-core source**
   (src/primitives/xfield.h's XFieldMaxBlockSize::IsValid): `data > 1000`. A block
   carrying an out-of-range value is invalid at the consensus level, not just
   rejected by this script.

Usage:
    ./scripts/simulate_maxblocksize_change.py

Requires signer-set-b already active (scripts/simulate_federation_change.py's
rotation already confirmed via RPC). Reads CORE_RPC_USER / CORE_RPC_PASS /
ROUND_DURATION / MAX_BLOCK_SIZE_HEIGHT / MAX_BLOCK_SIZE_NEW from the environment --
the same job-level env vars the workflow already sets for the other steps.

Why not tapyrus-core's generatetoaddress RPC (instant block mining): same reason as
simulate_reorg.py -- no single party holds the aggregate private key by design.
"""
import asyncio
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.generate_dev_secrets import default_tapyrus_setup_bin  # noqa: E402
from scripts.lib.ceremony import CeremonyError, extract_vss_for, require_executable  # noqa: E402
from scripts.lib.log import log  # noqa: E402
from scripts.lib.rpc import CoreRpcClient, RpcError, RpcUnreachable  # noqa: E402
from scripts.simulate_federation_change import (  # noqa: E402
    REUSE_NODE_INDEX,
    SIGNER_COUNT,
    SIGNER_SET_B,
    SIGNER_THRESHOLD,
    XFieldSignoffCeremony,
)

RPC_HOST = "127.0.0.1"
CORE_RPC_PORT = "12381"
HEIGHT_POLL_INTERVAL_SECONDS = 5
# Same liveness-timeout approach as simulate_reorg.py/simulate_federation_change.py's
# _wait_for_height -- duplicated rather than shared, matching this repo's existing
# per-script self-containment.
STALL_TIMEOUT_ROUND_MULTIPLIER = 4
MIN_STALL_TIMEOUT_SECONDS = 180

MAX_BLOCK_SIZE_MIN = 1000

# The 3 currently-active signer-set-b members and where their live federations.toml
# lives -- node-0 is signer-set-a's shared identity (runtime/signers), signer-b-1/
# signer-b-2 are signer-set-b's own genuinely new identities (runtime/signers-b),
# matching docker/docker-compose.yml's mounts.
ACTIVE_SIGNER_NODE_DIRS = (
    REPO_ROOT / "runtime" / "signers" / f"node-{REUSE_NODE_INDEX}",
    REPO_ROOT / "runtime" / "signers-b" / "node-1",
    REPO_ROOT / "runtime" / "signers-b" / "node-2",
)

ALL_NODES = (
    ("core-1a", 12381), ("core-1b", 12382), ("core-2a", 12383),
    ("core-2b", 12384), ("core-3a", 12385), ("core-3b", 12386), ("core-7", 12387),
)
ALL_NODE_NAMES = tuple(name for name, _ in ALL_NODES)


def _maxblocksize_xfield_hex(value):
    """Encodes XField::MaxBlockSize(value) as the hex string tapyrus-setup
    sign/computesig --xfield expects -- see this module's docstring point 1.
    """
    if value <= MAX_BLOCK_SIZE_MIN or value > 0xFFFFFFFF:
        raise CeremonyError(f"max-block-size must be > {MAX_BLOCK_SIZE_MIN} and fit in a u32, got: {value}")
    return f"02{value.to_bytes(4, 'little').hex()}"


def _federation_entry_toml(block_height, max_block_size, threshold, node_vss_lines, signature):
    """One [[federation]] entry for a max-block-size change. Always a member entry
    here (unlike simulate_federation_change.py's helper) -- every node this script
    writes to is, and stays, a full member of signer-set-b.
    """
    vss_block = ",\n".join(f'    "{vss}"' for vss in node_vss_lines)
    return (
        "[[federation]]\n"
        f"block-height = {block_height}\n"
        f"threshold = {threshold}\n"
        f"max-block-size = {max_block_size}\n"
        f"node-vss = [\n{vss_block}\n]\n"
        f'signature = "{signature}"\n'
    )


class MaxBlockSizeChangeSimulator:
    def __init__(self, rpc_user, rpc_pass, round_duration, height_offset, new_max_block_size):
        self._round_duration = round_duration
        self._height_offset = height_offset
        self._new_max_block_size = new_max_block_size
        self._clients = {name: CoreRpcClient(RPC_HOST, port, rpc_user, rpc_pass) for name, port in ALL_NODES}
        self._tapyrus_setup_bin = default_tapyrus_setup_bin()
        self._set_dir = REPO_ROOT / "secrets" / SIGNER_SET_B
        self._pubkeys = self._read_pubkeys()
        self._aggpubkey = self._read_aggpubkey()
        self._scheduled_height = None
        self._signature_hex = None

    async def run(self):
        await self._sign_off_change()
        await self._compute_scheduled_height()
        self._write_configs()
        await self._wait_for_change()
        await self._confirm_change_via_rpc()
        log.info(
            f"done. max-block-size {self._new_max_block_size} took effect at height "
            f"{self._scheduled_height} (confirmed via getblockchaininfo on all 7 core nodes), signed off "
            f"by signer-set-b ({self._aggpubkey[:12]}...)."
        )

    def _read_pubkeys(self):
        return [line for line in (self._set_dir / "pubkeys.txt").read_text().splitlines() if line]

    def _read_aggpubkey(self):
        return (self._set_dir / "aggregated-public-key.txt").read_text().strip()

    def _write_atomic(self, path, content):
        """Write-to-temp-then-rename -- see simulate_federation_change.py's module
        docstring point 4 for why federation_watcher.rs needs this, not an in-place
        write.
        """
        tmp_path = path.with_suffix(".tmp")
        tmp_path.write_text(content)
        tmp_path.rename(path)

    async def _sign_off_change(self):
        log.step("signer-set-b signing off on its own max-block-size change (--xfield sign/computesig)")
        xfield_hex = _maxblocksize_xfield_hex(self._new_max_block_size)
        ceremony = XFieldSignoffCeremony(
            self._tapyrus_setup_bin, self._set_dir, SIGNER_THRESHOLD, self._pubkeys, xfield_hex,
        )
        self._signature_hex = await ceremony.run()
        log.info(f"signoff signature: {self._signature_hex[:12]}...")

    async def _compute_scheduled_height(self):
        current_height = await self._clients["core-1a"].call("getblockcount")
        self._scheduled_height = current_height + self._height_offset
        log.info(
            f"scheduling max-block-size={self._new_max_block_size} at height {self._scheduled_height} "
            f"(current {current_height} + {self._height_offset})"
        )

    def _write_configs(self):
        log.step("adding a max-block-size federations.toml entry for signer-set-b's 3 active members")
        b_raw_dir = self._set_dir / "raw"
        # Every one of the 3 active nodes is a full member here, but each needs its
        # OWN filtered node-vss (same set of shares, filtered to a different
        # receiving pubkey) -- not one shared entry copy-pasted three ways.
        for node_dir, pubkey in zip(ACTIVE_SIGNER_NODE_DIRS, self._pubkeys):
            node_vss_lines = [
                extract_vss_for(b_raw_dir / f"nodevss_from_{i}.txt", pubkey)
                for i in range(SIGNER_COUNT)
            ]
            entry = _federation_entry_toml(
                self._scheduled_height, self._new_max_block_size,
                SIGNER_THRESHOLD, node_vss_lines, self._signature_hex,
            )
            federations_path = node_dir / "federations.toml"
            self._write_atomic(federations_path, federations_path.read_text() + "\n" + entry)

    async def _wait_for_change(self):
        log.step(f"waiting for the chain to reach the scheduled height {self._scheduled_height}")
        await self._wait_for_height(ALL_NODE_NAMES, self._scheduled_height)
        log.info(f"height {self._scheduled_height} reached")

    async def _confirm_change_via_rpc(self):
        # getblockchaininfo, not container/signer logs or script-reported state: its
        # maxBlockSizes array is core's own consensus-level history of xfield
        # MaxBlockSize changes -- [{"<decimal-value>": <height>}, ...], confirmed
        # directly from tapyrus-core source (src/rpc/blockchain.cpp's
        # GetXFieldNameForRpc / src/xfieldhistory.cpp's CXFieldHistory::ToUniValue,
        # value via XFieldMaxBlockSize::ToString() = std::to_string(data), a decimal
        # string not hex, unlike aggregatePubkeys). Checked on all 7 core nodes.
        log.step(
            f"confirming maxBlockSizes includes {self._new_max_block_size} at height "
            f"{self._scheduled_height} via getblockchaininfo (all 7 core nodes)"
        )
        expected_key = str(self._new_max_block_size)
        failures = []
        for name in ALL_NODE_NAMES:
            info = await self._clients[name].call("getblockchaininfo")
            entries = info.get("maxBlockSizes", [])
            if any(entry.get(expected_key) == self._scheduled_height for entry in entries):
                log.info(f"{name}: confirmed -- maxBlockSizes has {{{expected_key}: {self._scheduled_height}}}")
            else:
                failures.append(f"{name}: no maxBlockSizes entry for {expected_key} at height {self._scheduled_height}, got {entries}")
        if failures:
            raise CeremonyError(
                f"max-block-size change not confirmed via RPC on {len(failures)}/{len(ALL_NODE_NAMES)} node(s):\n  "
                + "\n  ".join(failures)
            )

    # -- height polling (same liveness/stall approach as the other simulate_*.py scripts) --

    async def _wait_for_height(self, node_names, target):
        stall_timeout_seconds = max(
            MIN_STALL_TIMEOUT_SECONDS, int(self._round_duration) * STALL_TIMEOUT_ROUND_MULTIPLIER
        )
        last_state = None
        last_progress_at = time.monotonic()
        while True:
            infos = await asyncio.gather(*(self._node_chain_info(name) for name in node_names))
            heights = [info["blocks"] if info else None for info in infos]
            if all(height is not None and height >= target for height in heights):
                return min(heights)

            state = tuple(info["bestblockhash"] if info else None for info in infos)
            now = time.monotonic()
            if state != last_state:
                last_state = state
                last_progress_at = now
            elif now - last_progress_at >= stall_timeout_seconds:
                stuck = {name: h for name, h in zip(node_names, heights) if h is None or h < target}
                raise TimeoutError(
                    f"height {target} not reached and no bestblockhash change across {node_names} for "
                    f"{stall_timeout_seconds}s: {stuck}"
                )
            await asyncio.sleep(HEIGHT_POLL_INTERVAL_SECONDS)

    async def _node_chain_info(self, name):
        try:
            return await self._clients[name].call("getblockchaininfo")
        except RpcUnreachable:
            return None


async def main():
    rpc_user = os.environ.get("CORE_RPC_USER", "rpcuser")
    rpc_pass = os.environ.get("CORE_RPC_PASS", "rpcpassword")
    round_duration = os.environ.get("ROUND_DURATION", "60")
    height_offset = int(os.environ.get("MAX_BLOCK_SIZE_HEIGHT", "10"))
    new_max_block_size = int(os.environ.get("MAX_BLOCK_SIZE_NEW", "2000000"))

    require_executable(
        default_tapyrus_setup_bin(),
        "Build it first: ./scripts/checkout_repos.py && cd workdir/tapyrus-signer && cargo build --release",
    )
    log.step(f"simulating a max-block-size change: {new_max_block_size} takes effect {height_offset} block(s) from now")
    simulator = MaxBlockSizeChangeSimulator(rpc_user, rpc_pass, round_duration, height_offset, new_max_block_size)
    await simulator.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (CeremonyError, TimeoutError, RpcError, RpcUnreachable) as exc:
        log.error(str(exc))
        sys.exit(1)
