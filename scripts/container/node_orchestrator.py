#!/usr/bin/env python3
"""Runs inside a core-* container (see entrypoint_wrapper.sh), supervising tapyrusd
directly rather than being it -- launches it as a child process, then, for core-1b/
2b/3b/core-7 only, randomly stops/restarts/reindexes/invalidates it via real RPC
calls for as long as the container runs, proving the network can absorb real node
churn and still converge, not just run a clean, scripted happy path. core-1a/2a/3a
(the 3 signers' own RPC targets) get crash-recovery supervision like every other
node but never a deliberate chaos action -- live testing found that even one of them
briefly catching up from a chaos-triggered restart could make it miss its turn in
tapyrus-signer's round-robin master selection, throwing off generate_traffic.py's
coinbase-rotation tracking; excluding them at the source is simpler than trying to
make every downstream consumer of that rotation tolerate it. See doc/work-done.md
for the full design, including:

- why every action checks a shared pause file first (avoids corrupting
  simulate_reorg.py's/simulate_federation_change.py's own precise node up/down
  assumptions during their sensitive windows);
- why the reward/restart-flavor assignment is static per node (round-robin over
  -reindex/-reindex-chainstate/-reloadxfield), not re-randomized per action;
- why a restarted node stays down until the rest of the network has produced a
  few more real blocks (polled from another node), not just however long
  tapyrusd itself takes to stop and relaunch -- restarting at the very next
  block wouldn't give other peers any real chance to notice and drop the
  now-stale connection first;
- why chaos waits out a startup grace period before its first action -- gives
  the mesh formed right after bring-up one chance to converge before anything
  perturbs it, rather than fighting wait_for_topology.py's own purpose.

Usage (via entrypoint_wrapper.sh, not run directly):
    node_orchestrator.py --node-name=core-1a --restart-flavor=reindex -- tapyrusd <args...>

Reads CORE_RPC_USER / CORE_RPC_PASS / PRNG_SEED_BASE from the environment -- same
job-level env vars the rest of this repo's scripts already use. PRNG_SEED_BASE is
otherwise unconsumed anywhere else in this repo (see doc/work-done.md) -- this is its
first real use, seeded per node so all 7 don't act in lockstep, but still
deterministic/reproducible for a given run.
"""
import asyncio
import os
import random
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, "/app")

from scripts.lib.log import log  # noqa: E402
from scripts.lib.rpc import CoreRpcClient, RpcError, RpcUnreachable  # noqa: E402

RPC_HOST = "127.0.0.1"
RPC_PORT = 12381  # container-internal port -- same for every core-* service

ALL_NODES = ("core-1a", "core-1b", "core-2a", "core-2b", "core-3a", "core-3b", "core-7")

# The 3 signers' own RPC targets never join the continuous chaos loop (see module
# docstring) -- only core-1b/2b/3b/core-7 do.
CHAOS_NODES = ("core-1b", "core-2b", "core-3b", "core-7")

RESTART_FLAVOR_FLAGS = {
    "reindex": "-reindex",
    "reindex-chainstate": "-reindex-chainstate",
    "reloadxfield": "-reloadxfield",
}

PAUSE_FILE = Path("/orchestrator-control/pause")

RPC_READY_TIMEOUT_SECONDS = 120
RPC_READY_POLL_INTERVAL_SECONDS = 3
PROCESS_EXIT_TIMEOUT_SECONDS = 60
CRASH_POLL_INTERVAL_SECONDS = 5
PAUSE_POLL_INTERVAL_SECONDS = 10
MIN_ACTION_INTERVAL_SECONDS = 30
MAX_ACTION_INTERVAL_SECONDS = 180
# Before the very first action: with 7 nodes each independently churning every
# 30-180s, some node is essentially always mid-restart, which fights
# wait_for_topology.py's very purpose -- confirming the mesh formed correctly
# right after bring-up, before any signer/traffic exists. Chaos still runs
# continuously for the rest of the workflow per the user's design; this only
# delays its first action, past wait_for_topology.py's own 300s budget.
STARTUP_GRACE_SECONDS = 360
INVALIDATE_RECONSIDER_MIN_DELAY_SECONDS = 5
INVALIDATE_RECONSIDER_MAX_DELAY_SECONDS = 30
# _wait_out_pause() is a shared gate across every chaos node -- while a host-side
# script holds the pause file, any node whose own randomized timer expires during
# that window queues up at the gate instead of firing on its own schedule. All
# queued nodes then release in the same instant the file disappears, regardless of
# how spread out their original timers were -- confirmed live: 3 nodes independently
# invalidated the exact same shared tip in the exact same second this way. This
# jitter destaggers a shared release without changing each node's own long-run
# action frequency.
POST_PAUSE_JITTER_MAX_SECONDS = 15

