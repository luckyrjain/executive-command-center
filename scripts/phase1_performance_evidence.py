"""Generate the Phase 1 performance-acceptance recorded-result evidence.

The `performance-acceptance` CI job (`.github/workflows/phase1-acceptance.yml`)
runs the representative-scale performance test suite and tees its output to
a human-readable log (`.local/evidence/phase1-performance.log`). That log is
useful for humans but is not machine-checkable: nothing records whether the
run passed, or which commit it ran against, in a form the Phase 1 acceptance
checker (`scripts/check_phase1_acceptance.py`) can validate.

This script closes that gap the same way Task 9's `scripts/phase1_evidence.py`
does for the backup/restore drill: it emits a small, structured JSON
recorded-result artifact -- status, head SHA, timestamp -- with no test
content (note bodies, fixture data, etc.), consistent with the project-wide
rule that evidence artifacts must not carry user content. The workflow step
determines pass/fail itself (from the test run's real exit code, captured
via `PIPESTATUS` since the run is piped through `tee`) and passes it in via
`--status`; this script does not re-run or second-guess the tests.

CLI usage:

    uv run python scripts/phase1_performance_evidence.py \\
        --status passed \\
        --output-json .local/evidence/phase1-performance.json

Exits 0 if `--status passed`, 1 if `--status failed`.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _git_head_sha(root: Path = Path(".")) -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    sha = result.stdout.strip()
    return sha or None


def build_report(*, status: str) -> dict[str, Any]:
    if status not in ("passed", "failed"):
        raise ValueError(f"status must be 'passed' or 'failed', got {status!r}")
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "status": status,
        "head_sha": _git_head_sha(),
    }


def write_report(report: dict[str, Any], output_json: Path) -> None:
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(report, indent=2, sort_keys=True))


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status", required=True, choices=["passed", "failed"])
    parser.add_argument(
        "--output-json",
        type=Path,
        default=Path(".local/evidence/phase1-performance.json"),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    report = build_report(status=args.status)
    write_report(report, args.output_json)
    print(f"Phase 1 performance evidence report: {report['status']}")
    print(f"  JSON: {args.output_json}")
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
