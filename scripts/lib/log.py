"""Uniform, leveled, timestamped logging for this repo's own CI/orchestration scripts.

Separate from docker-compose container log collection (the workflow's "Collect logs"
step, which pulls each container's own log via `docker logs`) -- this is for the
scripts' own narration of what they're doing, so every step's output reads the same
way regardless of which script produced it.

Usage:
    from scripts.lib.log import log
    log.step("generating signer-set-a aggpubkey")
    log.info("signer 2 converged on the same aggpubkey")
    log.warn("retrying after a transient RPC failure")
    log.error("core-1a never became healthy")   # does not exit -- caller decides
"""
import datetime
import os
import sys


class Logger:
    """Leveled logger writing timestamped lines to stdout/stderr.

    To change the destination (e.g. also tee to a file), subclass and override
    `_write` rather than editing this class.
    """

    def __init__(self, script_name=None):
        self._script_name = script_name or os.path.basename(sys.argv[0])

    def _timestamp(self):
        # UTC, numeric-only (no weekday/month names) so this never depends on locale.
        return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _write(self, stream, level, message):
        # flush=True: stdout is fully block-buffered when not attached to a tty (the
        # normal case in CI), which would otherwise delay when a line actually
        # appears -- and with concurrent asyncio coroutines (e.g. checking out 3 repos
        # at once) each logging independently, buffering could reorder their lines
        # relative to each other instead of reflecting the order they were logged in.
        print(f"{self._timestamp()} [{level:<5}] [{self._script_name}] {message}", file=stream, flush=True)

    def step(self, message):
        self._write(sys.stdout, "STEP", message)

    def info(self, message):
        self._write(sys.stdout, "INFO", message)

    def warn(self, message):
        self._write(sys.stderr, "WARN", message)

    def error(self, message):
        self._write(sys.stderr, "ERROR", message)


log = Logger()
