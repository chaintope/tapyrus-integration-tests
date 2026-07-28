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
"""
import asyncio
import subprocess
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
    """`docker compose up -d <service_names>` -- for services with no container yet
    (first-time creation), unlike start_nodes/stop_nodes which only ever act on
    already-created ones.
    """
    await compose("up", "-d", *service_names)


async def recreate_fresh(*service_names):
    """`docker compose up -d --force-recreate <service_names>` -- forces a genuinely
    new container (fresh writable layer), not just a process restart. For services
    with no mounted volume, this is the only way to reset their state -- see
    simulate_reorg.py's redis reset, where round-coordination state must not survive
    between the isolated groups' builds.
    """
    await compose("up", "-d", "--force-recreate", *service_names)
