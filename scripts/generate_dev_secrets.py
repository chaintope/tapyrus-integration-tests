#!/usr/bin/env python3
"""Run the real tapyrus-setup federation-setup ceremony (createkey -> createnodevss ->
aggregate) for a throwaway signer set, for local/dev use.

This drives the ACTUAL tapyrus-setup CLI, built from whichever tapyrus-signer ref is
checked out (config/repos.py's SIGNER_REPO_REF, defaulted from the signer_repo_ref
workflow input) -- not a simulation. Verified manually end-to-end (see
doc/work-done.md): 3 signers/threshold 2 converge on an identical aggpubkey, and a
genesis block signed with the follow-on ceremony (createblockvss/sign/computesig, see
sign_genesis.py) was loaded and validated by a real tapyrusd node.

Scope: this script only covers aggpubkey generation (steps 1-3 of the ceremony).
Genesis-signing (steps 4-7) is sign_genesis.py.

Usage:
    ./scripts/generate_dev_secrets.py <set-name> <node-count> <threshold> [tapyrus-setup-bin]

Example (the initial 3-signer federation, threshold 2):
    ./scripts/generate_dev_secrets.py signer-set-a 3 2

Example (a second, disjoint key set used later for federation rotation -- see
doc/weekly-integration-test-plan.md section 3a's --xfield signing flow):
    ./scripts/generate_dev_secrets.py signer-set-b 3 2

tapyrus-setup-bin defaults to workdir/tapyrus-signer/target/release/tapyrus-setup
(i.e. built via checkout_repos.py + `cargo build --release` there first).

Output layout (all under ./secrets/, which is gitignored -- nothing here is ever
meant to be committed):
    secrets/<set-name>/pubkeys.txt                  all N compressed pubkeys, one per line
    secrets/<set-name>/aggregated-public-key.txt     the converged aggpubkey (hex) --
                                                      verified identical across all N nodes
    secrets/<set-name>/node-<i>/
        signer.key             WIF private key for node i                      (0600)
        public-key.txt         node i's own compressed pubkey (hex)
        node-secret-share.hex  node i's secret share from `aggregate`           (0600)
    secrets/<set-name>/raw/nodevss_from_<i>.txt      raw createnodevss stdout per sender,
                                                      kept for audit/debugging and reuse by
                                                      sign_genesis.py
"""
import argparse
import asyncio
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.lib.ceremony import CeremonyError, TapyrusSetupCeremony, require_executable  # noqa: E402
from scripts.lib.log import log  # noqa: E402

SECRET_FILE_MODE = 0o600


class AggpubkeyCeremony(TapyrusSetupCeremony):
    """Runs createkey -> createnodevss -> aggregate for one signer set.

    Mirrors doc/weekly-integration-test-plan.md section 4a steps 1-3. Each step is
    its own method so a future rotation ceremony (signer-set-b) can reuse this class
    as-is -- it's already parameterized on set_dir/node_count/threshold, not tied to
    "signer-set-a".
    """

    def __init__(self, tapyrus_setup_bin, set_dir, node_count, threshold):
        super().__init__(tapyrus_setup_bin)
        self._set_dir = set_dir
        self._raw_dir = set_dir / "raw"
        self._node_count = node_count
        self._threshold = threshold
        self._wifs = []
        self._pubkeys = []

    async def run(self):
        self._set_dir.mkdir(parents=True, exist_ok=True)
        self._raw_dir.mkdir(parents=True, exist_ok=True)
        await self._create_keys()
        self._write_pubkeys_file()
        await self._create_node_vss()
        aggpubkey = await self._aggregate()
        self._write_aggpubkey_file(aggpubkey)
        return aggpubkey

    async def _create_keys(self):
        # Each signer's own createkey call is independent -- run all N concurrently,
        # closer to how N real, separate signer processes would actually do this.
        log.info(f"generating {self._node_count} signer keys (threshold {self._threshold})...")
        results = await asyncio.gather(*(self._run_setup("createkey") for _ in range(self._node_count)))
        for i, result in enumerate(results):
            wif, pubkey = result.split()
            self._wifs.append(wif)
            self._pubkeys.append(pubkey)

            node_dir = self._set_dir / f"node-{i}"
            node_dir.mkdir(exist_ok=True)
            self._write_secret(node_dir / "signer.key", wif)
            (node_dir / "public-key.txt").write_text(pubkey)

    def _write_pubkeys_file(self):
        (self._set_dir / "pubkeys.txt").write_text("\n".join(self._pubkeys) + "\n")

    def _pubkey_args(self):
        return [f"--public-key={pubkey}" for pubkey in self._pubkeys]

    async def _create_node_vss(self):
        log.info("running createnodevss for each signer...")
        outputs = await asyncio.gather(*(
            self._run_setup(
                "createnodevss", *self._pubkey_args(),
                f"--private-key={self._wifs[i]}", f"--threshold={self._threshold}",
            )
            for i in range(self._node_count)
        ))
        for i, output in enumerate(outputs):
            (self._raw_dir / f"nodevss_from_{i}.txt").write_text(output)

    async def _aggregate(self):
        log.info("running aggregate for each signer...")

        async def run_one(j):
            pubkey_j = self._pubkeys[j]
            vss_args = [
                f"--vss={self._extract_vss_for(self._raw_dir / f'nodevss_from_{i}.txt', pubkey_j)}"
                for i in range(self._node_count)
            ]
            return await self._run_setup("aggregate", *vss_args, f"--private-key={self._wifs[j]}")

        outputs = await asyncio.gather(*(run_one(j) for j in range(self._node_count)))

        aggpubkey = None
        for j, output in enumerate(outputs):
            aggpubkey_j, node_secret_share_j = output.split()
            self._write_secret(self._set_dir / f"node-{j}" / "node-secret-share.hex", node_secret_share_j)

            if aggpubkey is None:
                aggpubkey = aggpubkey_j
            elif aggpubkey != aggpubkey_j:
                raise CeremonyError(
                    f"signer {j} computed a different aggregated public key than signer 0 -- "
                    f"ceremony is broken\n  signer 0: {aggpubkey}\n  signer {j}: {aggpubkey_j}"
                )
        return aggpubkey

    def _write_aggpubkey_file(self, aggpubkey):
        (self._set_dir / "aggregated-public-key.txt").write_text(aggpubkey)

    @staticmethod
    def _write_secret(path, content):
        path.write_text(content)
        path.chmod(SECRET_FILE_MODE)


