#!/usr/bin/env python3
"""Collect one coinbase payout address from each first-layer core-* node's wallet
(getnewaddress RPC), retrying each node until its RPC is actually up.

`docker compose up -d` returning just means the containers were started, not that
tapyrusd has finished initializing its RPC server -- calling getnewaddress right after
can hit connection-refused/timeout, and a naive `curl | jq` pipeline masks that (jq
prints "null" for a failed/empty response and the pipeline still exits 0). This retries
each node until it answers, and fails loudly if a node returns an empty address instead
of silently writing a bad line to the output file.

Usage:
    ./scripts/collect_coinbase_addresses.py <port> [<port> ...] [--output FILE]
        [--timeout-seconds N] [--poll-interval-seconds N]

Example (the workflow's 3 first-layer nodes):
    ./scripts/collect_coinbase_addresses.py 12381 12383 12385 --output /tmp/addrs.txt

Each port is a host-published RPC port (see docker/docker-compose.yml), reached at
127.0.0.1 the same way wait_for_topology.py does. Writes one address per line, in the
same order as the ports given, to --output (default: ./runtime/addrs.txt).

CORE_RPC_USER / CORE_RPC_PASS env vars (defaults rpcuser / rpcpassword) match the
workflow's existing job-level env vars of the same names.
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
DEFAULT_TIMEOUT_SECONDS = 120
DEFAULT_POLL_INTERVAL_SECONDS = 3


async def get_address(port, rpc_user, rpc_pass, timeout_seconds, poll_interval_seconds):
    client = CoreRpcClient(RPC_HOST, port, rpc_user, rpc_pass)
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            address = await client.call("getnewaddress")
            break
        except RpcUnreachable as exc:
            if time.monotonic() >= deadline:
                raise TimeoutError(f"port {port} never became reachable within {timeout_seconds}s: {exc}")
            await asyncio.sleep(poll_interval_seconds)

    if not address:
        raise RuntimeError(f"port {port} returned an empty getnewaddress result")
    return address


def parse_args():
    parser = argparse.ArgumentParser(description="Collect one coinbase address per first-layer node.")
    parser.add_argument("ports", type=int, nargs="+", help="host-published RPC port for each first-layer node")
    parser.add_argument("--output", type=Path, default=None, help="default: ./runtime/addrs.txt")
    parser.add_argument(
        "--timeout-seconds", type=int, default=DEFAULT_TIMEOUT_SECONDS,
        help=f"give up per node after this many seconds (default: {DEFAULT_TIMEOUT_SECONDS})",
    )
    parser.add_argument(
        "--poll-interval-seconds", type=int, default=DEFAULT_POLL_INTERVAL_SECONDS,
        help=f"seconds between retries (default: {DEFAULT_POLL_INTERVAL_SECONDS})",
    )
    return parser.parse_args()


async def main():
    args = parse_args()
    output = args.output or (REPO_ROOT / "runtime" / "addrs.txt")
    rpc_user = os.environ.get("CORE_RPC_USER", "rpcuser")
    rpc_pass = os.environ.get("CORE_RPC_PASS", "rpcpassword")

    log.step(f"collecting coinbase addresses from {len(args.ports)} node(s)")
    addresses = await asyncio.gather(*(
        get_address(port, rpc_user, rpc_pass, args.timeout_seconds, args.poll_interval_seconds)
        for port in args.ports
    ))

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(addresses) + "\n")
    log.info(f"done. wrote {len(addresses)} address(es) to {output}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (TimeoutError, RuntimeError) as exc:
        log.error(str(exc))
        sys.exit(1)
