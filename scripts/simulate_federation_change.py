#!/usr/bin/env python3
"""Drive a genuine aggpubkey rotation: signer-set-b runs its own offline ceremony
(with one member's identity deliberately REUSED from signer-set-a, not fresh --
see point 3 below), signer-set-a signs off on the handoff via tapyrus-setup's
--xfield sign/computesig flow, federations.toml is regenerated for all 5 involved
signer nodes, signer-set-b's two genuinely new identities are brought up alongside
the still-running signer-set-a (never stopped or restarted -- see point 4), and the
script waits for the scheduled height to confirm the chain keeps advancing past the
handoff.

Encodes doc/weekly-integration-test-plan.md section 3 step 9 / section 4a's
"Federation change" note. Built by reading the real tapyrus-signer source directly
(v0.4.8 of the `tapyrus` crate it depends on, plus chaintope/tapyrus-signer@master).
Five things worth knowing:

1. **--xfield encoding, verified against real test vectors** (tapyrus-signer's
   src/cli/setup/sign.rs test_execute_xfield, and rust-tapyrus v0.4.8's
   src/blockdata/block.rs Encodable impl for XField): AggregatePublicKey(pubkey)
   encodes as a 1-byte type tag (0x01), a CompactSize length prefix, then the raw
   compressed-pubkey bytes -- e.g. 33 bytes -> "0121" + the 66-hex-char pubkey. See
   _aggpubkey_xfield_hex below.

2. **federations.toml membership is per-entry, not implicit**: reading
   tapyrus-signer's federation.rs, a [[federation]] entry's `threshold`/`node-vss`
   fields are `Option<...>` and "must be None when the signer is not a member of the
   federation" (the struct's own doc comment). So a node not in a given federation
   gets that entry with just block-height/aggregated-public-key(/signature) -- enough
   to know about and verify it, not enough to sign under it. Also: node-vss in a
   member entry is already filtered to that one node's own shares at file-generation
   time (extract_vss_for), not written in full and filtered at load time -- confirmed
   by reading assemble_signer_configs.py's existing (already-active) code, not
   assumed.

3. **One signer identity is shared across both federations, deliberately**: node-0
   (signer-0) keeps its EXISTING key from signer-set-a as one of signer-set-b's 3
   members too (PartialReuseAggpubkeyCeremony), rather than signer-set-b being 3
   entirely fresh keys. It ends up a full member of BOTH federation entries in its
   own federations.toml, so it can keep contributing to signer-set-a's threshold
   right up to the handoff height and immediately continue under signer-set-b after
   it -- no gap where nobody who's currently running has a valid entry in the
   about-to-be-active federation. Only signer-b-1/signer-b-2 (node-0's two
   signer-set-b co-members) are genuinely new identities needing new containers.

4. **signer-0/1/2 are never stopped or restarted -- federations.toml is live-reloaded
   instead**: src/federation_watcher.rs (read directly, not guessed) runs a background
   thread that watches federations.toml's directory and re-reads/re-validates it on
   any create/write/rename touching that filename, applying a successful reload
   between rounds (never mid-round). A malformed edit is logged and ignored, not
   applied, so this script's own federations.toml correctness matters more, not less,
   than it would with a restart. Per that file's own tests, the watcher only reliably
   observes a *complete* file -- edits must go through a write-to-temp-then-rename
   (see _write_atomic below), not an in-place write, matching its own test helper.

5. **Shared redis, global broadcast channel**: signer-b-1/signer-b-2's containers
   (docker/docker-compose.yml) run ALONGSIDE signer-set-a's existing ones, sharing the
   same redis instance and the same first-layer core RPC targets (core-2a/3a).
   tapyrus-signer's round-coordination broadcast channel is a single global redis
   pub/sub channel ("tapyrus-signer"), not scoped per federation/aggpubkey (see
   net.rs) -- worth watching for cross-talk between the two concurrently-running
   signer sets in a live run.

Usage:
    ./scripts/simulate_federation_change.py

Requires signer-set-a already generated and signing (same precondition as
scripts/simulate_reorg.py). Reads ROUND_DURATION / FEDERATION_CHANGE_OFFSET_BLOCKS from the
environment -- the same job-level env vars the workflow already sets for the other
steps.

Why not tapyrus-core's generatetoaddress RPC (instant block mining): same reason as
simulate_reorg.py -- no single party holds the aggregate private key by design.
"""
import asyncio
import os
import sys
import time
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.assemble_signer_configs import CoreRpc, Redis, SignerConfigAssembler  # noqa: E402
from scripts.generate_dev_secrets import AggpubkeyCeremony, default_tapyrus_setup_bin  # noqa: E402
from scripts.lib.ceremony import CeremonyError, TapyrusSetupCeremony, extract_vss_for, require_executable  # noqa: E402
from scripts.lib.compose import ComposeError, bring_up  # noqa: E402
from scripts.lib.log import log  # noqa: E402
from scripts.lib.orchestrator_control import pause_node_orchestrators, resume_node_orchestrators  # noqa: E402
from scripts.lib.rpc import CoreRpcClient, RpcError, RpcUnreachable  # noqa: E402

