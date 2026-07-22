#!/usr/bin/env python3
"""Render the prod-mode tapyrus.conf shared by all 7 core-* nodes.

tapyrus/tapyrusd's own entrypoint.sh only auto-generates a conf if none is mounted at
${CONF_DIR}/tapyrus.conf -- and that auto-generated one hardcodes dev=1 / [dev] /
networkid=1905960821, which is what silently made NETWORK_ID a dead input (see
doc/work-done.md): nothing ever mounted a conf of our own, so every node always ran
that fixed dev network regardless of what NETWORK_ID was set to.

Verified against a real tapyrusd container: dropping dev=1/[dev] (prod mode -- see
"Build unsigned genesis", which no longer passes tapyrus-genesis -dev either) and
setting networkid=<NETWORK_ID> at the top level is what the entrypoint script reads to
decide which genesis.<network_id> file to load -- getblockchaininfo then reports
"mode": "prod" and "chain": "<network_id>" as expected.

Usage:
    ./scripts/render_tapyrus_conf.py [output-file]

Reads NETWORK_ID / CORE_RPC_USER / CORE_RPC_PASS from the environment (same job-level
env vars the workflow already sets for the other steps).
"""
import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.lib.log import log  # noqa: E402

DEFAULT_OUTPUT = REPO_ROOT / "docker" / "generated" / "tapyrus.conf"


class TapyrusConfRenderer:
    """Renders the one tapyrus.conf every core-* node mounts (see
    docker/docker-compose.yml) -- prod mode, rpcport fixed to match the compose file's
    RPC port mappings, networkid the only thing that varies per run.

    No `port=` (P2P listen port) override -- deliberately left at prod mode's own
    default (2357, confirmed against a real container's "Bound to 0.0.0.0:2357" log
    line). docker/docker-compose.yml's -connect=<service-name> targets have no
    explicit port, so tapyrus-core resolves them against the chain's *default* P2P
    port (CConnman::ConnectNode), not whatever this conf's own port= says -- pinning
    port= to a value here would silently desync from what -connect actually dials,
    breaking every P2P edge with nothing listening on the other end. See
    doc/work-done.md.
    """

    def __init__(self, network_id, rpc_user, rpc_pass):
        self._network_id = network_id
        self._rpc_user = rpc_user
        self._rpc_pass = rpc_pass

    def render(self):
        return (
            f"rpcuser={self._rpc_user}\n"
            f"rpcpassword={self._rpc_pass}\n"
            "bind=0.0.0.0\n"
            "rpcallowip=0.0.0.0/0\n"
            "\n"
            "server=1\n"
            "keypool=1\n"
            "discover=0\n"
            "\n"
            "rpcport=12381\n"
            f"networkid={self._network_id}\n"
            "\n"
            # tapyrusd's own default is disabled (0), not the 0.0002 shown as an
            # example in --help -- on a brand-new chain (every run here starts one),
            # estimatesmartfee has no block history yet and sendtoaddress/issuetoken
            # fail outright with "Fee estimation failed" until fallbackfee is set.
            # Confirmed live: scripts/generate_traffic.py's funding/send/issuance
            # calls failed 100% of the time without this, for the whole run, on a
            # throwaway dev chain there's no real fee market to estimate anyway. See
            # doc/work-done.md.
            "fallbackfee=0.0002\n"
            # Small test chain (a handful of blocks, a handful of UTXOs) -- the
            # 450MB default is pure overhead across 7 concurrent core-* containers.
            "dbcache=64\n"
            # Default (100) sized for a real, internet-facing node fending off
            # unconnectable garbage from arbitrary peers; this network's only peers
            # are the other 6 core-* nodes (docker/docker-compose.yml's fixed
            # -connect topology). Kept above 0, not tightened to it, since the
            # planned reorg step (doc/project-plan.md Milestone 3/4) can transiently
            # orphan real transactions from the losing side.
            "maxorphantx=20\n"
            # Default (336h / 2 weeks) targets a long-running production node; every
            # run here is a single CI job under a few hours (see the workflow's
            # timeout-minutes). 2h bounds mempool memory without risking evicting
            # anything an hours-long full-scale run still needs mid-test.
            "mempoolexpiry=2\n"
        )


def parse_args():
    parser = argparse.ArgumentParser(description="Render the shared prod-mode tapyrus.conf for the 7 core-* nodes.")
    parser.add_argument(
        "output", nargs="?", type=Path, default=DEFAULT_OUTPUT,
        help=f"default: {DEFAULT_OUTPUT}",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    network_id = os.environ.get("NETWORK_ID", "1905960821")
    rpc_user = os.environ.get("CORE_RPC_USER", "rpcuser")
    rpc_pass = os.environ.get("CORE_RPC_PASS", "rpcpassword")

    log.step(f"rendering tapyrus.conf (networkid={network_id})")
    renderer = TapyrusConfRenderer(network_id, rpc_user, rpc_pass)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(renderer.render())
    log.info(f"wrote {args.output}")


if __name__ == "__main__":
    main()
