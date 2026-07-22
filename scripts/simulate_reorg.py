#!/usr/bin/env python3
"""Drive a genuine two-sided reorg: split the 7-node network into two isolated
groups, let each independently threshold-sign its own fork from a common baseline,
reconnect, and confirm the losing group's fork shows up as a real `valid-fork` tip via
getchaintips -- not simulated.

This is the exact 8-step recipe in doc/weekly-integration-test-plan.md section 4d,
already run once by hand end-to-end (see doc/work-done.md's "Reorg -- full run
transcript" for the verified transcript this script encodes).

Usage:
    ./scripts/simulate_reorg.py

Requires the 7-node topology and signer-set-a already up and converged (same
precondition as scripts/generate_traffic.py). Reads
CHAIN_HEIGHT_BEFORE_REORG / REORG_LOSER_BLOCKS / REORG_WINNER_MARGIN /
CORE_RPC_USER / CORE_RPC_PASS / ROUND_DURATION from the environment -- the same
job-level env vars the workflow already sets for the other steps.

CHAIN_HEIGHT_BEFORE_REORG is a floor to wait for, not an assumed starting point (and
in the workflow, it's always TX_ROUND_COUNT + 2, not an independent value -- see the
"Derive CHAIN_HEIGHT_BEFORE_REORG" step -- tying it to whatever
scripts/generate_traffic.py produces when that runs first in the same job). The
baseline step waits until height >= CHAIN_HEIGHT_BEFORE_REORG, then uses whatever
height was *actually* reached -- which can be higher -- as the real reference point
for the loser/winner fork targets. Using the literal input value instead would let
already-elapsed height silently satisfy those targets too: group A/B would "build
their fork" without producing any new blocks at all, a silent no-op reorg instead of
a real one.

Why not tapyrus-core's generatetoaddress RPC (instant block mining): it requires the
aggregate *private* key as a parameter, which no single party holds by design in a
real threshold-signed federation (it's split across the 3 signers via VSS). Blocks
here can only come from the live tapyrus-signerd trio's normal round-robin process, at
ROUND_DURATION cadence -- this script is inherently as slow as that process, not a
shortcut around it.

Signer-set-a's repoint step (recipe step 5) reuses
scripts/assemble_signer_configs.py's SignerConfigAssembler directly (not a
subprocess) to regenerate tapyrus-signer.toml with a new rpc-endpoint-host --
pubkeys come from secrets/<set-name>/pubkeys.txt (persistent for the whole job); each
node's to-address is re-read from its own already-written
runtime/signers/node-<i>/tapyrus-signer.toml (via stdlib tomllib) rather than
depending on the original "Collect coinbase addresses" step's /tmp file still being
around -- keeps this script self-contained.

Also verifies transactions aren't silently lost during the reorg: right after the
split, sends one canary TPC transaction from CANARY_SENDER to CANARY_RECEIVER (both
group A, using a UTXO that predates the split so it can't legitimately conflict with
anything group B does) -- this gets mined into one of group A's now-doomed blocks. In
Bitcoin-Core-derived reorg handling (which tapyrus-core inherits), a disconnected
block's transactions are pushed back into the mempool unless their inputs are already
spent on the new chain (a real conflict, correctly dropped) or they depend on an
output that only existed on the losing side (an "orphan" case mempool logic doesn't
always handle cleanly -- historically one of Bitcoin Core's more bug-prone corners).
After reconnection, this script confirms the canary is either re-confirmed or back in
the mempool -- not vanished. Deliberately the simple case only (one transaction, input
predates the fork point, no legitimate conflict is possible) -- dependent-transaction
chains and deliberate double-spend/conflict scenarios are real follow-ups, not
attempted here. Confirms "not lost" (mempool or re-confirmed), not "re-mined into a
new block": that would need signer-set-a restarted after reconnection to produce one
more real block, which this script doesn't do (signers stay stopped after the winning
fork is built, matching the base recipe).
"""
import asyncio
import os
import subprocess
import sys
import time
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.assemble_signer_configs import CoreRpc, Redis, SignerConfigAssembler  # noqa: E402
from scripts.lib.log import log  # noqa: E402
from scripts.lib.rpc import CoreRpcClient, RpcError, RpcUnreachable  # noqa: E402
from scripts.wait_for_topology import TopologyWaiter  # noqa: E402

