#!/usr/bin/env python3
"""Verify tapyrus-seeder end-to-end: it correctly reports only genuinely-listening
nodes, and a brand-new node can actually auto-bootstrap onto the network through it --
not just that a container exists and answers pings.

Two checks, both against the already-running 7-node stack (docker/docker-compose.yml
section 4b) plus the `seeder` service this script brings up itself:

1. **The seeder only reports genuinely-listening nodes.** `core-7` never listens in
   this topology (see docker-compose.yml's "CONNECT/LISTEN DESIGN" note) but is
   seeded anyway, deliberately, as a real negative case. Polls `dig` against the
   seeder's DNS port until the returned address set stabilizes on exactly the 6
   nodes that DO listen (`core-1a`/`1b`/`2a`/`2b`/`3a`/`3b`) -- `core-7` can
   legitimately appear transiently before that (see doc/work-done.md's Lessons
   learnt for why), so it's only asserted against once the response has stabilized
   on the full expected set, or times out.

2. **A brand-new node can auto-bootstrap through it for real.** Brings up
   `seeder-test-node` (docker-compose.yml) -- an 8th tapyrus-core node with NO
   `-connect` at all, configured only with `-addseeder=<seed-hostname>`
   (tapyrus-core's own DNS-seed client) and its container DNS resolver pointed at
   the seeder itself (compose's `dns:` field, substituted from `SEEDER_IP` -- the
   seeder's real container IP, resolved via `docker inspect` since compose's own
   YAML can't reference another service's dynamic IP). Polls its `getpeerinfo` RPC
   until it shows at least one peer, then confirms that peer is one of the 6
   legitimately-listening nodes -- proof the new node discovered and connected to
   the real network through the seeder's DNS answer alone, with no hardcoded
   topology knowledge of its own.

Both checks depend on docker-compose.yml's `default:` network using a custom,
non-default subnet -- see that file's own comment and doc/work-done.md's Lessons
learnt for why (short version: tapyrus-core and tapyrus-seeder both refuse to ever
treat a non-routable address as usable, and Docker's default bridge range -- and
every "officially reserved for testing" range -- counts as non-routable).

Usage:
    ./scripts/verify_seeder.py

Requires the 7-node topology and signer-set-a already up and converged (same
precondition as scripts/generate_traffic.py). Reads CORE_RPC_USER / CORE_RPC_PASS
from the environment, same job-level env vars the workflow already sets.
"""
import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.lib.compose import ComposeError, bring_up  # noqa: E402
from scripts.lib.log import log  # noqa: E402
from scripts.lib.rpc import CoreRpcClient, RpcError, RpcUnreachable  # noqa: E402

RPC_HOST = "127.0.0.1"
SEEDER_TEST_NODE_RPC_PORT = 12388
SEEDER_DNS_PORT = 5390
SEED_HOSTNAME = "seed.tapyrus-integration-tests.local"

LISTENING_NODES = ("core-1a", "core-1b", "core-2a", "core-2b", "core-3a", "core-3b")
ALL_SEEDED_NODES = LISTENING_NODES + ("core-7",)

DIG_POLL_INTERVAL_SECONDS = 10
# db.cpp's MIN_RETRY (60s) x2 (need 3 successful tests, 60s+ apart, per address)
# plus handshake/test overhead and margin.
DIG_TIMEOUT_SECONDS = 300
RPC_READY_TIMEOUT_SECONDS = 120
RPC_READY_POLL_INTERVAL_SECONDS = 3
BOOTSTRAP_TIMEOUT_SECONDS = 120


class SeederVerificationError(Exception):
    """The seeder didn't behave as expected -- see the message for which check."""


