"""Shared pause-file coordination with scripts/container/node_orchestrator.py.

That script checks for PAUSE_FILE before every chaos action (restart/reindex/
invalidate/reconsider) on every core-* node -- touching it here tells every node's
orchestrator to skip its own actions until it's removed again. Used by any host-side
script whose recipe depends on core nodes NOT churning mid-sequence: simulate_reorg.py
(exactly one group up and building at a time), simulate_federation_change.py and
simulate_maxblocksize_change.py (federations.toml's atomic-write + live-reload
window), and generate_traffic.py (several distinct all-7-nodes-reachable windows
within one run -- address collection, balance seeding, coinbase-rotation
calibration, each block wait). See doc/work-done.md for the full design.

pause/resume calls nest: a depth counter (in-process only, not shared across
scripts) means an inner pause/resume pair -- e.g. _wait_for_next_block's own,
called from within generate_traffic.py's own broader calibration window -- doesn't
prematurely remove the file while an outer caller still needs it paused.

Usage:
    from scripts.lib.orchestrator_control import pause_node_orchestrators, resume_node_orchestrators
    pause_node_orchestrators()
    try:
        ...
    finally:
        resume_node_orchestrators()
"""
from pathlib import Path

from scripts.lib.log import log

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PAUSE_FILE = REPO_ROOT / "runtime" / "orchestrator-control" / "pause"

_depth = 0


def pause_node_orchestrators():
    global _depth
    _depth += 1
    if _depth == 1:
        PAUSE_FILE.parent.mkdir(parents=True, exist_ok=True)
        PAUSE_FILE.touch()
        log.info("node orchestrators paused")


def resume_node_orchestrators():
    global _depth
    _depth = max(0, _depth - 1)
    if _depth == 0:
        PAUSE_FILE.unlink(missing_ok=True)
        log.info("node orchestrators resumed")