RPC_HOST = "127.0.0.1"
DOCKER_DIR = REPO_ROOT / "docker"
SIGNER_SET_NAME = "signer-set-a"
SIGNER_THRESHOLD = 2
CORE_RPC_PORT = "12381"
REDIS_HOST = "redis"
REDIS_PORT = "6379"
HEIGHT_POLL_INTERVAL_SECONDS = 5
HEIGHT_POLL_TIMEOUT_SECONDS = 600
CONVERGENCE_TIMEOUT_SECONDS = 120
CANARY_SEND_AMOUNT_TPC = 0.001

SIGNERS = ("signer-0", "signer-1", "signer-2")
CANARY_SENDER = "core-1a"
CANARY_RECEIVER = "core-1b"

# (node name, host-published RPC port) -- see docker/docker-compose.yml's port
# mappings. Same node set generate_traffic.py/wait_for_topology.py use.
NODES = (
    ("core-1a", 12381),
    ("core-1b", 12382),
    ("core-2a", 12383),
    ("core-2b", 12384),
    ("core-3a", 12385),
    ("core-3b", 12386),
    ("core-7", 12387),
)
GROUP_A = ("core-1a", "core-1b", "core-2a", "core-2b")
GROUP_B = ("core-3a", "core-3b", "core-7")
ALL_NODE_NAMES = tuple(name for name, _ in NODES)


class ReorgError(Exception):
    """A reorg-recipe step didn't produce the expected chain state."""


