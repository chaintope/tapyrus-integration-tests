"""Shared pause-file coordination with scripts/container/node_orchestrator.py.

That script checks for PAUSE_FILE before every chaos action (restart/reindex/
invalidate/reconsider) on every core-* node -- touching it here tells every node's
orchestrator to skip its own actions until it's removed again. This is a single
shared pause across all 7 core-* nodes at once, not a per-node/per-container
action -- there's no individual container name to attach to a pause/resume event.
Used by any host-side script whose recipe depends on core nodes NOT churning
mid-sequence: simulate_reorg.py (exactly one group up and building at a time),
simulate_federation_change.py and simulate_maxblocksize_change.py
(federations.toml's atomic-write + live-reload window), and generate_traffic.py
(several distinct all-7-nodes-reachable windows within one run -- address
collection, balance seeding, each block wait, each settle-loop pass). See
doc/work-done.md for the full design.

pause/resume calls nest: a depth counter (in-process only, not shared across
scripts) means an inner pause/resume pair doesn't prematurely remove the file
while an outer caller still needs it paused -- only the outermost call (depth
1->paused, depth 0->resumed) actually touches the file or logs. `reason`
identifies what actually triggered a given pause/resume line -- with several
call sites in the same script (generate_traffic.py's settle loop alone pauses
many times a round), an unlabeled "node orchestrators paused" repeated dozens
of times gives no way to tell them apart.

Usage:
    from scripts.lib.orchestrator_control import pause_node_orchestrators, resume_node_orchestrators
    pause_node_orchestrators("collecting addresses")
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
_reason = None


def pause_node_orchestrators(reason=None):
    global _depth, _reason
    _depth += 1
    if _depth == 1:
        _reason = reason
        PAUSE_FILE.parent.mkdir(parents=True, exist_ok=True)
        PAUSE_FILE.touch()
        log.info(f"node orchestrators paused ({reason})" if reason else "node orchestrators paused")


def resume_node_orchestrators():
    global _depth, _reason
    _depth = max(0, _depth - 1)
    if _depth == 0:
        PAUSE_FILE.unlink(missing_ok=True)
        log.info(f"node orchestrators resumed ({_reason})" if _reason else "node orchestrators resumed")
        _reason = None
