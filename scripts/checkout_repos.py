#!/usr/bin/env python3
"""Clone (or update) tapyrus-signer, tapyrus-core, and tapyrus-seeder at configurable
URL/ref into ./workdir/, so the Docker builds have something to build from. The three
repos are checked out concurrently (asyncio.gather), not one after another.

Usage: ./scripts/checkout_repos.py
Config: config/repos.py (override any field via the environment first, e.g.
        `SIGNER_REPO_URL=/path/to/local/checkout ./scripts/checkout_repos.py`)
"""
import asyncio
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from config.repos import ReposConfig  # noqa: E402
from scripts.lib.log import log  # noqa: E402


class RepoCheckout:
    """Clones or updates a single upstream repo into workdir/<name>."""

    def __init__(self, workdir):
        self._workdir = workdir

    async def run(self, repo):
        dest = self._workdir / repo.name
        if (dest / ".git").is_dir():
            await self._update(repo, dest)
        else:
            await self._clone(repo, dest)
        # tapyrus-core vendors secp256k1 as a git submodule -- harmless no-op for
        # repos with no submodules (see doc/work-done.md).
        await self._git(dest, "submodule", "update", "--init", "--recursive")
        rev = (await self._git(dest, "rev-parse", "--short", "HEAD")).strip()
        subject = (await self._git(dest, "log", "-1", "--format=%s")).strip()
        log.info(f"{repo.name}: now at {rev} ({subject})")

    async def _update(self, repo, dest):
        log.info(f"{repo.name}: updating existing checkout at {dest}")
        await self._git(dest, "fetch", "origin", repo.ref)
        await self._git(dest, "checkout", "FETCH_HEAD")

    async def _clone(self, repo, dest):
        log.info(f"{repo.name}: cloning {repo.url} @ {repo.ref} -> {dest}")
        returncode, _, _ = await self._run(
            "git", "clone", "--branch", repo.ref, "--single-branch", "--depth", "1",
            repo.url, str(dest),
            check=False,
        )
        if returncode != 0:
            # ref might be a commit sha, not a branch/tag -- fall back to a full clone.
            log.warn(
                f"{repo.name}: '{repo.ref}' isn't a branch/tag on a shallow clone, "
                "retrying with a full clone"
            )
            await self._run("git", "clone", repo.url, str(dest))
            await self._git(dest, "checkout", repo.ref)

    async def _git(self, dest, *args):
        _, stdout, _ = await self._run("git", "-C", str(dest), *args)
        return stdout

    @staticmethod
    async def _run(*cmd, check=True):
        process = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        if check and process.returncode != 0:
            raise subprocess.CalledProcessError(process.returncode, cmd, stdout.decode(), stderr.decode())
        return process.returncode, stdout.decode(), stderr.decode()


async def main():
    config = ReposConfig()
    workdir = REPO_ROOT / "workdir"
    workdir.mkdir(exist_ok=True)

    log.step("checking out tapyrus-signer, tapyrus-core, tapyrus-seeder")
    checkout = RepoCheckout(workdir)
    await asyncio.gather(*(checkout.run(repo) for repo in config))

    contents = " ".join(sorted(p.name for p in workdir.iterdir()))
    log.info(f"done. workdir contents: {contents}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except subprocess.CalledProcessError as exc:
        log.error(f"command failed: {' '.join(exc.cmd)}")
        if exc.stderr:
            log.error(exc.stderr.strip())
        sys.exit(1)
