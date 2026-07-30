#!/usr/bin/env python3
"""Poll every core-* node's RPC port until the 7-node topology (plan doc section 4b)
has fully converged -- each node's own peer count matches the pattern the -connect
graph is supposed to produce, not just "all 7 containers are up".

Usage:
    ./scripts/wait_for_topology.py [--timeout-seconds N] [--poll-interval-seconds N]

Expected connection counts (doc/weekly-integration-test-plan.md section 4b, verified
live in doc/work-done.md via getpeerinfo): each first-layer node (-a) has exactly its
-b sibling (1), each second-layer node (-b) has its -a parent plus core-7 (2), and
core-7 has all three -b nodes (3) -- the "1/2/1/2/1/2/3" pattern in the root
README.md's variable table. This mapping is not configurable per-run (see README.md:
the 7-node topology is wired 1:1 to exactly 3 signers), so it's hardcoded here rather
than taking node/port arguments.

RPC host/port: host ports are docker-compose's published host-side ports (see
docker/docker-compose.yml), reachable from the runner the same way the "Collect
coinbase addresses" workflow step already reaches core-1a/2a/3a. CORE_RPC_USER /
CORE_RPC_PASS env vars match the workflow's existing job-level env of the same names.
"""
import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.lib.log import log  # noqa: E402
from scripts.lib.rpc import CoreRpcClient, RpcUnreachable  # noqa: E402

RPC_HOST = "127.0.0.1"
DEFAULT_TIMEOUT_SECONDS = 300
DEFAULT_POLL_INTERVAL_SECONDS = 5

# (node name, host-published RPC port, expected getconnectioncount) -- see
# docker/docker-compose.yml's port mappings, which this hardcodes to.
NODES = (
    ("core-1a", 12381, 1),
    ("core-1b", 12382, 2),
    ("core-2a", 12383, 1),
    ("core-2b", 12384, 2),
    ("core-3a", 12385, 1),
    ("core-3b", 12386, 2),
    ("core-7", 12387, 3),
)


class TopologyWaiter:
    """Polls every node in NODES' getconnectioncount until all match their expected
    value, or gives up after timeout_seconds.
    """

    def __init__(self, rpc_user, rpc_pass, timeout_seconds, poll_interval_seconds):
        self._rpc_user = rpc_user
        self._rpc_pass = rpc_pass
        self._timeout_seconds = timeout_seconds
        self._poll_interval_seconds = poll_interval_seconds

    async def run(self):
        deadline = time.monotonic() + self._timeout_seconds
        attempt = 0
        while True:
            attempt += 1
            mismatches = await self._check_once(attempt)
            if not mismatches:
                log.info(f"topology converged after {attempt} attempt(s)")
                return
            if time.monotonic() >= deadline:
                for name, detail in mismatches:
                    log.error(f"{name}: {detail}")
                raise TimeoutError(
                    f"topology did not converge within {self._timeout_seconds}s "
                    f"({len(mismatches)}/{len(NODES)} node(s) still mismatched)"
                )
            await asyncio.sleep(self._poll_interval_seconds)

    async def _check_once(self, attempt):
        # All 7 RPC calls are dispatched at once rather than one at a time -- this
        # attempt still waits for all 7 responses (the slowest one bounds it), but
        # doesn't pay for their latency added up.
        results = await asyncio.gather(*(self._check_node(*node) for node in NODES))
        mismatches = [result for result in results if result is not None]

        converged = len(NODES) - len(mismatches)
        log.info(f"attempt {attempt}: {converged}/{len(NODES)} node(s) converged")
        return mismatches

    async def _check_node(self, name, port, expected):
        client = CoreRpcClient(RPC_HOST, port, self._rpc_user, self._rpc_pass)
        try:
            actual = await client.call("getconnectioncount")
        except RpcUnreachable as exc:
            return (name, f"not reachable yet ({exc})")
        if actual != expected:
            return (name, f"expected {expected} connection(s), got {actual}")
        return None


def parse_args():
    parser = argparse.ArgumentParser(description="Wait for the 7-node topology to converge.")
    parser.add_argument(
        "--timeout-seconds", type=int, default=DEFAULT_TIMEOUT_SECONDS,
        help=f"give up after this many seconds (default: {DEFAULT_TIMEOUT_SECONDS})",
    )
    parser.add_argument(
        "--poll-interval-seconds", type=int, default=DEFAULT_POLL_INTERVAL_SECONDS,
        help=f"seconds between polling attempts (default: {DEFAULT_POLL_INTERVAL_SECONDS})",
    )
    return parser.parse_args()


async def main():
    args = parse_args()
    rpc_user = os.environ.get("CORE_RPC_USER", "rpcuser")
    rpc_pass = os.environ.get("CORE_RPC_PASS", "rpcpassword")

    log.step(f"waiting for topology convergence (timeout {args.timeout_seconds}s)")
    waiter = TopologyWaiter(rpc_user, rpc_pass, args.timeout_seconds, args.poll_interval_seconds)
    await waiter.run()
    log.info("done. all 7 nodes match the expected 1/2/1/2/1/2/3 connection-count pattern")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except TimeoutError as exc:
        log.error(str(exc))
        sys.exit(1)
