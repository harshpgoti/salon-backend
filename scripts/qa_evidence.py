"""Run the QA-critical pytest subset and record reviewable evidence (TASK-056).

The subset is the security/load/fault slice that G-QA acceptance depends on.
Tests emit `[qa-evidence] {json}` lines; this runner forwards all output to the
CI log, collects the metric lines, records the runner environment next to them
and writes `qa-evidence.json` for the workflow artifact.

Exit status is 0 only when the subset passes and every required metric was
emitted. Connection URLs are never printed or persisted; only the fact that
DATABASE_URL was set is recorded.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

SUBSET = [
    "tests/test_security_suite.py",
    "tests/test_load_invariants.py",
    "tests/test_callbacks.py",
    "tests/test_quiet_fallback.py",
    "tests/test_budgets.py",
]
REQUIRED_METRICS = {"qa04.hold_race", "qa_mixed.fairness"}
MARKER = "[qa-evidence] "


def _memory_mb() -> float | None:
    try:
        with open("/proc/meminfo", encoding="ascii") as handle:
            for line in handle:
                if line.startswith("MemTotal:"):
                    return round(int(line.split()[1]) / 1024, 1)
    except OSError:
        return None
    return None


def _runner_environment() -> dict:
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "git_sha": os.environ.get("GITHUB_SHA", ""),
        "git_ref": os.environ.get("GITHUB_REF_NAME", ""),
        "run_id": os.environ.get("GITHUB_RUN_ID", ""),
        "job": os.environ.get("GITHUB_JOB", ""),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "cpu_count": os.cpu_count(),
        "memory_mb": _memory_mb(),
        "database_url_configured": bool(os.environ.get("DATABASE_URL")),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="qa-evidence.json")
    args = parser.parse_args()

    command = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-s", *SUBSET]
    print("qa-evidence: running", " ".join(command), flush=True)
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, encoding="utf-8", errors="replace")
    metrics: list[dict] = []
    summary_lines: list[str] = []
    assert process.stdout is not None
    for line in process.stdout:
        print(line, end="", flush=True)
        stripped = line.strip()
        if stripped.startswith(MARKER):
            try:
                metrics.append(json.loads(stripped[len(MARKER):]))
            except json.JSONDecodeError:
                print(f"qa-evidence: unparsable metric line ignored: {stripped}", flush=True)
        elif " passed" in stripped or " failed" in stripped or " error" in stripped:
            summary_lines.append(stripped)
    exit_code = process.wait()

    names = {metric.get("metric") for metric in metrics}
    missing = sorted(REQUIRED_METRICS - names)
    record = {
        "task": "TASK-056",
        "requirement": "FR-082",
        "runner": _runner_environment(),
        "subset": SUBSET,
        "pytest_exit_code": exit_code,
        "pytest_summary": summary_lines[-3:],
        "metrics": metrics,
        "missing_required_metrics": missing,
        "ok": exit_code == 0 and not missing,
    }
    Path(args.output).write_text(json.dumps(record, indent=2, sort_keys=True), encoding="utf-8")
    print(f"qa-evidence: wrote {args.output} (exit_code={exit_code}, metrics={sorted(names)})", flush=True)
    if exit_code != 0:
        print("qa-evidence: FAILED - QA-critical subset did not pass.", flush=True)
        return 1
    if missing:
        print(f"qa-evidence: FAILED - required metrics missing: {missing}", flush=True)
        return 1
    print("qa-evidence: PASS - subset green and all published invariants recorded.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