# How long a stopped node stays down before a restart brings it back -- measured
# in real blocks produced by the rest of the network (polled from another node,
# not a timer), not just "however long tapyrusd takes to stop and relaunch".
# Restarting at the very next block wouldn't give other peers any real chance to
# notice and drop the now-stale connection first.
DOWNTIME_MIN_BLOCKS = 2
DOWNTIME_MAX_BLOCKS = 4
DOWNTIME_POLL_INTERVAL_SECONDS = 5
# Safety net only -- if the rest of the network stalls entirely (e.g. no signers
# running yet) rather than wait forever, log it and restart anyway. Kept well
# under wait_for_topology.py's own 300s convergence budget: that check runs
# before signers/traffic exist, so the block-count condition above can never be
# satisfied during it and every downtime falls through to this timeout.
DOWNTIME_TIMEOUT_SECONDS = 90


class NodeOrchestrator:
    def __init__(self, node_name, restart_flavor, tapyrusd_args, rpc_user, rpc_pass, rng):
        self._node_name = node_name
        self._restart_flavor_flag = RESTART_FLAVOR_FLAGS[restart_flavor]
        self._tapyrusd_args = tapyrusd_args
        self._rpc_user = rpc_user
        self._rpc_pass = rpc_pass
        self._rpc = CoreRpcClient(RPC_HOST, RPC_PORT, rpc_user, rpc_pass)
        self._rng = rng
        self._process = None
        # Guards _process itself -- both deliberate restarts and the crash
        # supervisor replace it, and must never do so at the same time.
        self._lock = asyncio.Lock()
        # asyncio.create_task() keeps no strong reference of its own to the
        # returned Task -- an unreferenced one can be garbage-collected before it
        # finishes (documented asyncio gotcha). These two fields exist purely to
        # hold that reference for as long as the orchestrator runs.
        self._background_tasks = set()

    async def run(self):
        await self._launch()
        self._track_background_task(asyncio.create_task(self._supervise_crashes()))
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(
                sig, lambda: self._track_background_task(asyncio.create_task(self._shutdown()))
            )
        if self._node_name not in CHAOS_NODES:
            # Crash recovery (_supervise_crashes, already started above) still
            # applies -- only the deliberate chaos loop is skipped, see module
            # docstring. Blocks forever rather than returning, so run() (and the
            # process) stays alive for the signal handlers registered above.
            log.step(f"{self._node_name}: not a chaos node -- staying up under crash supervision only")
            await asyncio.Event().wait()
        await self._action_loop()

    def _track_background_task(self, task):
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    # -- process lifecycle -------------------------------------------------------

    async def _launch(self, extra_args=None):
        args = list(self._tapyrusd_args) + (list(extra_args) if extra_args else [])
        log.step(f"{self._node_name}: launching tapyrusd ({' '.join(args[1:])})")
        self._process = await asyncio.create_subprocess_exec(*args)
        await self._wait_for_rpc_ready()

    async def _wait_for_rpc_ready(self):
        deadline = time.monotonic() + RPC_READY_TIMEOUT_SECONDS
        while True:
            try:
                await self._rpc.call("getblockcount")
                return
            except RpcUnreachable as exc:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"{self._node_name}: RPC never became reachable within "
                        f"{RPC_READY_TIMEOUT_SECONDS}s: {exc}"
                    )
                await asyncio.sleep(RPC_READY_POLL_INTERVAL_SECONDS)

    async def _restart(self, extra_args=None, reason=""):
        async with self._lock:
            log.step(f"{self._node_name}: stopping ({reason})")
            try:
                await self._rpc.call("stop")
            except (RpcError, RpcUnreachable) as exc:
                log.warn(f"{self._node_name}: stop RPC failed ({exc}), killing process directly")
                self._process.kill()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=PROCESS_EXIT_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                log.warn(f"{self._node_name}: didn't exit within {PROCESS_EXIT_TIMEOUT_SECONDS}s, killing")
                self._process.kill()
                await self._process.wait()
            # Deliberately held across this wait, not just the stop/exit above --
            # otherwise _supervise_crashes would see this same still-stopped
            # process, think it crashed on its own, and relaunch it immediately,
            # defeating the whole point of staying down for a while.
            await self._wait_out_downtime()
            log.step(f"{self._node_name}: restarting ({reason})")
            await self._launch(extra_args)

    async def _peer_height(self):
        """Real block height from any other reachable node -- this node's own RPC
        is down for the whole span this is used in, so it can't ask itself."""
        for other in ALL_NODES:
            if other == self._node_name:
                continue
            client = CoreRpcClient(other, RPC_PORT, self._rpc_user, self._rpc_pass)
            try:
                return await client.call("getblockcount")
            except (RpcError, RpcUnreachable):
                continue
        return None

    async def _wait_out_downtime(self):
        target_blocks = self._rng.randint(DOWNTIME_MIN_BLOCKS, DOWNTIME_MAX_BLOCKS)
        baseline = await self._peer_height()
        if baseline is None:
            log.warn(
                f"{self._node_name}: no other node reachable to measure real downtime "
                f"against -- falling back to a flat {DOWNTIME_TIMEOUT_SECONDS}s"
            )
            await asyncio.sleep(DOWNTIME_TIMEOUT_SECONDS)
            return
        log.step(
            f"{self._node_name}: staying down until the network produces "
            f"{target_blocks} more block(s) (currently {baseline})"
        )
        deadline = time.monotonic() + DOWNTIME_TIMEOUT_SECONDS
        while True:
            await asyncio.sleep(DOWNTIME_POLL_INTERVAL_SECONDS)
            height = await self._peer_height()
            if height is not None and height >= baseline + target_blocks:
                log.info(f"{self._node_name}: network reached height {height} -- restarting now")
                return
            if time.monotonic() >= deadline:
                log.warn(
                    f"{self._node_name}: network didn't produce {target_blocks} block(s) within "
                    f"{DOWNTIME_TIMEOUT_SECONDS}s (stuck at {height}) -- restarting anyway"
                )
                return

    async def _supervise_crashes(self):
        # Polls returncode rather than awaiting process.wait() directly, so this
        # never has two coroutines racing on the same Process object's completion
        # -- the lock + identity check below is what actually decides whether a
        # given exit was already handled by a deliberate _restart() call.
        while True:
            process = self._process
            while process.returncode is None:
                await asyncio.sleep(CRASH_POLL_INTERVAL_SECONDS)
            async with self._lock:
                if process is not self._process:
                    # A deliberate _restart() already replaced it while this loop
                    # was polling -- that exit is already handled.
                    continue
                log.warn(
                    f"{self._node_name}: tapyrusd exited unexpectedly "
                    f"(code {process.returncode}) -- relaunching plain"
                )
                await self._launch()

    async def _shutdown(self):
        # This runs as its own Task (the signal handler creates it), not awaited
        # by run()'s own coroutine -- sys.exit() here would only end this one
        # Task, leaving _action_loop() running forever with nothing left to stop
        # it. os._exit() terminates the whole process directly instead, safe
        # because the meaningful cleanup (stopping tapyrusd cleanly via RPC,
        # not SIGKILLing it) already happened above.
        log.step(f"{self._node_name}: shutting down")
        async with self._lock:
            if self._process and self._process.returncode is None:
                try:
                    await asyncio.wait_for(self._rpc.call("stop"), timeout=10)
                except (RpcError, RpcUnreachable, asyncio.TimeoutError):
                    self._process.terminate()
                try:
                    await asyncio.wait_for(self._process.wait(), timeout=PROCESS_EXIT_TIMEOUT_SECONDS)
                except asyncio.TimeoutError:
                    self._process.kill()
        os._exit(0)

    # -- chaos actions ------------------------------------------------------------

    async def _action_loop(self):
        log.step(f"{self._node_name}: grace period ({STARTUP_GRACE_SECONDS}s) before chaos begins")
        await asyncio.sleep(STARTUP_GRACE_SECONDS)
        while True:
            for action in self._shuffled_cycle():
                await self._wait_out_pause()
                try:
                    await action()
                except Exception as exc:  # noqa: BLE001 -- one bad action must not kill the loop
                    log.error(f"{self._node_name}: action {action.__name__} failed unexpectedly: {exc}")
                await asyncio.sleep(self._rng.uniform(MIN_ACTION_INTERVAL_SECONDS, MAX_ACTION_INTERVAL_SECONDS))

    def _shuffled_cycle(self):
        # Every node does at least one of each per cycle, in random order --
        # reshuffled and repeated for as long as the container runs.
        actions = [self._plain_restart, self._flavored_restart, self._invalidate_and_reconsider]
        self._rng.shuffle(actions)
        return actions

    async def _wait_out_pause(self):
        waited = False
        while PAUSE_FILE.exists():
            waited = True
            await asyncio.sleep(PAUSE_POLL_INTERVAL_SECONDS)
        if waited:
            # Only jitter if this node actually queued at the gate -- the common
            # case (pause file absent) shouldn't pay this delay on every single
            # action. See POST_PAUSE_JITTER_MAX_SECONDS.
            await asyncio.sleep(self._rng.uniform(0, POST_PAUSE_JITTER_MAX_SECONDS))

    async def _plain_restart(self):
        await self._restart(reason="plain")

    async def _flavored_restart(self):
        await self._restart(extra_args=[self._restart_flavor_flag], reason=self._restart_flavor_flag)

    async def _invalidate_and_reconsider(self):
        try:
            block_hash = await self._rpc.call("getbestblockhash")
            await self._rpc.call("invalidateblock", [block_hash])
        except (RpcError, RpcUnreachable) as exc:
            # Includes the documented, expected case: tapyrus-core refuses to
            # invalidate a block at or before the last xfield-change height
            # (blockchain.cpp) -- a normal skip, not a failure.
            log.warn(f"{self._node_name}: invalidateblock skipped ({exc})")
            return
        log.step(f"{self._node_name}: invalidated {block_hash}")
        await asyncio.sleep(self._rng.uniform(
            INVALIDATE_RECONSIDER_MIN_DELAY_SECONDS, INVALIDATE_RECONSIDER_MAX_DELAY_SECONDS
        ))
        try:
            await self._rpc.call("reconsiderblock", [block_hash])
            log.step(f"{self._node_name}: reconsidered {block_hash}")
        except (RpcError, RpcUnreachable) as exc:
            log.warn(f"{self._node_name}: reconsiderblock failed ({exc})")


