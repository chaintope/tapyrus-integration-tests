#!/usr/bin/env python3
"""Verify tapyrus-seeder end-to-end, in both bring-up modes docker-compose.yml
supports for the 7 core-* nodes:

1. **addseeder mode**: all 7 nodes come up with only `-addseeder=<seed-hostname>`,
   no `-connect` at all. Proves the seeder can grow every node's peer count from
   nothing, organically, via real DNS-seed discovery -- not just that `dig` resolves
   something. See doc/work-done.md's Lessons learnt for why this needs its own
   phase: a node with no `-connect` of its own uses whatever `-addseeder` hands it to
   open real outbound connections, which is the point here but silently destabilizes
   the fixed topology if left on permanently.
2. **connect mode**: the fixed topology every other script in this repo (traffic
   generation, reorg, federation change) actually runs against. Re-verifies the
   seeder correctly reports only genuinely-listening nodes (`core-7` never listens
   here, seeded anyway as a deliberate negative case), and that a brand-new node with
   no topology knowledge of its own can auto-bootstrap through the seeder alone.

Both modes depend on docker-compose.yml's `default:` network using a custom,
non-default subnet -- see that file's own comment and doc/work-done.md's Lessons
learnt for why (short version: tapyrus-core and tapyrus-seeder both refuse to ever
treat a non-routable address as usable, and Docker's default bridge range -- and
every "officially reserved for testing" range -- counts as non-routable).

Usage:
    ./scripts/verify_seeder.py

Requires only redis already up (`docker compose up -d redis`) -- this script brings
the 7 core-* nodes up itself, in both modes above, tearing down and recreating them
between the two. Run it before wait_for_topology.py and anything else that depends
on the fixed topology being stable (traffic generation, reorg, etc.), not after.
"""
import asyncio
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.lib.compose import ComposeError, bring_up, compose  # noqa: E402
from scripts.lib.log import log  # noqa: E402
from scripts.lib.rpc import CoreRpcClient, RpcError, RpcUnreachable  # noqa: E402

RPC_HOST = "127.0.0.1"
SEEDER_TEST_NODE_RPC_PORT = 12388
SEEDER_DNS_PORT = 5390
SEED_HOSTNAME = "seed.tapyrus-integration-tests.local"

LISTENING_NODES = ("core-1a", "core-1b", "core-2a", "core-2b", "core-3a", "core-3b")
CORE_NODES = LISTENING_NODES + ("core-7",)

CORE_RPC_PORTS = {
    "core-1a": 12381, "core-1b": 12382, "core-2a": 12383,
    "core-2b": 12384, "core-3a": 12385, "core-3b": 12386, "core-7": 12387,
}

# The fixed topology every other script in this repo runs against (docker-compose.yml
# section 4b) -- see that file's own "CONNECT/LISTEN DESIGN" comment.
#
# core-2b carries -persistmempool=1: it's one of node_orchestrator.py's CHAOS_NODES
# (deliberately restarted/reindexed/invalidated). With this set, a clean restart
# persists the mempool to disk first, so a transaction core-2b sent as sender
# should never go missing from its own wallet view that way -- which is why
# generate_traffic.py's CHAOS_SENDER_GRACE_NODES deliberately excludes core-2b
# from the other three chaos nodes' extra confirmation-check retries; it
# shouldn't need them.
CONNECT_MODE_ARGS = {
    "core-1a": "",
    "core-1b": "-connect=core-1a -listen=1",
    "core-2a": "",
    "core-2b": "-connect=core-2a -listen=1 -persistmempool=1",
    "core-3a": "",
    "core-3b": "-connect=core-3a -listen=1",
    "core-7": "-connect=core-1b -connect=core-2b -connect=core-3b",
}

