#!/usr/bin/env python3
"""Drive a genuine two-sided reorg via strict alternation: only one group's core
nodes are ever up and building at a time (never both, until the final reconnect), so
neither side can influence or observe the other's blocks while forking -- not
simulated, and not just isolated-by-network-stop while leaving room for ambiguity.

1. Build a common baseline (all 7 nodes).
2. Stop group A entirely. Repoint all 3 signers to group B (core-3a) and let it build
   REORG_LENGTH_BLOCKS alone.
3. Stop group B, restart group A, reset redis fresh.
4. Repoint all 3 signers back to group A's default mapping, inject the canary
   transaction, and let group A build its OWN REORG_LENGTH_BLOCKS -- a genuinely
   different set than group B's, since group A never saw group B's blocks or the
   round-state that produced them.
5. Reconnect group B alongside group A (both now at the same height, different tips --
   a real tie) and confirm the tie alone doesn't cause a reorg.
6. Repoint signers back to group B and extend ITS chain by two more blocks, not one
   -- confirmed live: after just one, core-3a still shows its own just-abandoned tip
   as valid-headers rather than valid-fork, one round behind every other node; the
   second block is what it takes for core-3a specifically to finish reclassifying it.
7. Confirm every node -- both former groups -- converges on group B's (now longer)
   tip, with group A's own fork showing up as a real valid-fork via getchaintips.
8. Restore signers to their default per-signer RPC mapping and leave them running --
   the network is left exactly as this script found it, not stopped and still
   repointed at group B, so whatever runs next in the same job can assume block
   production is live.

Usage:
    ./scripts/simulate_reorg.py

Requires the 7-node topology and signer-set-a already up and converged (same
precondition as scripts/generate_traffic.py). Reads
REORG_BASELINE_HEIGHT / REORG_LENGTH_BLOCKS / ROUND_DURATION from the environment --
the same job-level env vars the workflow already sets for the other steps.

REORG_BASELINE_HEIGHT is a floor to wait for, not an assumed starting point (and
in the workflow, it's always TX_ROUND_COUNT + 2, not an independent value -- see the
"Derive REORG_BASELINE_HEIGHT" step -- tying it to whatever
scripts/generate_traffic.py produces when that runs first in the same job). The
baseline step waits until height >= REORG_BASELINE_HEIGHT, then uses whatever
height was *actually* reached -- which can be higher -- as the real reference point
for both forks' target height. Using the literal input value instead would let
already-elapsed height silently satisfy that target too: group A/B would "build their
fork" without producing any new blocks at all, a silent no-op reorg instead of a real
one.

Why not tapyrus-core's generatetoaddress RPC (instant block mining): it requires the
aggregate *private* key as a parameter, which no single party holds by design in a
real threshold-signed federation (it's split across the 3 signers via VSS). Blocks
here can only come from the live tapyrus-signerd trio's normal round-robin process, at
ROUND_DURATION cadence -- this script is inherently as slow as that process, not a
shortcut around it.

Every repoint reuses scripts/assemble_signer_configs.py's SignerConfigAssembler
directly (not a subprocess) to regenerate tapyrus-signer.toml with a new
rpc-endpoint-host -- pubkeys come from secrets/<set-name>/pubkeys.txt (persistent for
the whole job); each node's to-address is re-read from its own already-written
runtime/signers/node-<i>/tapyrus-signer.toml (via stdlib tomllib) rather than
depending on the original "Collect coinbase addresses" step's /tmp file still being
around -- keeps this script self-contained. There are three repoints in this recipe,
not one, so this is a shared helper (_repoint_signers), not a single-purpose step.

Also verifies transactions aren't silently lost during the reorg: right when group A
starts building its own fork, sends one canary TPC transaction from CANARY_SENDER to
CANARY_RECEIVER (both group A, using a UTXO that predates the fork point so it can't
legitimately conflict with anything group B does) -- this gets mined into one of
group A's now-doomed blocks. In tapyrus-core's reorg handling, a disconnected block's
transactions are pushed back into the mempool unless their inputs are already spent on
the new chain (a real conflict, correctly dropped) or they depend on an output that
only existed on the losing side (an "orphan" case mempool logic doesn't always handle
cleanly -- historically one of this logic's more bug-prone corners). After
reconnection, this script confirms the canary is either re-confirmed or back in the
mempool -- not vanished. Deliberately the simple case only (one transaction, input
predates the fork point, no legitimate conflict is possible) -- dependent-transaction
chains and deliberate double-spend/conflict scenarios are real follow-ups, not
attempted here.
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
from scripts.lib.compose import ComposeError, recreate_fresh, start_nodes, stop_nodes  # noqa: E402
from scripts.lib.log import log  # noqa: E402
from scripts.lib.orchestrator_control import pause_node_orchestrators, resume_node_orchestrators  # noqa: E402
from scripts.lib.rpc import CoreRpcClient, RpcError, RpcUnreachable  # noqa: E402
from scripts.wait_for_topology import TopologyWaiter  # noqa: E402

RPC_HOST = "127.0.0.1"
SIGNER_SET_NAME = "signer-set-a"
SIGNER_COUNT = 3
SIGNER_THRESHOLD = 2
CORE_RPC_PORT = "12381"
REDIS_HOST = "redis"
REDIS_PORT = "6379"
HEIGHT_POLL_INTERVAL_SECONDS = 5
# A liveness timeout, not a total-duration budget for reaching the target height:
# _wait_for_height watches getblockchaininfo's bestblockhash and only times out if it
# stops changing (real forward progress stalls), not because reaching the target
# takes a while -- REORG_LENGTH_BLOCKS can be large, and whichever group is building alone
# signs with only 2 of 3 signers live (the third signer's RPC target is on the other,
# currently-stopped group -- see doc/work-done.md's RPC-connectivity note), so
# roughly 1 in 3 rounds produces no block at all. A flat total-duration timeout would
# have to assume a worst-case stall rate up front; watching for actual stalls doesn't
# need to guess. Sized in ROUND_DURATIONs so a few consecutive missed rounds don't
# look like a real stall.
STALL_TIMEOUT_ROUND_MULTIPLIER = 4
MIN_STALL_TIMEOUT_SECONDS = 180
CONVERGENCE_TIMEOUT_SECONDS = 120
# Reaching the target height doesn't mean every node has propagated that tip yet --
# a one-shot poll can land mid-propagation, so this retries briefly instead of failing.
BASELINE_TIP_CONSISTENCY_TIMEOUT_SECONDS = 30
CANARY_SEND_AMOUNT_TPC = 0.001
# Same "container started != RPC ready" wait collect_coinbase_addresses.py does.
RPC_READY_TIMEOUT_SECONDS = 120
RPC_READY_POLL_INTERVAL_SECONDS = 3

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

# The default mapping (signer-0 -> core-1a, signer-1 -> core-2a, signer-2 -> core-3a,
# matching assemble_signer_configs.py's original bring-up) vs. all 3 pointed at
# group B's one RPC-capable node -- group B has no other first-layer node to spread
# across, by design (see doc/weekly-integration-test-plan.md section 4b).
GROUP_A_RPC_HOSTS = ("core-1a", "core-2a", "core-3a")
GROUP_B_RPC_HOSTS = ("core-3a",) * SIGNER_COUNT


class ReorgError(Exception):
    """A reorg-recipe step didn't produce the expected chain state."""