RPC_HOST = "127.0.0.1"
SIGNER_SET_A = "signer-set-a"
SIGNER_SET_B = "signer-set-b"
SIGNER_COUNT = 3
SIGNER_THRESHOLD = 2
CORE_RPC_PORT = "12381"
REDIS_HOST = "redis"
REDIS_PORT = "6379"
HEIGHT_POLL_INTERVAL_SECONDS = 5
# Same liveness-timeout approach as simulate_reorg.py's _wait_for_height (see that
# script for the full rationale) -- duplicated rather than shared, matching this
# repo's existing per-script self-containment (each ceremony script already has its
# own default_tapyrus_setup_bin/TAPYRUS_SETUP_BUILD_HINT, etc.).
STALL_TIMEOUT_ROUND_MULTIPLIER = 4
MIN_STALL_TIMEOUT_SECONDS = 180

# Only 2 new containers, not 3 -- node-0's identity is reused as one of signer-set-b's
# members too (see this module's docstring point 3), so it doesn't need one.
SIGNERS_B = ("signer-b-1", "signer-b-2")
REUSE_NODE_INDEX = 0
# Mirrors signer-1/signer-2's own first-layer mapping (docker/docker-compose.yml) --
# RPC access isn't exclusive to one client, so reusing the same nodes avoids needing
# more core nodes / a topology change just for signer-set-b.
SIGNER_B_RPC_HOSTS = ("core-2a", "core-3a")

ALL_NODES = (
    ("core-1a", 12381), ("core-1b", 12382), ("core-2a", 12383),
    ("core-2b", 12384), ("core-3a", 12385), ("core-3b", 12386), ("core-7", 12387),
)
ALL_NODE_NAMES = tuple(name for name, _ in ALL_NODES)


def _aggpubkey_xfield_hex(pubkey_hex):
    """Encodes XField::AggregatePublicKey(pubkey) as the hex string tapyrus-setup
    sign/computesig --xfield expects -- see this module's docstring point 1.
    """
    pubkey_bytes = bytes.fromhex(pubkey_hex)
    if len(pubkey_bytes) != 33 or pubkey_hex[:2] not in ("02", "03"):
        raise CeremonyError(f"expected a 33-byte compressed pubkey for --xfield, got: {pubkey_hex}")
    return f"01{len(pubkey_bytes):02x}{pubkey_hex}"


def _federation_entry_toml(block_height, aggpubkey, threshold=None, node_vss_lines=None, signature=None):
    """One [[federation]] entry. threshold/node_vss_lines are omitted entirely (not
    just empty) for a federation this node isn't a member of -- see this module's
    docstring point 2. signature is omitted for block-height 0 (tapyrus-signer's
    Federation::from never looks for one there).
    """
    lines = ["[[federation]]", f"block-height = {block_height}"]
    if threshold is not None:
        lines.append(f"threshold = {threshold}")
    lines.append(f'aggregated-public-key = "{aggpubkey}"')
    if node_vss_lines is not None:
        vss_block = ",\n".join(f'    "{vss}"' for vss in node_vss_lines)
        lines.append(f"node-vss = [\n{vss_block}\n]")
    if signature is not None:
        lines.append(f'signature = "{signature}"')
    return "\n".join(lines) + "\n"


