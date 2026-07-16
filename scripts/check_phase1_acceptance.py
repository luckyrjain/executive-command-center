from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

REQUIRED_BUDGETS = {
    "performance.dashboard_p95_ms_local": 2000,
    "performance.search_p95_ms_local": 500,
    "performance.search_p95_ms_ci": 800,
    "performance.ranking_10000_entities_ms": 500,
    "backup_restore.postgres_major": 18,
    "backup_restore.rto_seconds": 600,
    "final_gate.critical_findings": 0,
    "final_gate.high_findings": 0,
    "final_gate.medium_findings": 0,
    "final_gate.critical_dependency_vulnerabilities": 0,
}

REQUIRED_EVIDENCE = {
    "acceptance_contract",
    "search_performance",
    "priority_performance",
    "browser_acceptance",
    "backup",
    "restore",
    "restore_verification",
    "acceptance_workflow",
    "standard_ci_workflow",
}


def _get(document: dict[str, Any], dotted_key: str) -> Any:
    value: Any = document
    for part in dotted_key.split("."):
        if not isinstance(value, dict) or part not in value:
            raise KeyError(dotted_key)
        value = value[part]
    return value


def validate(document: dict[str, Any], root: Path = Path(".")) -> list[str]:
    errors: list[str] = []
    if document.get("schema_version") != 1:
        errors.append("schema_version must be 1")

    for key, expected in REQUIRED_BUDGETS.items():
        try:
            actual = _get(document, key)
        except KeyError:
            errors.append(f"missing required budget: {key}")
            continue
        if actual != expected:
            errors.append(f"{key} must be {expected}, got {actual}")

    accessibility = document.get("accessibility", {})
    if accessibility.get("standard") != "WCAG 2.2 AA":
        errors.append("accessibility.standard must be WCAG 2.2 AA")
    for flag in ("keyboard_core_flows", "visible_focus", "document_title_required"):
        if accessibility.get(flag) is not True:
            errors.append(f"accessibility.{flag} must be true")

    backup = document.get("backup_restore", {})
    for flag in ("migration_head_must_match", "row_counts_must_match"):
        if backup.get(flag) is not True:
            errors.append(f"backup_restore.{flag} must be true")

    final_gate = document.get("final_gate", {})
    for flag in (
        "workspace_isolation_required",
        "audit_coverage_required",
        "ai_disabled_acceptance_required",
    ):
        if final_gate.get(flag) is not True:
            errors.append(f"final_gate.{flag} must be true")

    evidence = document.get("evidence", {})
    if not isinstance(evidence, dict):
        errors.append("evidence must be an object")
    else:
        for key in sorted(REQUIRED_EVIDENCE):
            value = evidence.get(key)
            if not isinstance(value, str) or not value:
                errors.append(f"missing required evidence: {key}")
                continue
            if not (root / value).is_file():
                errors.append(f"evidence file does not exist: {value}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "path",
        nargs="?",
        default="config/phase1-acceptance.json",
        type=Path,
    )
    args = parser.parse_args()
    document = json.loads(args.path.read_text())
    errors = validate(document)
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print("Phase 1 acceptance contract and executable evidence are valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
