#!/bin/sh
# Runs INSIDE a core-* container (and seeder-test-node, the same tapyrusd image
# in every way that matters here), as its command: -- everything in
# scripts/container/ does, unlike the rest of scripts/ which runs on the CI host
# (see doc/scripts.md).
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
#
# -rpccookiefile is appended here rather than in docker-compose.yml's own per-node
# args, since $NODE_NAME is already available to build the per-node path -- see
# doc/work-done.md. Applies to every tapyrusd launch, including chaos restarts:
# node_orchestrator.py reuses these same "$@" args (its own tapyrusd_args) for
# every _launch() call, not just the first.
#
# tapyrus-core always writes that cookie file 0600 (owner-only) -- this container
# runs as root, so every other container can already read it regardless, but the
# CI host process (a non-root user) reading it straight off the bind mount can't.
# The background loop below re-chmods it to 644 on this container's own root
# privileges every time tapyrus-core (re)writes it, including chaos restarts --
# see doc/work-done.md.
set -eu

set -- "$@" -rpccookiefile="/cookies/${NODE_NAME}.cookie"

(
    while true; do
        chmod 644 "/cookies/${NODE_NAME}.cookie" 2>/dev/null || true
        sleep 1
    done
) &

if [ -n "${NODE_ORCHESTRATOR:-}" ]; then
    exec python3 /app/scripts/container/node_orchestrator.py \
        --node-name="$NODE_NAME" \
        --restart-flavor="$NODE_ORCHESTRATOR_FLAVOR" \
        -- tapyrusd "$@"
else
    exec tapyrusd "$@"
fi
