"""Shared plumbing for scripts that drive the tapyrus-setup ceremony CLI (see
doc/weekly-integration-test-plan.md section 4a). AggpubkeyCeremony
(generate_dev_secrets.py) and GenesisSigningCeremony (sign_genesis.py) both need the
same "run a tapyrus-setup subcommand", "extract this signer's vss line", and "require
the binary exists" logic, so it lives here once instead of being copied.
"""
import asyncio
import os
import subprocess
import sys

from scripts.lib.log import log


class CeremonyError(Exception):
    """A ceremony precondition failed or signers diverged -- always fatal."""


def require_executable(path, build_hint):
    if path.is_file() and os.access(path, os.X_OK):
        return
    log.error(f"tapyrus-setup binary not found or not executable at: {path}")
    log.error(build_hint)
    sys.exit(1)


def extract_vss_for(file, pubkey):
    """Pull the vss line addressed to `pubkey` out of one signer's createnodevss/
    createblockvss output file.

    Standalone (not tied to TapyrusSetupCeremony) because assemble_signer_configs.py
    needs it too, without running tapyrus-setup itself at all -- it only reads the
    raw/*.txt files a prior ceremony run already produced.

    createnodevss's/createblockvss's output lines are `<receiver_pubkey>:<vss_hex>`,
    one per receiver, sorted by receiver pubkey (BTreeMap iteration) -- NOT in
    --public-key argument order. Must extract by matching the actual pubkey, never by
    line position, or this fails with an opaque InvalidSS error from tapyrus-setup
    itself. See doc/work-done.md.
    """
    for line in file.read_text().splitlines():
        receiver, _, vss = line.partition(":")
        if receiver == pubkey:
            return vss
    raise CeremonyError(f"no vss addressed to {pubkey} found in {file}")


class TapyrusSetupCeremony:
    """Base class for a multi-signer tapyrus-setup ceremony run.

    Subclasses add their own sequence of subcommands (createkey/createnodevss/
    aggregate for AggpubkeyCeremony; createblockvss/sign/computesig for
    GenesisSigningCeremony) on top of the two primitives every ceremony needs:
    running the binary, and pulling one signer's own line out of another signer's
    *vss output.
    """

    def __init__(self, tapyrus_setup_bin):
        self._tapyrus_setup_bin = tapyrus_setup_bin

    async def _run_setup(self, *args):
        cmd = [str(self._tapyrus_setup_bin), *args]
        process = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            raise subprocess.CalledProcessError(process.returncode, cmd, stdout.decode(), stderr.decode())
        return stdout.decode()

    _extract_vss_for = staticmethod(extract_vss_for)