DIG_POLL_INTERVAL_SECONDS = 10
# db.cpp's MIN_RETRY (60s) x2 (need 3 successful tests, 60s+ apart, per address) plus
# handshake/test overhead and margin -- plus, unlike a seeder started against
# already-existing core nodes, ThreadSeeder's -s hostname resolution here always
# misses on its first attempt (the core nodes come up at nearly the same moment as
# the seeder itself, in both phases below), so it eats a full extra 3-minute wait for
# its own periodic re-resolution before any testing can even begin. Confirmed live:
# a 300s timeout was too tight for this design, cutting it off mid-convergence.
DIG_TIMEOUT_SECONDS = 480
RPC_READY_TIMEOUT_SECONDS = 120
RPC_READY_POLL_INTERVAL_SECONDS = 3
BOOTSTRAP_TIMEOUT_SECONDS = 120
PEER_DISCOVERY_POLL_INTERVAL_SECONDS = 5
PEER_DISCOVERY_TIMEOUT_SECONDS = 90
# Every core-* node in this test network sits on the same /24 (51.51.51.0/24,
# see work-done.md's Lessons learnt), so they're all one netgroup --
# tapyrus-core's own outbound-connection diversity logic (ThreadOpenConnections,
# net.cpp: "Only connect out to one peer per network group") caps each node's
# OWN outbound dials at ~1 real peer, permanently, no matter how many addresses
# -addseeder handed it. A node not lucky enough to also be picked as someone
# else's inbound target legitimately never exceeds 1 real peer -- confirmed
# live: requiring 2+ total peers only passed for a stuck node once the seeder's
# own periodic crawl cycled back around and re-probed it (a second, transient
# connection), 15+ minutes later, not because of any new organic discovery.
# The seeder's own crawl connection is transient anyway (see the same Lessons
# learnt), so it can't be relied on to reliably contribute to a peer count at
# all. The real, achievable signal is a peer that ISN'T the seeder -- 1 is
# enough, as long as it's the right one.
SEEDER_SUBVER = "/tapyrus-seeder:0.01/"


class SeederVerificationError(Exception):
    """The seeder didn't behave as expected -- see the message for which check."""


def _require_dig():
    # Without this, a missing `dig` surfaces as a raw FileNotFoundError traceback
    # from subprocess.run below -- it's raised before main()'s own except tuple can
    # catch anything meaningful. GitHub's ubuntu runners ship it; this only ever
    # bites a local run without bind-utils/dnsutils installed.
    if shutil.which("dig") is None:
        log.error("'dig' not found on PATH -- required for this script's DNS checks")
        log.error("install it first (e.g. `apt install dnsutils` or `brew install bind`)")
        sys.exit(1)


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


def _args_env_var(node_name):
    # core-1a -> CORE_1A_ARGS -- matches docker-compose.yml's ${CORE_1A_ARGS:-} etc.
    return f"CORE_{node_name[len('core-'):].upper()}_ARGS"