def parse_args(argv):
    if "--" in argv:
        idx = argv.index("--")
        own_args, tapyrusd_args = argv[:idx], argv[idx + 1:]
    else:
        own_args, tapyrusd_args = argv, []

    node_name = None
    restart_flavor = None
    for arg in own_args:
        if arg.startswith("--node-name="):
            node_name = arg.split("=", 1)[1]
        elif arg.startswith("--restart-flavor="):
            restart_flavor = arg.split("=", 1)[1]
    if not node_name or restart_flavor not in RESTART_FLAVOR_FLAGS:
        sys.exit(
            "usage: node_orchestrator.py --node-name=<name> "
            f"--restart-flavor=<{'|'.join(RESTART_FLAVOR_FLAGS)}> -- tapyrusd <args...>"
        )
    if not tapyrusd_args:
        sys.exit("usage: no tapyrusd command given after '--'")
    return node_name, restart_flavor, tapyrusd_args


def main():
    node_name, restart_flavor, tapyrusd_args = parse_args(sys.argv[1:])

    rpc_user = os.environ.get("CORE_RPC_USER", "rpcuser")
    rpc_pass = os.environ.get("CORE_RPC_PASS", "rpcpassword")
    seed_base = os.environ.get("PRNG_SEED_BASE", "0")
    rng = random.Random(f"{seed_base}:{node_name}")

    log.step(f"{node_name}: node orchestrator starting (restart flavor: {restart_flavor})")
    orchestrator = NodeOrchestrator(node_name, restart_flavor, tapyrusd_args, rpc_user, rpc_pass, rng)
    asyncio.run(orchestrator.run())


if __name__ == "__main__":
    main()