def default_tapyrus_setup_bin():
    return REPO_ROOT / "workdir" / "tapyrus-signer" / "target" / "release" / "tapyrus-setup"


def parse_args():
    parser = argparse.ArgumentParser(description="Run the aggpubkey half of the tapyrus-setup ceremony.")
    parser.add_argument("set_name", help="name for this signer set, e.g. signer-set-a")
    parser.add_argument("node_count", type=int, help="number of signers (N)")
    parser.add_argument("threshold", type=int, help="signing threshold (t <= N)")
    parser.add_argument(
        "tapyrus_setup_bin", nargs="?", type=Path, default=None,
        help="path to the tapyrus-setup binary (default: workdir/tapyrus-signer/target/release/tapyrus-setup)",
    )
    return parser.parse_args()


TAPYRUS_SETUP_BUILD_HINT = (
    "Build it first, e.g.:\n"
    "  ./scripts/checkout_repos.py   # if not already checked out\n"
    "  cd workdir/tapyrus-signer && cargo build --release"
)


async def main():
    args = parse_args()
    tapyrus_setup_bin = args.tapyrus_setup_bin or default_tapyrus_setup_bin()

    if args.threshold > args.node_count:
        log.error(f"threshold ({args.threshold}) cannot exceed node-count ({args.node_count})")
        sys.exit(1)

    require_executable(tapyrus_setup_bin, TAPYRUS_SETUP_BUILD_HINT)

    set_dir = REPO_ROOT / "secrets" / args.set_name
    if (set_dir / "pubkeys.txt").exists():
        log.error(f"{set_dir} already has generated keys -- remove it first if you want to regenerate")
        sys.exit(1)

    log.step(f"generating aggpubkey for signer set '{args.set_name}'")
    ceremony = AggpubkeyCeremony(tapyrus_setup_bin, set_dir, args.node_count, args.threshold)
    aggpubkey = await ceremony.run()

    last_index = args.node_count - 1
    log.info(f"done. all {args.node_count} signers converged on aggregated public key: {aggpubkey}")
    log.info("wrote:")
    log.info(f"  {set_dir}/pubkeys.txt")
    log.info(f"  {set_dir}/aggregated-public-key.txt")
    log.info(f"  {set_dir}/node-{{0..{last_index}}}/{{signer.key,public-key.txt,node-secret-share.hex}}")
    log.info(f"  {set_dir}/raw/nodevss_from_{{0..{last_index}}}.txt (kept for the genesis-signing step)")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except subprocess.CalledProcessError as exc:
        log.error(f"command failed: {' '.join(exc.cmd)}")
        if exc.stderr:
            log.error(exc.stderr.strip())
        sys.exit(1)
    except CeremonyError as exc:
        log.error(str(exc))
        sys.exit(1)
