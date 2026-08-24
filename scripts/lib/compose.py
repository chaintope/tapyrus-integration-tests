"""Shared `docker compose` service control for scripts that bring specific named
core-*/signer-* services up or down as part of a live-stack scenario
(simulate_reorg.py, simulate_federation_change.py, and friends) -- every one of them
had its own byte-identical subprocess helper before this.

Every function below takes explicit service names, never a vague "the signers" or
"the core nodes" -- callers already know exactly which ones they mean at each point in
a recipe (e.g. a specific group's 4 nodes, or just the 2 brand-new signer-b-1/
signer-b-2), and a shared helper that silently assumed a fixed set would be the wrong
kind of DRY: it would need editing (or a growing pile of flags) every time a new
script's recipe needs a different subset.

Usage:
    from scripts.lib.compose import start_nodes, stop_nodes, bring_up, recreate_fresh
    await stop_nodes("core-3a", "core-3b", "core-7")
    await start_nodes(*SIGNERS)
    await bring_up("signer-b-1", "signer-b-2")     # services with no container yet
    await recreate_fresh("redis")                  # force a genuinely fresh instance
    await wait_for_running(*SIGNERS)                # confirm they're still up, not just that `up` exited 0
"""
import asyncio
import subprocess
import time
from pathlib import Path

from scripts.lib.log import log

DOCKER_DIR = Path(__file__).resolve().parent.parent.parent / "docker"


class ComposeError(Exception):
    """A `docker compose` invocation exited non-zero -- see the wrapped stderr."""


async def compose(*args):
    """Runs `docker compose <args>` in docker/, raising ComposeError on a non-zero
    exit. The primitive every helper below is built from -- kept public too, for a
    compose subcommand these helpers don't wrap.
    """
    log.info(f"docker compose {' '.join(args)}")
    process = await asyncio.create_subprocess_exec(
        "docker", "compose", *args, cwd=str(DOCKER_DIR),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    if process.returncode != 0:
        raise ComposeError(str(subprocess.CalledProcessError(process.returncode, ("docker", "compose", *args), stdout, stderr)))


async def start_nodes(*service_names):
    """`docker compose up -d --no-deps <service_names>` -- resumes already-created
    containers (e.g. after stop_nodes), picking up any config file changes made while
    stopped. --no-deps, not plain "start": signer services declare their RPC target
    as a depends_on (docker/docker-compose.yml), which "start" would cascade into,
    silently starting a node from a group meant to stay isolated.
    """
    await compose("up", "-d", "--no-deps", *service_names)


async def stop_nodes(*service_names):
    """`docker compose stop <service_names>` -- containers stay created (fast
    restart via start_nodes later), just not running.
    """
    await compose("stop", *service_names)


async def bring_up(*service_names):
    """`docker compose up -d --no-deps <service_names>` -- for services with no
    container yet (first-time creation), unlike start_nodes/stop_nodes which only
    ever act on already-created ones. --no-deps for the same reason as start_nodes.
    """
    await compose("up", "-d", "--no-deps", *service_names)


async def recreate_fresh(*service_names):
    """`docker compose up -d --force-recreate --no-deps <service_names>` -- forces a
    genuinely new container (fresh writable layer), not just a process restart. For
    services with no mounted volume, this is the only way to reset their state -- see
    simulate_reorg.py's redis reset, where round-coordination state must not survive
    between the isolated groups' builds. --no-deps for the same reason as
    start_nodes/bring_up: harmless for redis today (no depends_on), but this helper
    should stay safe by default if it's ever pointed at a service that has one.
    """
    await compose("up", "-d", "--force-recreate", "--no-deps", *service_names)


async def wait_for_running(*service_names, timeout_seconds=30, poll_interval_seconds=2):
    """Confirms every one of these already-started services is still actually
    running -- `docker compose up -d` exiting 0 (compose()'s only success check)
    means Docker successfully launched the containers, nothing more; it says
    nothing about whether the process inside stayed alive afterward. No service in
    docker-compose.yml has a `restart:` policy, so a startup crash (a transient
    dependency race, or the runner itself killing it under resource pressure) just
    leaves the container Exited, silently, with `up` having already reported
    success. Confirmed live: simulate_reorg.py's _restore_default_signers saw
    exactly this -- 2 of 3 signers died seconds after "starting", undetected, and
    the first sign of trouble was a completely different script (generate_traffic.py)
    hanging its full mempool-wait timeout minutes later with no diagnostic pointing
    back here. See doc/work-done.md.

    Deliberately a liveness check only (container still running), not a deeper
    health check (e.g. confirming round-signing is actually progressing) -- this
    repo has no compose-level healthchecks yet (see doc/work-done.md's Known
    issues), and liveness is what the confirmed failure needed."""
    deadline = time.monotonic() + timeout_seconds
    while True:
        process = await asyncio.create_subprocess_exec(
            "docker", "compose", "ps", "--status", "running", "--format", "{{.Service}}", *service_names,
            cwd=str(DOCKER_DIR), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            raise ComposeError(str(subprocess.CalledProcessError(
                process.returncode, ("docker", "compose", "ps", *service_names), stdout, stderr
            )))
        running = set(stdout.decode().split())
        missing = [name for name in service_names if name not in running]
        if not missing:
            return
        if time.monotonic() >= deadline:
            raise ComposeError(f"{missing} never reached (or fell out of) a running state within {timeout_seconds}s")
        await asyncio.sleep(poll_interval_seconds)
