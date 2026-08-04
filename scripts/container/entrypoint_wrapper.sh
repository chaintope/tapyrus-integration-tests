#!/bin/sh
# Runs INSIDE a core-* container, as its command: -- everything in scripts/container/
# does, unlike the rest of scripts/ which runs on the CI host (see doc/scripts.md).
#
# NODE_ORCHESTRATOR set: hand off to node_orchestrator.py, which launches and
# supervises tapyrusd itself (real stop/restart/invalidate/reconsider chaos -- see
# doc/work-done.md). Unset: exec tapyrusd directly, identical to every bring-up mode
# before this one existed -- this image is a strict superset of the plain one, so it's
# used for every core-* service regardless of mode.
#
# "$@" is whatever docker-compose.yml's command: passed after this script's own path
# (e.g. -datadir=... -conf=... -connect=core-1a -listen=1) -- tapyrusd's real args
# either way, just via one extra layer when the orchestrator is supervising it.
set -eu

if [ -n "${NODE_ORCHESTRATOR:-}" ]; then
    exec python3 /app/scripts/container/node_orchestrator.py \
        --node-name="$NODE_NAME" \
        --restart-flavor="$NODE_ORCHESTRATOR_FLAVOR" \
        -- tapyrusd "$@"
else
    exec tapyrusd "$@"
fi