class PartialReuseAggpubkeyCeremony(AggpubkeyCeremony):
    """Same ceremony as AggpubkeyCeremony, but one signer slot reuses an EXISTING
    key (signer-set-a's own node-0) instead of generating a fresh one via createkey
    -- see this module's docstring point 3 for why. Subclasses rather than edits
    AggpubkeyCeremony: the rest of the ceremony (createnodevss/aggregate) only cares
    that self._wifs/self._pubkeys end up populated, not how, so only _create_keys
    needs overriding.
    """

    def __init__(self, tapyrus_setup_bin, set_dir, node_count, threshold, reuse_index, reuse_wif, reuse_pubkey):
        super().__init__(tapyrus_setup_bin, set_dir, node_count, threshold)
        self._reuse_index = reuse_index
        self._reuse_wif = reuse_wif
        self._reuse_pubkey = reuse_pubkey

    async def _create_keys(self):
        fresh_indices = [i for i in range(self._node_count) if i != self._reuse_index]
        log.info(
            f"generating {len(fresh_indices)} fresh signer keys + reusing signer-set-a's "
            f"node-{self._reuse_index} identity (threshold {self._threshold})..."
        )
        results = await asyncio.gather(*(self._run_setup("createkey") for _ in fresh_indices))

        self._wifs = [None] * self._node_count
        self._pubkeys = [None] * self._node_count
        self._wifs[self._reuse_index] = self._reuse_wif
        self._pubkeys[self._reuse_index] = self._reuse_pubkey
        for i, result in zip(fresh_indices, results):
            wif, pubkey = result.split()
            self._wifs[i] = wif
            self._pubkeys[i] = pubkey

        for i in range(self._node_count):
            node_dir = self._set_dir / f"node-{i}"
            node_dir.mkdir(exist_ok=True)
            self._write_secret(node_dir / "signer.key", self._wifs[i])
            (node_dir / "public-key.txt").write_text(self._pubkeys[i])


