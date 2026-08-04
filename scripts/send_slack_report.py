#!/usr/bin/env python3
"""Send a pass/fail summary of this run to Slack via an incoming webhook.

Encodes doc/weekly-integration-test-plan.md section 3 step 12 / section 3a's "Report
on slack" design. Sent once, unconditionally (workflow step uses `if: always()`), so a
report goes out whether the run passed or failed.

Usage:
    ./scripts/send_slack_report.py <pass|fail>

Reads SLACK_WEBHOOK_URL from the environment -- if unset or blank, logs a warning and
exits 0 rather than failing the job just because the webhook hasn't been provisioned
yet (see README.md's variable table for how the workflow supplies it: a
workflow_dispatch input, falling back to the `SLACK_WEBHOOK_URL` repository secret).

Also reads (all already job-level env vars the workflow sets for other steps, plus
GitHub's own always-present step env vars -- nothing new to wire in): CORE_REPO_REF /
SIGNER_REPO_REF / SEEDER_REPO_REF, GITHUB_EVENT_NAME, GITHUB_SERVER_URL,
GITHUB_REPOSITORY, GITHUB_RUN_ID, GITHUB_SHA, SLACK_LOG_TAIL_LINES.

Run duration: RUN_STARTED_AT (Unix seconds, set by an early workflow step so this
step -- which can run tens of minutes to hours after that one -- can compute elapsed
time) is read the same way; blank/missing renders as "unknown" rather than failing.

Aggpubkeys: read from secrets/signer-set-a/aggregated-public-key.txt (always, once the
ceremony step has run) and secrets/signer-set-b/aggregated-public-key.txt (only present
once simulate_federation_change.py's rotation ceremony has run -- a run that failed
before then just omits it, not an error).

On failure, which container(s) to inline a log tail for isn't precisely attributable
without much deeper per-step instrumentation than this job has (a single sequential
job, not one step per container) -- instead of guessing from the step that failed,
this scans every container log the "Collect logs" step already wrote to logs/*.log
for a case-insensitive "error"/"panic" line, and inlines the tail of whichever
container(s) actually have one (capped at MAX_IMPLICATED_CONTAINERS, so one noisy
container can't push every other container's report out of the message).
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.lib.log import log  # noqa: E402

LOGS_DIR = REPO_ROOT / "logs"
ERROR_MARKERS = ("error", "panic")
DEFAULT_LOG_TAIL_LINES = 100
MAX_IMPLICATED_CONTAINERS = 3


def read_aggpubkey(set_name):
    path = REPO_ROOT / "secrets" / set_name / "aggregated-public-key.txt"
    if not path.is_file():
        return None
    return path.read_text().strip()


def format_duration(started_at):
    if not started_at:
        return "unknown"
    try:
        elapsed = int(time.time()) - int(started_at)
    except ValueError:
        return "unknown"
    minutes, seconds = divmod(max(elapsed, 0), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m{seconds:02d}s"


def find_implicated_containers(tail_lines):
    """Returns {container_name: [last tail_lines lines]} for every *.log file under
    LOGS_DIR containing an ERROR_MARKERS match, capped at MAX_IMPLICATED_CONTAINERS
    (alphabetical, so results are stable across runs, not just insertion order).
    """
    if not LOGS_DIR.is_dir():
        return {}

    implicated = {}
    for log_file in sorted(LOGS_DIR.glob("*.log")):
        lines = log_file.read_text(errors="replace").splitlines()
        if any(marker in line.lower() for line in lines for marker in ERROR_MARKERS):
            implicated[log_file.stem] = lines[-tail_lines:]
        if len(implicated) >= MAX_IMPLICATED_CONTAINERS:
            break
    return implicated


def build_message(status, tail_lines):
    event_name = os.environ.get("GITHUB_EVENT_NAME", "unknown")
    run_url = (
        f"{os.environ.get('GITHUB_SERVER_URL', '')}/{os.environ.get('GITHUB_REPOSITORY', '')}"
        f"/actions/runs/{os.environ.get('GITHUB_RUN_ID', '')}"
    )
    duration = format_duration(os.environ.get("RUN_STARTED_AT"))
    core_ref = os.environ.get("CORE_REPO_REF") or "(config/repos.py default)"
    signer_ref = os.environ.get("SIGNER_REPO_REF") or "(config/repos.py default)"
    seeder_ref = os.environ.get("SEEDER_REPO_REF") or "(config/repos.py default)"

    emoji = "✅" if status == "pass" else "❌"
    lines = [
        f"{emoji} *Weekly Cross-Repo Integration Test: {status.upper()}*",
        f"Trigger: `{event_name}` | Duration: {duration} | <{run_url}|Run link>",
        f"tapyrus-core: `{core_ref}` | tapyrus-signer: `{signer_ref}` | tapyrus-seeder: `{seeder_ref}`",
    ]

    aggpubkey_a = read_aggpubkey("signer-set-a")
    aggpubkey_b = read_aggpubkey("signer-set-b")
    if aggpubkey_a:
        lines.append(f"signer-set-a aggpubkey: `{aggpubkey_a}`")
    if aggpubkey_b:
        lines.append(f"signer-set-b aggpubkey (post-rotation): `{aggpubkey_b}`")

    if status != "pass":
        implicated = find_implicated_containers(tail_lines)
        if implicated:
            lines.append(f"Implicated container(s) (last {tail_lines} line(s) with an error/panic hit):")
            for name, tail in implicated.items():
                snippet = "\n".join(tail)
                lines.append(f"*{name}*:\n```{snippet}```")
        else:
            lines.append("No container log matched an error/panic pattern -- see the full logs artifact.")

    return "\n".join(lines)


def send(webhook_url, text):
    body = json.dumps({"text": text}).encode()
    request = urllib.request.Request(
        webhook_url, data=body, headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.status, response.read().decode()


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in ("pass", "fail"):
        log.error("usage: send_slack_report.py <pass|fail>")
        sys.exit(2)
    status = sys.argv[1]

    webhook_url = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
    if not webhook_url:
        log.warn("SLACK_WEBHOOK_URL not set -- skipping Slack report (not provisioned yet, see doc/project-plan.md)")
        return

    tail_lines = int(os.environ.get("SLACK_LOG_TAIL_LINES", DEFAULT_LOG_TAIL_LINES))
    text = build_message(status, tail_lines)

    log.step(f"sending {status} report to Slack")
    try:
        http_status, response_body = send(webhook_url, text)
    except (urllib.error.URLError, TimeoutError) as exc:
        # A failed notification shouldn't retroactively fail an otherwise-passing run
        # (this is the very last step) -- logged loudly, not raised.
        log.error(f"failed to send Slack report: {exc}")
        return
    if http_status != 200:
        log.error(f"Slack webhook returned HTTP {http_status}: {response_body}")
        return
    log.info("Slack report sent")


if __name__ == "__main__":
    main()
