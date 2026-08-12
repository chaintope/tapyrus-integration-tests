#!/usr/bin/env python3
"""Switches the 7 core-* nodes verify_seeder.py's connect-mode phase left running
into "connect + orchestrator" mode: tears them down and recreates them with
NODE_ORCHESTRATOR set, so entrypoint_wrapper.sh hands off to
scripts/container/node_orchestrator.py instead of running tapyrusd directly. From
this point on, every core-* node randomly stops/restarts/reindexes/invalidates
itself for as long as the container runs -- see doc/work-done.md for the full design.

This script only brings the chaos-supervised nodes up and confirms their RPC is
reachable -- it does NOT wait for chaos or assert recovery itself. The rest of the
workflow's own existing checks (wait_for_topology.py, every generate_traffic.py
settle-height assertion, simulate_reorg.py's getchaintips checks) are what actually
prove the network keeps converging under chaos, same as they already do today.

Usage:
    ./scripts/start_node_orchestrator.py

Requires redis and the 7 core-* nodes already up in connect mode (verify_seeder.py's
own phase 2 precondition) -- this script's teardown+recreate only touches the core-*
nodes, not redis.
"""
import asyncio
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.lib.compose import ComposeError, bring_up, compose  # noqa: E402
from scripts.lib.log import log  # noqa: E402
from scripts.lib.rpc import CoreRpcClient, RpcError, RpcUnreachable  # noqa: E402
from scripts.verify_seeder import CONNECT_MODE_ARGS, CORE_NODES, CORE_RPC_PORTS, _args_env_var  # noqa: E402

RPC_HOST = "127.0.0.1"
RPC_READY_TIMEOUT_SECONDS = 120
RPC_READY_POLL_INTERVAL_SECONDS = 3


class NodeOrchestratorStartupError(Exception):
    """A core-* node's RPC never came back up after switching to orchestrator mode."""


async def _wait_for_rpc_ready(client, node_name):
    deadline = time.monotonic() + RPC_READY_TIMEOUT_SECONDS
    while True:
        try:
            await client.call("getblockcount")
            return
        except RpcUnreachable as exc:
            if time.monotonic() >= deadline:
                raise NodeOrchestratorStartupError(
                    f"{node_name}: RPC never became reachable within {RPC_READY_TIMEOUT_SECONDS}s: {exc}"
                )
            await asyncio.sleep(RPC_READY_POLL_INTERVAL_SECONDS)


async def main():
    log.step("switching the 7 core-* nodes to connect + orchestrator mode")
    # Full teardown + recreate, not just a restart: NODE_ORCHESTRATOR/
    # NODE_ORCHESTRATOR_FLAVOR only take effect at container creation (compose
    # env var substitution), same as CORE_<NAME>_ARGS itself.
    await compose("stop", *CORE_NODES)
    await compose("rm", "-f", *CORE_NODES)

    # Every recreate needs this set again -- entrypoint.sh writes it to a fresh
    # container's own datadir itself; without it tapyrusd has no genesis to load
    # at all and crashes immediately ("bad-features, Incorrect Block features").
    os.environ["GENESIS_BLOCK_WITH_SIG"] = (REPO_ROOT / "secrets" / "signer-set-a" / "genesis.hex").read_text().strip()
    os.environ["NODE_ORCHESTRATOR"] = "1"
    for node, args in CONNECT_MODE_ARGS.items():
        os.environ[_args_env_var(node)] = args

    await bring_up(*CORE_NODES)

    log.step("waiting for all 7 nodes' RPC to come back up under the orchestrator")
    clients = {name: CoreRpcClient(RPC_HOST, CORE_RPC_PORTS[name], name) for name in CORE_NODES}
    await asyncio.gather(*(_wait_for_rpc_ready(client, name) for name, client in clients.items()))
    _persist_env_for_rest_of_job()
    log.info("done. all 7 core-* nodes are now running under scripts/container/node_orchestrator.py")


def _persist_env_for_rest_of_job():
    # NODE_ORCHESTRATOR only lives in this process's own os.environ otherwise --
    # signer-0/1/2 and signer-set-b's services all `depends_on` a core-1a/2a/3a
    # node, so any later `docker compose up` touching them (Bring up signers,
    # Federation change), from a fresh process that never re-sets it, would
    # resolve NODE_ORCHESTRATOR back to unset and Compose would silently recreate
    # that core node to match, reverting it to plain tapyrusd. Self-contained here
    # rather than left to the workflow step's own `echo >> $GITHUB_ENV`, for the
    # same reason verify_seeder.py's own env persistence is self-contained: this
    # script running in any other context (a different workflow, standalone,
    # local testing) shouldn't silently reintroduce the same drift. See
    # doc/work-done.md.
    github_env = os.environ.get("GITHUB_ENV")
    if not github_env:
        return  # not running under GitHub Actions -- nothing to persist to
    with open(github_env, "a") as f:
        f.write("NODE_ORCHESTRATOR=1\n")
    log.info("persisted NODE_ORCHESTRATOR to $GITHUB_ENV for the rest of the job")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (NodeOrchestratorStartupError, ComposeError, RpcError, RpcUnreachable) as exc:
        log.error(str(exc))
        sys.exit(1)