class XFieldSignoffCeremony(TapyrusSetupCeremony):
    """Runs createblockvss -> sign -> computesig against an ALREADY-established
    signer set, producing a signature over an xfield change rather than a block --
    the "current federation signs off on the handoff" step. A fresh createblockvss
    round, not a reuse of the genesis-signing one (see scripts/sign_genesis.py) --
    reusing VSS-derived signing material across two different signed payloads is a
    nonce-reuse footgun in Schnorr-style multi-party signing, not just unnecessary.

    Deliberately its own class, not a subclass of sign_genesis.py's
    GenesisSigningCeremony: --block and --xfield are mutually exclusive at the CLI
    level, and computesig's xfield output is a bare signature, not a signed block, so
    little of that class's block-specific plumbing would actually be reused.
    """

    def __init__(self, tapyrus_setup_bin, set_dir, threshold, pubkeys, xfield_hex):
        super().__init__(tapyrus_setup_bin)
        self._set_dir = set_dir
        # Separate from set_dir/raw/ (the original aggpubkey ceremony's node-vss,
        # which this reuses read-only below) -- this is a fresh block-vss round of
        # its own.
        self._raw_dir = set_dir / "raw" / "xfield-signoff"
        self._raw_dir.mkdir(parents=True, exist_ok=True)
        self._threshold = threshold
        self._pubkeys = pubkeys
        self._node_count = len(pubkeys)
        self._xfield = xfield_hex
        self._aggpubkey = (set_dir / "aggregated-public-key.txt").read_text().rstrip("\n")

    async def run(self):
        await self._create_block_vss()
        await self._sign()
        return await self._computesig()

    def _node_dir(self, i):
        return self._set_dir / f"node-{i}"

    def _wif(self, i):
        return (self._node_dir(i) / "signer.key").read_text().rstrip("\n")

    def _node_secret_share(self, i):
        return (self._node_dir(i) / "node-secret-share.hex").read_text().rstrip("\n")

    def _pubkey_args(self):
        return [f"--public-key={pubkey}" for pubkey in self._pubkeys]

    async def _create_block_vss(self):
        log.info("running createblockvss for each signer (fresh round for the xfield signoff)...")
        outputs = await asyncio.gather(*(
            self._run_setup(
                "createblockvss", *self._pubkey_args(),
                f"--private-key={self._wif(i)}", f"--threshold={self._threshold}",
            )
            for i in range(self._node_count)
        ))
        for i, output in enumerate(outputs):
            (self._raw_dir / f"blockvss_from_{i}.txt").write_text(output)

    async def _sign(self):
        log.info("running sign for each signer (--xfield)...")

        async def sign_one(j):
            pubkey_j = self._pubkeys[j]
            vss_args = [
                f"--block-vss={self._extract_vss_for(self._raw_dir / f'blockvss_from_{i}.txt', pubkey_j)}"
                for i in range(self._node_count)
            ]
            return await self._run_setup(
                "sign", *vss_args,
                f"--aggregated-public-key={self._aggpubkey}",
                f"--node-secret-share={self._node_secret_share(j)}",
                f"--private-key={self._wif(j)}", f"--xfield={self._xfield}",
                f"--threshold={self._threshold}",
            )

        local_sigs = await asyncio.gather(*(sign_one(j) for j in range(self._node_count)))
        for j, local_sig in enumerate(local_sigs):
            (self._node_dir(j) / "local-sig-xfield.txt").write_text(local_sig)

    async def _computesig(self):
        log.info(f"running computesig (signer 0 combines all {self._node_count} local signatures, --xfield)...")
        signer0_pubkey = self._pubkeys[0]

        sig_args = [
            f"--sig={(self._node_dir(i) / 'local-sig-xfield.txt').read_text().rstrip(chr(10))}"
            for i in range(self._node_count)
        ]
        blockvss_args = [
            f"--block-vss={self._extract_vss_for(self._raw_dir / f'blockvss_from_{i}.txt', signer0_pubkey)}"
            for i in range(self._node_count)
        ]
        # node-vss reuses the ORIGINAL aggpubkey ceremony's output (set_dir/raw/, not
        # this class's own xfield-signoff raw dir) -- it identifies each signer's
        # share of the aggregate key itself, established once, not re-derived per
        # signed payload (unlike block-vss, which is fresh every time -- see above).
        nodevss_args = [
            f"--node-vss={self._extract_vss_for(self._set_dir / 'raw' / f'nodevss_from_{i}.txt', signer0_pubkey)}"
            for i in range(self._node_count)
        ]

        result = await self._run_setup(
            "computesig", *sig_args, *blockvss_args, *nodevss_args,
            f"--aggregated-public-key={self._aggpubkey}",
            f"--node-secret-share={self._node_secret_share(0)}",
            f"--private-key={self._wif(0)}", f"--xfield={self._xfield}",
            f"--threshold={self._threshold}",
        )
        # computesig's xfield output is a bare signature (no second token for a
        # .split() to naturally absorb the trailing newline into, unlike createkey/
        # aggregate above) -- left raw, that trailing "\n" lands inside the quoted
        # TOML string built in _federation_entry_toml, which federation_watcher then
        # rejects as invalid (NewlineInString) and silently keeps serving the
        # last-known-good federations forever, well past the scheduled height.
        return result.rstrip("\n")


