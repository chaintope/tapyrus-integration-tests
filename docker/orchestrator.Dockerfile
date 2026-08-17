# Adds python3 to the already-built tapyrus-core image, for scripts/container/
# node_orchestrator.py to run inside core-* containers (see doc/work-done.md).
# A strict superset of tapyrus/tapyrusd:master-local -- entrypoint_wrapper.sh falls
# through to plain tapyrusd when NODE_ORCHESTRATOR isn't set, so this image is safe
# to use for every core-* service in every bring-up mode, not just orchestrator mode.
FROM tapyrus/tapyrusd:master-local
RUN apt-get update && apt-get install -y --no-install-recommends python3 \
    && rm -rf /var/lib/apt/lists/*