class SeederVerifier:
    async def run(self):
        # Every core-* service bind-mounts ../runtime/orchestrator-control -- if
        # runtime/ doesn't exist yet when the first `docker compose up` below creates
        # it, the Docker daemon (root) creates it owned by root:root, and every
        # host-side write under runtime/ afterwards (this job's own
        # PAUSE_FILE.touch(), assemble_signer_configs.py's default output dir) then
        # fails as the non-root runner user. Pre-creating it here, before any
        # container touches it, keeps ownership with the runner.
        (REPO_ROOT / "runtime" / "orchestrator-control").mkdir(parents=True, exist_ok=True)
        # Same root-ownership reasoning for the cookie mount, plus clearing any
        # *.cookie left over from a previous run that got SIGKILLed rather than shut
        # down cleanly (a clean tapyrus-core shutdown deletes its own cookie file; a
        # SIGKILL doesn't) -- a stale cookie would otherwise be briefly readable with
        # the wrong credentials until the fresh one overwrites it.
        cookie_dir = REPO_ROOT / "runtime" / "rpc-cookies"
        cookie_dir.mkdir(parents=True, exist_ok=True)
        for stale_cookie in cookie_dir.glob("*.cookie"):
            stale_cookie.unlink()
        # Same signed genesis every core-* node loads -- needed in this process's own
        # environment for both bring-up phases below (docker-compose.yml's
        # GENESIS_BLOCK_WITH_SIG substitution), same as the workflow's old "Bring up
        # redis + 7 core nodes" step used to set inline.
        os.environ["GENESIS_BLOCK_WITH_SIG"] = (REPO_ROOT / "secrets" / "signer-set-a" / "genesis.hex").read_text().strip()
        try:
            await self._bring_up_seeder()
            await self._run_addseeder_phase()
            await self._run_connect_phase()
            node_ips = self._resolve_node_ips()
            await self._wait_for_seeder_convergence_connect_mode(node_ips)
            await self._verify_new_node_bootstraps_via_seeder(node_ips)
            log.info("done. seeder grows peer counts organically via -addseeder, correctly reports only listening nodes in connect mode, and a brand-new node auto-bootstrapped through it for real.")
        finally:
            # Guaranteed even on failure, not just success: this script's own job is
            # done once run() returns either way, but seeder/seeder-test-node aren't
            # part of the fixed topology every later step (reorg, federation change)
            # runs against -- left running, seeder-test-node holds a persistent P2P
            # connection into whichever listener it discovered, permanently throwing
            # off wait_for_topology.py's exact getconnectioncount match on that node
            # (and, during simulate_reorg.py's isolated-build phases, gives the
            # supposedly-isolated group a real path to learn the OTHER group's
            # blocks via header relay -- silently defeating the strict-alternation
            # isolation the whole reorg recipe depends on). The seeder's own crawler
            # adds flakiness on top even where seeder-test-node never attached: it
            # re-tests every address on its own cycle with real TCP connections held
            # open waiting for a reply that never comes (see work-done.md's Lessons
            # learnt), which can transiently perturb any node's exact-count poll.
            log.step("tearing down seeder-test-node and seeder (verification complete)")
            await compose("stop", "seeder-test-node", "seeder")
            await compose("rm", "-f", "seeder-test-node", "seeder")

    async def _bring_up_seeder(self):
        log.step("bringing up tapyrus-seeder, seeded directly from all 7 core nodes")
        await bring_up("seeder")
        os.environ["SEEDER_IP"] = _container_ip("docker-seeder-1")

    async def _run_addseeder_phase(self):
        log.step("phase 1 (addseeder mode): bringing up all 7 core nodes with only -addseeder, no -connect")
        for node in CORE_NODES:
            os.environ[_args_env_var(node)] = f"-addseeder={SEED_HOSTNAME}"
        await bring_up(*CORE_NODES)
        await self._wait_for_all_rpc_ready(CORE_NODES)

        log.step("waiting for the seeder's own -s crawl to converge -- it doesn't need the core nodes' -addseeder to work")
        await self._wait_for_seeder_convergence_all_7(self._resolve_node_ips())

        # ThreadDNSAddressSeed (tapyrus-core's -addseeder consumer) runs exactly once
        # at process startup, before the seeder above had converged -- confirmed live,
        # it logged "0 addresses found from DNS seeds" the first time. Restarting now
        # re-runs it against a seeder that actually has answers.
        log.step("restarting the 7 core nodes so their one-shot -addseeder lookup runs again, now that the seeder has real answers")
        await compose("restart", *CORE_NODES)
        await self._wait_for_all_rpc_ready(CORE_NODES)

        await self._verify_peer_discovery()

    async def _run_connect_phase(self):
        log.step("phase 2 (connect mode): tearing down the 7 core nodes and the seeder, restoring the fixed topology")
        # Also recreating the seeder, not just the core nodes: container recreation
        # gives every node a new IP, and the seeder's phase-1 "good" set is keyed on
        # the old ones -- reusing it would mean waiting out stale entries instead of
        # converging cleanly.
        await compose("stop", *CORE_NODES, "seeder")
        await compose("rm", "-f", *CORE_NODES, "seeder")

        for node, args in CONNECT_MODE_ARGS.items():
            os.environ[_args_env_var(node)] = args

        await self._bring_up_seeder()
        await bring_up(*CORE_NODES)
        await self._wait_for_all_rpc_ready(CORE_NODES)
        self._persist_env_for_rest_of_job()

    def _persist_env_for_rest_of_job(self):
        # GENESIS_BLOCK_WITH_SIG/SEEDER_IP/CORE_<NAME>_ARGS only live in this
        # process's own os.environ otherwise -- any later script or workflow step
        # that touches `docker compose up` on these containers, from a fresh
        # process that never re-derives them, sees Compose treat the
        # resolved-to-empty fallback as a real config change and silently
        # recreates them, resetting -connect (and anything else in command:) to
        # whatever that fresh process happens to resolve. Confirmed live twice:
        # "Bring up signers" recreating core-1a/2a/3a via their depends_on on
        # signer-0/1/2 (SEEDER_IP unset there), and simulate_reorg.py stranding
        # core-1b/core-2b with no peers this same way when it restarts group A --
        # which hung the whole job past its own timeout, since nothing else can
        # ever make group A's height-wait succeed once two of its four nodes can
        # never receive another block. See doc/work-done.md.
        github_env = os.environ.get("GITHUB_ENV")
        if not github_env:
            return  # not running under GitHub Actions -- nothing to persist to
        with open(github_env, "a") as f:
            f.write(f"GENESIS_BLOCK_WITH_SIG={os.environ['GENESIS_BLOCK_WITH_SIG']}\n")
            f.write(f"SEEDER_IP={os.environ['SEEDER_IP']}\n")
            for node, args in CONNECT_MODE_ARGS.items():
                f.write(f"{_args_env_var(node)}={args}\n")
        log.info("persisted GENESIS_BLOCK_WITH_SIG/SEEDER_IP/CORE_*_ARGS to $GITHUB_ENV for the rest of the job")

    def _resolve_node_ips(self):
        ips = {name: _container_ip(f"docker-{name}-1") for name in CORE_NODES}
        log.info(f"resolved container IPs: {ips}")
        return ips

    async def _verify_peer_discovery(self):
        log.step("polling all 7 nodes until each has a real peer (not just the seeder's own probe)")
        clients = {n: CoreRpcClient(RPC_HOST, CORE_RPC_PORTS[n], n) for n in CORE_NODES}

        discovered = set()
        deadline = time.monotonic() + PEER_DISCOVERY_TIMEOUT_SECONDS
        while len(discovered) < len(CORE_NODES):
            # Every not-yet-discovered node checked concurrently, not one at a
            # time -- each node's own log line below always names it, since
            # gather() doesn't preserve the per-node ordering a sequential loop
            # would.
            pending = [n for n in CORE_NODES if n not in discovered]
            results = await asyncio.gather(*(self._check_node_peers(clients[n], n) for n in pending))
            counts = {}
            for n, peer_count, has_real_peer in results:
                counts[n] = peer_count
                if has_real_peer:
                    discovered.add(n)
            if len(discovered) >= len(CORE_NODES):
                break
            if time.monotonic() >= deadline:
                missing = set(CORE_NODES) - discovered
                raise SeederVerificationError(
                    f"these nodes never got a real (non-seeder) peer within "
                    f"{PEER_DISCOVERY_TIMEOUT_SECONDS}s: {missing} (last peer counts: {counts})"
                )
            await asyncio.sleep(PEER_DISCOVERY_POLL_INTERVAL_SECONDS)
        log.info("confirmed: every core node discovered at least one real peer organically via -addseeder")

    async def _check_node_peers(self, client, name):
        peers = await client.call("getpeerinfo")
        has_real_peer = any(p.get("subver") != SEEDER_SUBVER for p in peers)
        if has_real_peer:
            log.info(f"{name}: confirmed a real core-node peer (not the seeder) came from -addseeder")
        return name, len(peers), has_real_peer

    async def _wait_for_seeder_convergence_all_7(self, node_ips):
        # addseeder mode: core-7 has no -connect at all here, so it listens like the
        # other 6 and legitimately becomes good too -- confirmed live (dig returned
        # all 7). No exclusion check, unlike connect mode below.
        expected_ips = set(node_ips.values())

        log.step(f"polling the seeder's DNS ({SEED_HOSTNAME}) until it reports all 7 nodes as good")
        deadline = time.monotonic() + DIG_TIMEOUT_SECONDS
        while True:
            returned_ips = _dig_a_records(RPC_HOST, SEEDER_DNS_PORT, SEED_HOSTNAME)
            log.info(f"dig returned {len(returned_ips)}/7 expected address(es): {returned_ips or '(none yet)'}")
            if returned_ips == expected_ips:
                log.info("confirmed: seeder reports all 7 nodes as good")
                return
            if time.monotonic() >= deadline:
                raise SeederVerificationError(
                    f"seeder never converged on all 7 nodes within {DIG_TIMEOUT_SECONDS}s -- "
                    f"last response: {returned_ips}, expected: {expected_ips}"
                )
            await asyncio.sleep(DIG_POLL_INTERVAL_SECONDS)

    async def _wait_for_seeder_convergence_connect_mode(self, node_ips):
        expected_ips = {node_ips[name] for name in LISTENING_NODES}
        excluded_ip = node_ips["core-7"]

        log.step(
            f"polling the seeder's DNS ({SEED_HOSTNAME}) until it reports all "
            f"{len(LISTENING_NODES)} listening nodes -- confirming core-7 is never among them"
        )
        deadline = time.monotonic() + DIG_TIMEOUT_SECONDS
        while True:
            # This script itself runs on the host, not as a container on the compose
            # network, so it must go through the seeder's host-published port
            # (127.0.0.1:5390) -- the host has no route to the seeder's actual
            # container IP under Docker Desktop, only to ports it published. A
            # container-to-container caller doesn't have that problem and can use
            # the real container IP directly; that's why seeder-test-node below
            # points its dns: at SEEDER_IP (via _container_ip) instead of this same
            # host-published port.
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
        expected_ips = {node_ips[name] for name in LISTENING_NODES}

        log.step(f"bringing up seeder-test-node (no -connect, -addseeder only) via seeder at {seeder_ip}")
        await bring_up("seeder-test-node")

        client = CoreRpcClient(RPC_HOST, SEEDER_TEST_NODE_RPC_PORT, "seeder-test-node")
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

    async def _wait_for_all_rpc_ready(self, nodes):
        clients = [CoreRpcClient(RPC_HOST, CORE_RPC_PORTS[n], n) for n in nodes]
        await asyncio.gather(*(self._wait_for_rpc_ready(c) for c in clients))

    async def _wait_for_rpc_ready(self, client):
        deadline = time.monotonic() + RPC_READY_TIMEOUT_SECONDS
        while True:
            try:
                await client.call("getblockcount")
                return
            except RpcUnreachable as exc:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"a node's RPC never became reachable within {RPC_READY_TIMEOUT_SECONDS}s: {exc}")
                await asyncio.sleep(RPC_READY_POLL_INTERVAL_SECONDS)


async def main():
    _require_dig()

    log.step("verifying tapyrus-seeder in both bring-up modes: addseeder-driven peer growth, then the fixed connect topology")
    verifier = SeederVerifier()
    await verifier.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (SeederVerificationError, TimeoutError, ComposeError, RpcError, RpcUnreachable, subprocess.CalledProcessError) as exc:
        log.error(str(exc))
        sys.exit(1)