class RotationSignerConfigAssembler(SignerConfigAssembler):
    """Writes signer-set-b's node configs: the same tapyrus-signer.toml
    SignerConfigAssembler already produces (inherited, unchanged), but a
    federations.toml with a non-member copy of signer-set-a's original entry
    followed by this set's own full entry -- SignerConfigAssembler alone only ever
    writes a single, always-block-height-0, always-member entry (see its own
    docstring), which doesn't fit a rotation target. Subclassed rather than editing
    that already-active class.
    """

    def __init__(self, prior_entry_toml, block_height, signature_hex, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._prior_entry_toml = prior_entry_toml
        self._block_height = block_height
        self._signature_hex = signature_hex

    def _federations_toml(self, pubkey, node_vss_pubkeys):
        # SIGNER_COUNT (the full original ceremony size), not len(node_vss_pubkeys)
        # -- this is called for just the 2 new nodes (node_vss_pubkeys is that
        # smaller subset), but nodevss_from_{i}.txt exists for all 3 original
        # senders (0/1/2), and a share is needed from each of them.
        node_vss_lines = [
            extract_vss_for(self._raw_dir / f"nodevss_from_{i}.txt", pubkey)
            for i in range(SIGNER_COUNT)
        ]
        own_entry = _federation_entry_toml(
            self._block_height, self._aggpubkey,
            threshold=self._threshold, node_vss_lines=node_vss_lines, signature=self._signature_hex,
        )
        return self._prior_entry_toml + "\n" + own_entry


class FederationChangeSimulator:
    def __init__(self, round_duration, height_offset):
        self._round_duration = round_duration
        self._height_offset = height_offset
        self._clients = {name: CoreRpcClient(RPC_HOST, port, name) for name, port in ALL_NODES}
        self._tapyrus_setup_bin = default_tapyrus_setup_bin()
        self._pubkeys_a = self._read_pubkeys(SIGNER_SET_A)
        self._aggpubkey_a = self._read_aggpubkey(SIGNER_SET_A)
        self._aggpubkey_b = None
        self._scheduled_height = None
        self._signature_hex = None

    async def run(self):
        await self._generate_signer_set_b()
        await self._sign_off_handoff()
        await self._compute_scheduled_height()
        # Paused from the federations.toml write through RPC confirmation: a core
        # node mid-restart exactly when _confirm_rotation_via_rpc's single,
        # non-retrying getblockchaininfo check runs would fail the whole step
        # spuriously. try/finally so a failure partway through still resumes chaos
        # for the rest of the job. See scripts/lib/orchestrator_control.py.
        pause_node_orchestrators()
        try:
            self._write_configs()
            await self._bring_up_signer_set_b()
            await self._wait_for_rotation()
            await self._confirm_rotation_via_rpc()
        finally:
            resume_node_orchestrators()
        log.info(
            f"done. signer-set-b ({self._aggpubkey_b[:12]}...) took over at height "
            f"{self._scheduled_height} (confirmed via getblockchaininfo on all 7 core nodes), signed off by "
            f"signer-set-a ({self._aggpubkey_a[:12]}...); signer-0/1/2 never restarted (live-reloaded "
            f"federations.toml instead)."
        )

    def _set_dir(self, set_name):
        return REPO_ROOT / "secrets" / set_name

    def _read_pubkeys(self, set_name):
        return [line for line in (self._set_dir(set_name) / "pubkeys.txt").read_text().splitlines() if line]

    def _read_aggpubkey(self, set_name):
        return (self._set_dir(set_name) / "aggregated-public-key.txt").read_text().strip()

    def _read_to_address(self, node_dir):
        with (node_dir / "tapyrus-signer.toml").open("rb") as f:
            return tomllib.load(f)["signer"]["to-address"]

    def _write_atomic(self, path, content):
        """Write-to-temp-then-rename -- federation_watcher.rs's own tests write this
        way (write_atomically), and its module docstring says the watcher is only
        guaranteed to observe a *complete* file this way, not via an in-place write.
        with_suffix(".tmp") mirrors the Rust test helper's with_extension("tmp")
        (replaces the extension, doesn't append to it).
        """
        tmp_path = path.with_suffix(".tmp")
        tmp_path.write_text(content)
        tmp_path.rename(path)

    async def _generate_signer_set_b(self):
        log.step(
            f"generating signer-set-b's aggpubkey ({SIGNER_COUNT} signers, threshold {SIGNER_THRESHOLD}, "
            f"node-{REUSE_NODE_INDEX} shared with signer-set-a)"
        )
        require_executable(
            self._tapyrus_setup_bin,
            "Build it first: ./scripts/checkout_repos.py && cd workdir/tapyrus-signer && cargo build --release",
        )
        set_dir = self._set_dir(SIGNER_SET_B)
        if (set_dir / "pubkeys.txt").exists():
            raise CeremonyError(f"{set_dir} already has generated keys -- remove it first to regenerate")
        reuse_dir = self._set_dir(SIGNER_SET_A) / f"node-{REUSE_NODE_INDEX}"
        reuse_wif = (reuse_dir / "signer.key").read_text().rstrip("\n")
        reuse_pubkey = (reuse_dir / "public-key.txt").read_text().rstrip("\n")
        ceremony = PartialReuseAggpubkeyCeremony(
            self._tapyrus_setup_bin, set_dir, SIGNER_COUNT, SIGNER_THRESHOLD,
            REUSE_NODE_INDEX, reuse_wif, reuse_pubkey,
        )
        self._aggpubkey_b = await ceremony.run()
        log.info(f"signer-set-b converged on aggregated public key: {self._aggpubkey_b[:12]}...")

    async def _sign_off_handoff(self):
        log.step("signer-set-a signing off on the handoff to signer-set-b (--xfield sign/computesig)")
        xfield_hex = _aggpubkey_xfield_hex(self._aggpubkey_b)
        ceremony = XFieldSignoffCeremony(
            self._tapyrus_setup_bin, self._set_dir(SIGNER_SET_A), SIGNER_THRESHOLD, self._pubkeys_a, xfield_hex,
        )
        self._signature_hex = await ceremony.run()
        log.info(f"handoff signature: {self._signature_hex[:12]}...")

    async def _compute_scheduled_height(self):
        current_height = await self._clients["core-1a"].call("getblockcount")
        self._scheduled_height = current_height + self._height_offset
        log.info(
            f"scheduling signer-set-b's takeover at height {self._scheduled_height} "
            f"(current {current_height} + {self._height_offset})"
        )

    def _write_configs(self):
        log.step("regenerating federations.toml for all 5 involved signer nodes (3 signer-set-a + 2 new signer-set-b)")
        a_dir = REPO_ROOT / "runtime" / "signers"
        b_dir = REPO_ROOT / "runtime" / "signers-b"
        b_pubkeys = self._read_pubkeys(SIGNER_SET_B)
        b_raw_dir = self._set_dir(SIGNER_SET_B) / "raw"

        # node-0 is shared between both federations (see _generate_signer_set_b) --
        # it gets a FULL member entry 2 (its own filtered node-vss from signer-set-b's
        # ceremony), not the non-member stub the other two signer-set-a nodes get, so
        # it stays a valid signer straight through the handoff.
        shared_node_vss = [
            extract_vss_for(b_raw_dir / f"nodevss_from_{i}.txt", b_pubkeys[REUSE_NODE_INDEX])
            for i in range(SIGNER_COUNT)
        ]
        entry2_member = _federation_entry_toml(
            self._scheduled_height, self._aggpubkey_b,
            threshold=SIGNER_THRESHOLD, node_vss_lines=shared_node_vss, signature=self._signature_hex,
        )
        entry2_nonmember = _federation_entry_toml(
            self._scheduled_height, self._aggpubkey_b, signature=self._signature_hex,
        )
        # Atomic write, not a plain write_text -- signer-0/1/2 are never restarted
        # (see this module's docstring point 4), so this edit is what
        # federation_watcher.rs's background thread has to observe live.
        for i in range(SIGNER_COUNT):
            federations_path = a_dir / f"node-{i}" / "federations.toml"
            new_entry = entry2_member if i == REUSE_NODE_INDEX else entry2_nonmember
            self._write_atomic(federations_path, federations_path.read_text() + "\n" + new_entry)

        # signer-b-1/signer-b-2 (signer-set-b's two genuinely new identities): a
        # non-member copy of signer-set-a's original entry (they weren't part of it),
        # then their own full entry. Reuses SignerConfigAssembler's
        # tapyrus-signer.toml writer unchanged (RotationSignerConfigAssembler only
        # overrides _federations_toml); written directly per node rather than via
        # run(), so each one's output directory keeps its TRUE index in signer-set-b's
        # own numbering (node-1/node-2, matching docker-compose.yml's signer-b-1/
        # signer-b-2 mounts) instead of being renumbered from zero.
        entry1_nonmember = _federation_entry_toml(0, self._aggpubkey_a)
        assembler = RotationSignerConfigAssembler(
            entry1_nonmember, self._scheduled_height, self._signature_hex,
            self._set_dir(SIGNER_SET_B), SIGNER_THRESHOLD, self._aggpubkey_b,
            CoreRpc(CORE_RPC_PORT), Redis(REDIS_HOST, REDIS_PORT),
            self._round_duration,
        )
        new_indices = [i for i in range(SIGNER_COUNT) if i != REUSE_NODE_INDEX]
        for idx, rpc_host in zip(new_indices, SIGNER_B_RPC_HOSTS):
            assembler._write_node(
                b_dir / f"node-{idx}",
                pubkey=b_pubkeys[idx],
                address=self._read_to_address(a_dir / f"node-{idx}"),
                core_rpc_host=rpc_host,
                node_vss_pubkeys=b_pubkeys,
            )

    async def _bring_up_signer_set_b(self):
        log.step("bringing up signer-b-1/signer-b-2 (signer-0/1/2 stay running -- see below)")
        await bring_up(*SIGNERS_B)

    async def _wait_for_rotation(self):
        log.step(f"waiting for the chain to reach the scheduled height {self._scheduled_height}")
        await self._wait_for_height(ALL_NODE_NAMES, self._scheduled_height)
        log.info(f"height {self._scheduled_height} reached")

    async def _confirm_rotation_via_rpc(self):
        # getblockchaininfo, not container/signer logs or script-reported state: its
        # aggregatePubkeys array is core's own consensus-level history of xfield
        # AggregatePublicKey changes -- [{"<pubkey-hex>": <height>}, ...], confirmed
        # directly from tapyrus-core source (src/rpc/blockchain.cpp's
        # GetXFieldNameForRpc / src/xfieldhistory.cpp's CXFieldHistory::ToUniValue,
        # value via XFieldAggPubKey::ToString() = HexStr(data), same hex format
        # self._aggpubkey_b already is). Checked on all 7 core nodes, not just one --
        # this is what actually proves the new federation took effect network-wide,
        # independent of whether any particular signer's own reload path worked.
        log.step(
            f"confirming aggregatePubkeys includes {self._aggpubkey_b[:12]}... at height "
            f"{self._scheduled_height} via getblockchaininfo (all 7 core nodes)"
        )
        failures = []
        for name in ALL_NODE_NAMES:
            info = await self._clients[name].call("getblockchaininfo")
            entries = info.get("aggregatePubkeys", [])
            if any(entry.get(self._aggpubkey_b) == self._scheduled_height for entry in entries):
                log.info(f"{name}: confirmed -- aggregatePubkeys has {{{self._aggpubkey_b[:12]}...: {self._scheduled_height}}}")
            else:
                failures.append(f"{name}: no aggregatePubkeys entry for {self._aggpubkey_b} at height {self._scheduled_height}, got {entries}")
        if failures:
            raise CeremonyError(
                f"rotation not confirmed via RPC on {len(failures)}/{len(ALL_NODE_NAMES)} node(s):\n  "
                + "\n  ".join(failures)
            )

    # -- height polling (same liveness/stall approach as simulate_reorg.py) --------

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
    round_duration = os.environ.get("ROUND_DURATION", "60")
    height_offset = int(os.environ.get("FEDERATION_CHANGE_OFFSET_BLOCKS", "10"))

    log.step(f"simulating a federation change: signer-set-b takes over {height_offset} block(s) from now")
    simulator = FederationChangeSimulator(round_duration, height_offset)
    await simulator.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (CeremonyError, TimeoutError, ComposeError, RpcError, RpcUnreachable) as exc:
        log.error(str(exc))
        sys.exit(1)