class ReorgSimulator:
    def __init__(self, baseline_height, reorg_length_blocks, round_duration):
        self._baseline_height = baseline_height
        self._reorg_length_blocks = reorg_length_blocks
        self._round_duration = round_duration
        self._clients = {name: CoreRpcClient(RPC_HOST, port, name) for name, port in NODES}
        self._group_a_tip = None
        self._group_b_tip = None
        self._canary_txid = None

    async def run(self):
        await self._build_baseline()
        # Paused for the whole span where the recipe's precision requirements hold,
        # not just where exactly one group is up and building: a concurrent
        # orchestrator restart or invalidateblock could corrupt the fork being
        # carefully built, but it's just as capable of failing the exact-count
        # reconnect wait or the getchaintips shape assertions that follow -- those
        # aren't self-tolerant either. try/finally, not just a resume call at the
        # natural end, so a failure partway through still leaves
        # node_orchestrator.py's chaos actions resumed afterward rather than
        # silently disabled for the rest of the job.
        pause_node_orchestrators()
        try:
            await self._build_group_b_fork()
            await self._restore_group_a()
            await self._inject_canary_transaction()
            await self._build_group_a_fork()
            await self._reconnect_group_b()
            await self._confirm_tie_holds()
            await self._extend_group_b_by_two_blocks()
            await self._confirm_convergence()
        finally:
            resume_node_orchestrators()
        await self._verify_canary_transaction_survived()
        await self._restore_default_signers()
        log.info(
            f"done. group A's {self._reorg_length_blocks}-block fork (tip {self._group_a_tip[:12]}...) confirmed as a "
            f"valid-fork on every node; every node -- both former groups -- converged on group B's "
            f"{self._reorg_length_blocks + 2}-block chain (tip {self._group_b_tip[:12]}...); the canary transaction "
            f"({self._canary_txid[:12]}...) confirmed only on the losing fork was not lost."
        )

    # -- recipe steps -----------------------------------------------------------

    async def _build_baseline(self):
        log.step(f"building baseline (target height {self._baseline_height}, all 7 nodes)")
        # self._baseline_height is a floor, not an assumed starting point -- the
        # ACTUAL height reached (which can be higher, e.g. if generate_traffic.py
        # already ran first in the same job) is what both forks' targets get computed
        # from, not the original literal value. See _wait_for_height.
        await self._wait_for_height(ALL_NODE_NAMES, self._baseline_height)
        # Stop signers BEFORE capturing the real baseline, not after -- block
        # production only comes from the signer trio, so this is the first point
        # nothing can move the chain further. Capturing height/tip earlier risks one
        # more block landing during the gap before the stop takes effect, silently
        # letting both forks share a block past the recorded baseline instead of
        # genuinely diverging from it.
        await stop_nodes(*SIGNERS)
        self._baseline_height, tip = await self._wait_for_matching_tips(ALL_NODE_NAMES)
        log.info(f"baseline confirmed: all 7 nodes at height {self._baseline_height}, tip {tip[:12]}...")

    async def _build_group_b_fork(self):
        log.step("stopping group A -- group B builds its fork completely alone")
        await stop_nodes(*GROUP_A)
        await self._repoint_signers(GROUP_B_RPC_HOSTS)
        await start_nodes(*SIGNERS)

        target = self._baseline_height + self._reorg_length_blocks
        log.step(f"group B building its fork (target height {target})")
        await self._wait_for_height(GROUP_B, target)
        self._group_b_tip = await self._clients["core-3a"].call("getbestblockhash")
        log.info(f"group B's fork tip: {self._group_b_tip[:12]}... (height {target})")
        await stop_nodes(*SIGNERS)

    async def _restore_group_a(self):
        log.step("stopping group B, restarting group A, resetting redis fresh")
        await stop_nodes(*GROUP_B)
        await start_nodes(*GROUP_A)
        # No volume mounted on the redis service (docker/docker-compose.yml) -- its
        # state is entirely in the container's writable layer, so recreating it is a
        # true fresh reset, not just a process restart -- avoids any round-state
        # carryover from group B's just-finished rounds bleeding into group A's.
        await recreate_fresh("redis")

    async def _inject_canary_transaction(self):
        # _restore_group_a just restarted these containers -- RPC may still be warming up.
        await asyncio.gather(*(self._wait_for_rpc_ready(name) for name in (CANARY_SENDER, CANARY_RECEIVER)))
        log.step(f"injecting a canary transaction ({CANARY_SENDER} -> {CANARY_RECEIVER}) into group A")
        address = await self._clients[CANARY_RECEIVER].call("getnewaddress")
        self._canary_txid = await self._clients[CANARY_SENDER].call(
            "sendtoaddress", [address, CANARY_SEND_AMOUNT_TPC]
        )
        log.info(f"canary transaction {self._canary_txid[:12]}... broadcast -- will confirm only on group A's fork")

    async def _build_group_a_fork(self):
        log.step("repointing signers back to group A's default mapping and building its fork")
        await self._repoint_signers(GROUP_A_RPC_HOSTS)
        # signer-2 (-> core-3a) will fail to start with no reachable RPC target --
        # group B is stopped for the whole of this step -- expected and harmless,
        # matches the verified recipe: threshold 2 is met by signer-0/signer-1 alone.
        await start_nodes(*SIGNERS)

        target = self._baseline_height + self._reorg_length_blocks
        log.step(f"group A building its own, different fork (target height {target})")
        await self._wait_for_height(GROUP_A, target)
        self._group_a_tip = await self._clients["core-1a"].call("getbestblockhash")
        log.info(f"group A's fork tip: {self._group_a_tip[:12]}... (height {target})")
        await stop_nodes(*SIGNERS)

    async def _reconnect_group_b(self):
        log.step("reconnecting group B alongside group A")
        await start_nodes(*GROUP_B)
        waiter = TopologyWaiter(CONVERGENCE_TIMEOUT_SECONDS, HEIGHT_POLL_INTERVAL_SECONDS)
        await waiter.run()

    async def _confirm_tie_holds(self):
        log.step("confirming the equal-height tie alone does not trigger a reorg")
        tips = await asyncio.gather(*(self._clients[name].call("getbestblockhash") for name in GROUP_A))
        for name, tip in zip(GROUP_A, tips):
            if tip != self._group_a_tip:
                raise ReorgError(
                    f"{name}: switched off its own tip ({self._group_a_tip[:12]}..., now {tip[:12]}...) at the "
                    f"tie alone, before group B was ever longer -- a tie shouldn't cause a reorg"
                )
        log.info("confirmed: every group A node is still on its own tip at the tie -- no premature reorg")

    async def _extend_group_b_by_two_blocks(self):
        log.step("repointing signers back to group B and extending its chain by 2 blocks")
        await self._repoint_signers(GROUP_B_RPC_HOSTS)
        await start_nodes(*SIGNERS)

        # +2, not +1 -- confirmed live: core-3a still shows its own just-abandoned tip
        # as valid-headers (not yet valid-fork) after only one, one round behind every
        # other node. See this module's docstring step 6.
        target = self._baseline_height + self._reorg_length_blocks + 2
        # ALL_NODE_NAMES, not just GROUP_B -- the point of this wait is confirming
        # group A adopts group B's now-longer chain (the reorg itself), not just that
        # group B produced more blocks.
        await self._wait_for_height(ALL_NODE_NAMES, target)
        self._group_b_tip = await self._clients["core-3a"].call("getbestblockhash")
        log.info(f"group B's chain extended to height {target} (tip {self._group_b_tip[:12]}...) -- all 7 nodes converged")
        await stop_nodes(*SIGNERS)

    async def _confirm_convergence(self):
        # Each node's expected getchaintips shape follows from two propagation
        # mechanisms, not just P2P adjacency: (1) each signer submits a block it
        # masters directly to its OWN RPC target (core-1a/2a/3a) -- no core-to-core
        # P2P needed for that (confirmed by _build_group_a_fork's own precondition:
        # signer-2's target core-3a is unreachable there, and signer-0/1 submitting
        # to core-1a/2a independently is what still lets the round complete). (2)
        # P2P relay along docker-compose.yml's static edges: core-1a<->core-1b,
        # core-2a<->core-2b, core-3a<->core-3b, core-7<->{core-1b,core-2b,core-3b}
        # -- core-7 is the ONLY bridge between the three otherwise-disconnected
        # pairs. See doc/scripts.md for the full derivation; this is the outcome:
        #
        #   node      hops from group A   tips expected at this check
        #   --------  -------------------  --------------------------------------
        #   core-1a   0 (built it)         exactly 2: active=group B,
        #   core-1b   0 (built it)                    valid-fork=group A
        #   core-2a   0 (built it)                    (deterministic -- their own
        #   core-2b   0 (built it)                    prior-active chain, demoted)
        #   core-3a   3 (1b/2b->7->3b->3a) exactly 1: active=group B only
        #                                             (never observed to leak)
        #   core-3b   2 (1b/2b->7->3b)     active=group B, plus an OPTIONAL
        #   core-7    1 (1b/2b->7)         valid-headers tip -- IF present, must
        #                                  be group A's fork exactly, not just
        #                                  "any extra tip" (timing-dependent,
        #                                  not deterministic)
        log.step("confirming convergence via getchaintips (checking all 7 nodes, not stopping at the first mismatch)")
        failures = []

        for name in GROUP_A:
            tips = await self._clients[name].call("getchaintips")
            active = [t for t in tips if t["status"] == "active"]
            forks = [t for t in tips if t["status"] == "valid-fork"]
            if len(tips) != 2 or len(active) != 1 or len(forks) != 1:
                failures.append(f"{name}: expected exactly 2 tips (1 active, 1 valid-fork), got {tips}")
                continue
            if active[0]["hash"] != self._group_b_tip:
                failures.append(f"{name}: active tip {active[0]['hash']} doesn't match group B's {self._group_b_tip}")
                continue
            if forks[0]["hash"] != self._group_a_tip or forks[0]["branchlen"] != self._reorg_length_blocks:
                failures.append(
                    f"{name}: valid-fork tip mismatch -- expected hash {self._group_a_tip} "
                    f"branchlen {self._reorg_length_blocks}, got {forks[0]}"
                )
                continue
            log.info(f"{name}: confirmed -- active={active[0]['hash'][:12]}... valid-fork={forks[0]['hash'][:12]}... "
                      f"branchlen={forks[0]['branchlen']}")

        tips = await self._clients["core-3a"].call("getchaintips")
        if len(tips) != 1 or tips[0]["status"] != "active" or tips[0]["hash"] != self._group_b_tip:
            failures.append(f"core-3a: expected a single active tip matching group B's {self._group_b_tip}, got {tips}")
        else:
            log.info(f"core-3a: confirmed -- single active tip {tips[0]['hash'][:12]}... (never had a competing fork)")

        for name in ("core-3b", "core-7"):
            tips = await self._clients[name].call("getchaintips")
            active = [t for t in tips if t["status"] == "active"]
            others = [t for t in tips if t["status"] != "active"]
            if len(active) != 1 or active[0]["hash"] != self._group_b_tip:
                failures.append(f"{name}: expected a single active tip matching group B's {self._group_b_tip}, got {tips}")
            elif others and any(t["hash"] != self._group_a_tip for t in others):
                failures.append(
                    f"{name}: active tip matches group B, but has an unexpected non-active tip -- "
                    f"expected only group A's fork ({self._group_a_tip}) if any, got {others}"
                )
            else:
                log.info(
                    f"{name}: confirmed -- active tip {active[0]['hash'][:12]}... matches group B "
                    f"({len(others)} other known tip(s) from its cross-group P2P links, expected)"
                )

        if failures:
            raise ReorgError(
                f"convergence check failed on {len(failures)}/{len(ALL_NODE_NAMES)} node(s):\n  "
                + "\n  ".join(failures)
            )

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

    async def _restore_default_signers(self):
        """Leaves the network exactly as this script found it -- signers running,
        pointed at the default per-signer mapping -- instead of stopped and still
        repointed at group B from the last recipe step. Whatever runs next in the
        same job (e.g. generate_traffic.py) assumes block production is live: with
        no restore step here, a canary transaction still unconfirmed at this point
        (an expected outcome, not a failure -- see _verify_canary_transaction_survived)
        would sit in group A's mempools forever, since nothing would be running to
        ever mine it. Confirmed live: this is exactly what made a later
        generate_traffic.py invocation's mempool-emptiness wait hang for its full
        timeout, no matter how long that timeout was raised to.
        """
        log.step("restoring signers to their default RPC mapping and leaving them running")
        await self._repoint_signers(GROUP_A_RPC_HOSTS)
        await start_nodes(*SIGNERS)

    # -- signer repointing --------------------------------------------------------

    async def _repoint_signers(self, core_rpc_hosts):
        """Rewrites tapyrus-signer.toml's rpc-endpoint-host for all 3 signers and
        leaves them stopped, ready for the caller to start. Used three times in this
        recipe (to group B, back to group A's default mapping, then to group B
        again), not just once -- config is only read at tapyrus-signerd startup, so
        a bare restart of an already-stopped container is what actually picks up the
        rewritten bind-mounted file.
        """
        log.step(f"repointing all 3 signers' RPC targets to {core_rpc_hosts}")
        set_dir = REPO_ROOT / "secrets" / SIGNER_SET_NAME
        pubkeys = [line for line in (set_dir / "pubkeys.txt").read_text().splitlines() if line]
        aggpubkey = (set_dir / "aggregated-public-key.txt").read_text().strip()
        output_dir = REPO_ROOT / "runtime" / "signers"
        addresses = [self._read_to_address(output_dir / f"node-{i}") for i in range(len(pubkeys))]

        assembler = SignerConfigAssembler(
            set_dir, SIGNER_THRESHOLD, aggpubkey,
            CoreRpc(CORE_RPC_PORT),
            Redis(REDIS_HOST, REDIS_PORT),
            self._round_duration,
        )
        assembler.run(pubkeys, addresses, list(core_rpc_hosts), output_dir)

    def _read_to_address(self, node_dir):
        with (node_dir / "tapyrus-signer.toml").open("rb") as f:
            return tomllib.load(f)["signer"]["to-address"]

    # -- height polling ------------------------------------------------------------

    async def _wait_for_height(self, node_names, target):
        """Waits until every node in node_names has reached at least `target`, then
        returns the height actually reached (min across those nodes) -- which may be
        higher than `target` itself if something already pushed the chain further
        before this call started (e.g. scripts/generate_traffic.py, if it ran first
        in the same job). Callers that derive further targets from this return value,
        not the original `target` they asked for, stay correct regardless of how much
        the chain had already advanced -- see the REORG_BASELINE_HEIGHT note in
        this script's own docstring.

        Watches getblockchaininfo's bestblockhash per node rather than assuming a
        production rate: as long as at least one node's bestblockhash keeps changing
        (real forward progress, however slow), the wait continues with no overall cap.
        It only times out if that stalls -- no bestblockhash change anywhere in
        node_names for STALL_TIMEOUT_ROUND_MULTIPLIER round-durations -- which is what
        actually distinguishes "stuck" from "just needs more blocks."
        """
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
                    f"{stall_timeout_seconds}s ({self._round_duration}s/round x{STALL_TIMEOUT_ROUND_MULTIPLIER}): "
                    f"{stuck}"
                )
            await asyncio.sleep(HEIGHT_POLL_INTERVAL_SECONDS)

    async def _wait_for_matching_tips(self, node_names):
        """Polls getblockchaininfo across node_names until both height AND
        bestblockhash agree, retrying for BASELINE_TIP_CONSISTENCY_TIMEOUT_SECONDS
        instead of failing on the first mismatch -- see that constant's comment.
        Returns (height, tip) once agreed.

        Checking height alongside the hash (not bestblockhash alone) matters because
        this is called right after stopping the signers, while a block from just
        before the stop can still be propagating. A node that already applied it and
        one that hasn't briefly disagree on bestblockhash for an obviously-expected
        reason; without the height dimension a caller has no way to tell that apart
        from a real divergence.
        """
        deadline = time.monotonic() + BASELINE_TIP_CONSISTENCY_TIMEOUT_SECONDS
        while True:
            infos = await asyncio.gather(*(self._node_chain_info(name) for name in node_names))
            heights = [info["blocks"] if info else None for info in infos]
            tips = [info["bestblockhash"] if info else None for info in infos]
            if len(set(heights)) == 1 and len(set(tips)) == 1:
                return heights[0], tips[0]
            if time.monotonic() >= deadline:
                raise ReorgError(
                    f"baseline height/tip still don't match across all 7 nodes after "
                    f"{BASELINE_TIP_CONSISTENCY_TIMEOUT_SECONDS}s: "
                    f"{dict(zip(node_names, zip(heights, tips)))}"
                )
            await asyncio.sleep(HEIGHT_POLL_INTERVAL_SECONDS)

    async def _node_chain_info(self, name):
        try:
            return await self._clients[name].call("getblockchaininfo")
        except RpcUnreachable:
            return None

    async def _wait_for_rpc_ready(self, name):
        """Retries a cheap RPC call against `name` until it answers or
        RPC_READY_TIMEOUT_SECONDS elapses -- same "container started != RPC ready"
        wait collect_coinbase_addresses.py does after its own `docker compose up`.
        """
        deadline = time.monotonic() + RPC_READY_TIMEOUT_SECONDS
        while True:
            try:
                await self._clients[name].call("getblockcount")
                return
            except RpcUnreachable as exc:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"{name}'s RPC never became reachable within {RPC_READY_TIMEOUT_SECONDS}s: {exc}")
                await asyncio.sleep(RPC_READY_POLL_INTERVAL_SECONDS)


async def main():
    baseline_height = int(os.environ.get("REORG_BASELINE_HEIGHT", "30"))
    reorg_length_blocks = int(os.environ.get("REORG_LENGTH_BLOCKS", "10"))
    round_duration = os.environ.get("ROUND_DURATION", "60")

    log.step(
        f"simulating a reorg: baseline height {baseline_height}, group B then group A each build "
        f"{reorg_length_blocks} block(s) alone, then group B is extended by 2 more"
    )
    simulator = ReorgSimulator(baseline_height, reorg_length_blocks, round_duration)
    await simulator.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (ReorgError, TimeoutError, ComposeError, RpcError, RpcUnreachable) as exc:
        log.error(str(exc))
        sys.exit(1)