def _container_ip(container_name):
    result = subprocess.run(
        ["docker", "inspect", container_name, "--format", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}"],
        capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


def _dig_a_records(dns_ip, dns_port, hostname):
    result = subprocess.run(
        ["dig", f"@{dns_ip}", "-p", str(dns_port), hostname, "A", "+short", "+time=3", "+tries=1"],
        capture_output=True, text=True,
    )
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


class SeederVerifier:
    def __init__(self, rpc_user, rpc_pass):
        self._rpc_user = rpc_user
        self._rpc_pass = rpc_pass

    async def run(self):
        await self._bring_up_seeder()
        node_ips = self._resolve_node_ips()
        await self._verify_only_listening_nodes_served(node_ips)
        await self._verify_new_node_bootstraps_via_seeder(node_ips)
        log.info("done. seeder correctly reports only listening nodes, and a brand-new node auto-bootstrapped through it for real.")

    async def _bring_up_seeder(self):
        log.step("bringing up tapyrus-seeder, seeded directly from all 7 core nodes")
        await bring_up("seeder")

    def _resolve_node_ips(self):
        ips = {name: _container_ip(f"docker-{name}-1") for name in ALL_SEEDED_NODES}
        log.info(f"resolved container IPs: {ips}")
        return ips

    async def _verify_only_listening_nodes_served(self, node_ips):
        expected_ips = {node_ips[name] for name in LISTENING_NODES}
        excluded_ip = node_ips["core-7"]

        log.step(
            f"polling the seeder's DNS ({SEED_HOSTNAME}) until it reports all "
            f"{len(LISTENING_NODES)} listening nodes -- confirming core-7 is never among them"
        )
        deadline = time.monotonic() + DIG_TIMEOUT_SECONDS
        while True:
            # This script runs on the host, not in a container -- dig against the
            # host-published port, not the seeder's internal docker-network IP
            # (that's only reachable from other containers, which is exactly why
            # seeder-test-node's SEEDER_IP below uses _container_ip instead).
            returned_ips = _dig_a_records(RPC_HOST, SEEDER_DNS_PORT, SEED_HOSTNAME)
            log.info(f"dig returned {len(returned_ips)}/{len(LISTENING_NODES)} expected address(es): {returned_ips or '(none yet)'}")
            if returned_ips == expected_ips:
                log.info("confirmed: seeder reports exactly the 6 listening nodes, never core-7")
                return
            if time.monotonic() >= deadline:
                if excluded_ip in returned_ips:
                    raise SeederVerificationError(
                        f"seeder served core-7's address ({excluded_ip}) even though it never listens -- "
                        f"full response: {returned_ips}"
                    )
                raise SeederVerificationError(
                    f"seeder never converged on all {len(LISTENING_NODES)} listening nodes within "
                    f"{DIG_TIMEOUT_SECONDS}s -- last response: {returned_ips}, expected: {expected_ips}"
                )
            await asyncio.sleep(DIG_POLL_INTERVAL_SECONDS)

    async def _verify_new_node_bootstraps_via_seeder(self, node_ips):
        seeder_ip = _container_ip("docker-seeder-1")
        os.environ["SEEDER_IP"] = seeder_ip
        # Same signed genesis every core-* node loads -- docker-compose.yml's
        # GENESIS_BLOCK_WITH_SIG substitution needs this set in this process's own
        # environment, same as the workflow's "Bring up redis + 7 core nodes" step
        # does inline; without it, this node loads no genesis at all and crashes
        # ("bad-features, Incorrect Block features" at height 0).
        os.environ["GENESIS_BLOCK_WITH_SIG"] = (REPO_ROOT / "secrets" / "signer-set-a" / "genesis.hex").read_text().strip()
        expected_ips = {node_ips[name] for name in LISTENING_NODES}

        log.step(f"bringing up seeder-test-node (no -connect, -addseeder only) via seeder at {seeder_ip}")
        await bring_up("seeder-test-node")

        client = CoreRpcClient(RPC_HOST, SEEDER_TEST_NODE_RPC_PORT, self._rpc_user, self._rpc_pass)
        await self._wait_for_rpc_ready(client)

        log.step("waiting for seeder-test-node's own getpeerinfo to show a peer discovered via the seeder")
        deadline = time.monotonic() + BOOTSTRAP_TIMEOUT_SECONDS
        while True:
            peers = await client.call("getpeerinfo")
            peer_ips = {peer["addr"].rsplit(":", 1)[0] for peer in peers}
            if peer_ips:
                if not peer_ips & expected_ips:
                    raise SeederVerificationError(
                        f"seeder-test-node connected to {peer_ips}, none of which is a legitimately "
                        f"listening node ({expected_ips}) -- something other than the seeder-provided "
                        f"address list is responsible for this connection"
                    )
                log.info(f"confirmed: seeder-test-node connected to {peer_ips & expected_ips} via the seeder")
                return
            if time.monotonic() >= deadline:
                raise SeederVerificationError(
                    f"seeder-test-node still has no peers after {BOOTSTRAP_TIMEOUT_SECONDS}s -- "
                    f"it never managed to auto-bootstrap via the seeder"
                )
            await asyncio.sleep(DIG_POLL_INTERVAL_SECONDS)

    async def _wait_for_rpc_ready(self, client):
        deadline = time.monotonic() + RPC_READY_TIMEOUT_SECONDS
        while True:
            try:
                await client.call("getblockcount")
                return
            except RpcUnreachable as exc:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"seeder-test-node's RPC never became reachable within {RPC_READY_TIMEOUT_SECONDS}s: {exc}")
                await asyncio.sleep(RPC_READY_POLL_INTERVAL_SECONDS)


async def main():
    rpc_user = os.environ.get("CORE_RPC_USER", "rpcuser")
    rpc_pass = os.environ.get("CORE_RPC_PASS", "rpcpassword")

    log.step("verifying tapyrus-seeder: only listening nodes served, and a new node auto-bootstraps through it")
    verifier = SeederVerifier(rpc_user, rpc_pass)
    await verifier.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (SeederVerificationError, TimeoutError, ComposeError, RpcError, RpcUnreachable, subprocess.CalledProcessError) as exc:
        log.error(str(exc))
        sys.exit(1)