class ReorgSimulator:
    def __init__(self, rpc_user, rpc_pass, baseline_height, loser_blocks, winner_margin, round_duration):
        self._rpc_user = rpc_user
        self._rpc_pass = rpc_pass
        self._baseline_height = baseline_height
        self._loser_blocks = loser_blocks
        self._winner_margin = winner_margin
        self._round_duration = round_duration
        self._clients = {name: CoreRpcClient(RPC_HOST, port, rpc_user, rpc_pass) for name, port in NODES}
        self._losing_tip = None
        self._winning_tip = None
        self._canary_txid = None

    async def run(self):
        await self._build_baseline()
        await self._split()
        await self._inject_canary_transaction()
        await self._build_losing_fork()
        await self._bring_back_group_b()
        await self._repoint_and_restart_signers()
        await self._build_winning_fork()
        await self._reconnect()
        await self._confirm_convergence()
        await self._verify_canary_transaction_survived()
        log.info(
            f"done. group A's {self._loser_blocks}-block fork (tip {self._losing_tip[:12]}...) confirmed as a "
            f"valid-fork on every ex-group-A node; the network converged on group B's longer chain; the canary "
            f"transaction ({self._canary_txid[:12]}...) confirmed only on the losing fork was not lost."
        )

    # -- recipe steps (doc/weekly-integration-test-plan.md section 4d) --------------

    async def _build_baseline(self):
        log.step(f"building baseline (target height {self._baseline_height}, all 7 nodes)")
        # self._baseline_height is a floor, not an assumed starting point -- the
        # ACTUAL height reached (which can be higher, e.g. if generate_traffic.py
        # already ran first in the same job) is what the loser/winner fork targets
        # get computed from, not the original literal value. See _wait_for_height.
        self._baseline_height = await self._wait_for_height(ALL_NODE_NAMES, self._baseline_height)
        tips = await asyncio.gather(*(self._clients[name].call("getbestblockhash") for name in ALL_NODE_NAMES))
        if len(set(tips)) != 1:
            raise ReorgError(f"baseline tips don't match across all 7 nodes: {dict(zip(ALL_NODE_NAMES, tips))}")
        log.info(f"baseline confirmed: all 7 nodes at height {self._baseline_height}, tip {tips[0][:12]}...")
        await self._compose("stop", *SIGNERS)

    async def _split(self):
        log.step("splitting the network: stopping core-3a/core-3b/core-7")
        await self._compose("stop", "core-3a", "core-3b", "core-7")
        # RPC mapping unchanged (signer-0/1 -> core-1a/core-2a, still live in group A);
        # signer-2 (-> core-3a) will fail to start with no reachable RPC target --
        # expected and harmless, matches the verified recipe.
        await self._compose("start", *SIGNERS)

    async def _inject_canary_transaction(self):
        log.step(f"injecting a canary transaction ({CANARY_SENDER} -> {CANARY_RECEIVER}) into group A")
        address = await self._clients[CANARY_RECEIVER].call("getnewaddress")
        self._canary_txid = await self._clients[CANARY_SENDER].call(
            "sendtoaddress", [address, CANARY_SEND_AMOUNT_TPC]
        )
        log.info(f"canary transaction {self._canary_txid[:12]}... broadcast -- will confirm only on group A's fork")

    async def _build_losing_fork(self):
        target = self._baseline_height + self._loser_blocks
        log.step(f"group A building its fork (target height {target})")
        await self._wait_for_height(GROUP_A, target)
        self._losing_tip = await self._clients["core-1a"].call("getbestblockhash")
        log.info(f"group A's fork tip: {self._losing_tip[:12]}... (height {target})")
        await self._compose("stop", *SIGNERS, *GROUP_A)

    async def _bring_back_group_b(self):
        log.step("bringing group B back (still at the baseline tip)")
        await self._compose("start", *GROUP_B)
        # No volume mounted on the redis service (docker/docker-compose.yml) -- its
        # state is entirely in the container's writable layer, so recreating it is a
        # true fresh reset, not just a process restart.
        log.step("resetting redis fresh")
        await self._compose("up", "-d", "--force-recreate", "redis")

    async def _repoint_and_restart_signers(self):
        log.step("repointing all 3 signers' RPC target to core-3a")
        set_dir = REPO_ROOT / "secrets" / SIGNER_SET_NAME
        pubkeys = [line for line in (set_dir / "pubkeys.txt").read_text().splitlines() if line]
        aggpubkey = (set_dir / "aggregated-public-key.txt").read_text().strip()
        output_dir = REPO_ROOT / "runtime" / "signers"
        addresses = [self._read_to_address(output_dir / f"node-{i}") for i in range(len(pubkeys))]

        assembler = SignerConfigAssembler(
            set_dir, SIGNER_THRESHOLD, aggpubkey,
            CoreRpc(CORE_RPC_PORT, self._rpc_user, self._rpc_pass),
            Redis(REDIS_HOST, REDIS_PORT),
            self._round_duration,
        )
        # Every signer's RPC target repoints to core-3a -- the required fix, not
        # optional: a signer with a dead RPC target can't contribute a threshold
        # share at all, even purely as a non-master over Redis (confirmed in
        # doc/work-done.md). Without this, group B would never resume signing.
        assembler.run(pubkeys, addresses, ["core-3a"] * len(pubkeys), output_dir)

        # Config is only read at tapyrus-signerd startup -- a bare restart of an
        # already-stopped container picks up the rewritten bind-mounted file.
        await self._compose("start", *SIGNERS)

    def _read_to_address(self, node_dir):
        with (node_dir / "tapyrus-signer.toml").open("rb") as f:
            return tomllib.load(f)["signer"]["to-address"]

    async def _build_winning_fork(self):
        target = self._baseline_height + self._loser_blocks + self._winner_margin
        log.step(f"group B building the longer fork (target height {target})")
        await self._wait_for_height(GROUP_B, target)
        self._winning_tip = await self._clients["core-3a"].call("getbestblockhash")
        log.info(f"group B's fork tip: {self._winning_tip[:12]}... (height {target})")
        await self._compose("stop", *SIGNERS)

    async def _reconnect(self):
        log.step("reconnecting group A alongside group B")
        await self._compose("start", *GROUP_A)
        waiter = TopologyWaiter(self._rpc_user, self._rpc_pass, CONVERGENCE_TIMEOUT_SECONDS, HEIGHT_POLL_INTERVAL_SECONDS)
        await waiter.run()

    async def _confirm_convergence(self):
        log.step("confirming convergence via getchaintips")
        for name in GROUP_A:
            tips = await self._clients[name].call("getchaintips")
            active = [t for t in tips if t["status"] == "active"]
            forks = [t for t in tips if t["status"] == "valid-fork"]
            if len(tips) != 2 or len(active) != 1 or len(forks) != 1:
                raise ReorgError(f"{name}: expected exactly 2 tips (1 active, 1 valid-fork), got {tips}")
            if active[0]["hash"] != self._winning_tip:
                raise ReorgError(f"{name}: active tip {active[0]['hash']} doesn't match group B's {self._winning_tip}")
            if forks[0]["hash"] != self._losing_tip or forks[0]["branchlen"] != self._loser_blocks:
                raise ReorgError(
                    f"{name}: valid-fork tip mismatch -- expected hash {self._losing_tip} "
                    f"branchlen {self._loser_blocks}, got {forks[0]}"
                )
            log.info(f"{name}: confirmed -- active={active[0]['hash'][:12]}... valid-fork={forks[0]['hash'][:12]}... "
                      f"branchlen={forks[0]['branchlen']}")

        # core-3a was on the winning side throughout -- never had a competing fork of
        # its own to abandon, so it should show a single active tip, not two.
        tips = await self._clients["core-3a"].call("getchaintips")
        if len(tips) != 1 or tips[0]["status"] != "active":
            raise ReorgError(f"core-3a: expected a single active tip (never split), got {tips}")
        log.info(f"core-3a: confirmed -- single active tip {tips[0]['hash'][:12]}... (never had a competing fork)")

    async def _verify_canary_transaction_survived(self):
        log.step("verifying the canary transaction survived the reorg (not lost)")
        client = self._clients[CANARY_SENDER]
        try:
            tx = await client.call("gettransaction", [self._canary_txid])
        except RpcError as exc:
            raise ReorgError(
                f"canary transaction {self._canary_txid} vanished from {CANARY_SENDER}'s wallet "
                f"entirely (not even an unconfirmed/conflicted record): {exc}"
            )

        if tx["confirmations"] > 0:
            log.info(f"canary transaction re-confirmed on the new chain (confirmations={tx['confirmations']})")
            return

        mempool_txids = await client.call("getrawmempool")
        if self._canary_txid in mempool_txids:
            log.info("canary transaction returned to the mempool -- pending, not lost")
            return

        raise ReorgError(
            f"canary transaction {self._canary_txid} was orphaned by the reorg (confirmations="
            f"{tx['confirmations']}) and never returned to the mempool -- appears to have been lost"
        )

    # -- height polling ------------------------------------------------------------

    async def _wait_for_height(self, node_names, target):
        """Waits until every node in node_names has reached at least `target`, then
        returns the height actually reached (min across those nodes) -- which may be
        higher than `target` itself if something already pushed the chain further
        before this call started (e.g. scripts/generate_traffic.py, if it ran first
        in the same job). Callers that derive further targets from this return value,
        not the original `target` they asked for, stay correct regardless of how much
        the chain had already advanced -- see the CHAIN_HEIGHT_BEFORE_REORG note in
        this script's own docstring.
        """
        deadline = time.monotonic() + HEIGHT_POLL_TIMEOUT_SECONDS
        while True:
            heights = await asyncio.gather(*(self._node_height(name) for name in node_names))
            if all(height is not None and height >= target for height in heights):
                return min(heights)
            if time.monotonic() >= deadline:
                stuck = {name: h for name, h in zip(node_names, heights) if h is None or h < target}
                raise TimeoutError(f"height {target} not reached within {HEIGHT_POLL_TIMEOUT_SECONDS}s: {stuck}")
            await asyncio.sleep(HEIGHT_POLL_INTERVAL_SECONDS)

    async def _node_height(self, name):
        try:
            return await self._clients[name].call("getblockcount")
        except RpcUnreachable:
            return None

    # -- docker compose -------------------------------------------------------------

    async def _compose(self, *args):
        log.info(f"docker compose {' '.join(args)}")
        process = await asyncio.create_subprocess_exec(
            "docker", "compose", *args, cwd=str(DOCKER_DIR),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            raise subprocess.CalledProcessError(process.returncode, ("docker", "compose", *args), stdout, stderr)


async def main():
    rpc_user = os.environ.get("CORE_RPC_USER", "rpcuser")
    rpc_pass = os.environ.get("CORE_RPC_PASS", "rpcpassword")
    baseline_height = int(os.environ.get("CHAIN_HEIGHT_BEFORE_REORG", "30"))
    loser_blocks = int(os.environ.get("REORG_LOSER_BLOCKS", "10"))
    winner_margin = int(os.environ.get("REORG_WINNER_MARGIN", "2"))
    round_duration = os.environ.get("ROUND_DURATION", "60")

    log.step(
        f"simulating a reorg: baseline height {baseline_height}, loser fork "
        f"{loser_blocks} block(s), winner margin {winner_margin} block(s)"
    )
    simulator = ReorgSimulator(rpc_user, rpc_pass, baseline_height, loser_blocks, winner_margin, round_duration)
    await simulator.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (ReorgError, TimeoutError, subprocess.CalledProcessError) as exc:
        log.error(str(exc))
        sys.exit(1)
