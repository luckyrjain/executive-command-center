from __future__ import annotations

import argparse
import json
import subprocess
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


def _current_head_sha(root: Path) -> str | None:
    """Return the current git HEAD commit SHA for `root`, or None if it
    cannot be determined (not a git checkout, git unavailable, etc.)."""
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


def validate_recorded_result(
    result: dict[str, Any],
    *,
    label: str,
    current_head_sha: str | None = None,
    max_high_findings: int | None = None,
    require_image_digest: bool = False,
) -> list[str]:
    """Validate the *content* of a recorded-result artifact (a JSON document
    written by the command/CI job that actually performed a Phase 1
    acceptance check), rather than merely checking that its source file
    exists.

    A recorded-result artifact is expected to carry, at minimum, a
    ``status`` ("passed"/"failed") and the ``head_sha`` of the commit it ran
    against -- so a stale result from a previous commit can't silently pass.
    Security-scan-style categories additionally carry an integer
    ``high_findings`` count (checked against an accepted threshold) and/or
    an ``image_digests`` mapping identifying the artifact(s) that were
    actually scanned.
    """
    errors: list[str] = []

    status = result.get("status")
    if status not in ("passed", "failed"):
        errors.append(f"{label}: recorded result is missing a valid status (passed/failed)")
    elif status != "passed":
        errors.append(f"{label}: recorded result status is {status!r}, expected 'passed'")

    head_sha = result.get("head_sha")
    if not head_sha or not isinstance(head_sha, str):
        errors.append(f"{label}: recorded result is missing head_sha")
    elif current_head_sha is not None and head_sha != current_head_sha:
        errors.append(
            f"{label}: recorded result head_sha {head_sha!r} does not match current "
            f"HEAD {current_head_sha!r} (stale result)"
        )

    if max_high_findings is not None:
        high_findings = result.get("high_findings")
        if not isinstance(high_findings, int) or isinstance(high_findings, bool):
            errors.append(f"{label}: recorded result is missing an integer high_findings count")
        elif high_findings > max_high_findings:
            errors.append(
                f"{label}: high_findings {high_findings} exceeds accepted threshold "
                f"{max_high_findings}"
            )

    if require_image_digest:
        image_digests = result.get("image_digests")
        if not isinstance(image_digests, dict) or not image_digests:
            errors.append(f"{label}: recorded result is missing image_digests")
        else:
            missing = sorted(image for image, digest in image_digests.items() if not digest)
            if missing:
                errors.append(f"{label}: recorded result is missing image_digest for: {missing}")

    return errors


def _resolve_within(root: Path, candidate: Path) -> Path | None:
    """Resolve `candidate`, returning it only if it stays within `root`.

    `--check-result`'s artifact path is a CLI argument, and `result_evidence`
    entries come from the checked-in config file -- both are authored by
    repo maintainers, not untrusted end users, but resolving strictly within
    the intended acceptance-evidence root closes off accidental or malicious
    path-traversal reads (CWE-23) outside that tree.
    """
    root_resolved = root.resolve()
    resolved = candidate.resolve()
    if resolved != root_resolved and root_resolved not in resolved.parents:
        return None
    return resolved


def check_result_evidence(
    document: dict[str, Any],
    category: str,
    artifact_path: Path,
    root: Path = Path("."),
) -> list[str]:
    """Validate a single recorded-result artifact against the
    `result_evidence.<category>` spec declared in the acceptance contract."""
    result_evidence = document.get("result_evidence", {})
    spec = result_evidence.get(category) if isinstance(result_evidence, dict) else None
    if not isinstance(spec, dict):
        return [f"result_evidence.{category} is not declared in the acceptance contract"]

    resolved_artifact = _resolve_within(root, artifact_path)
    if resolved_artifact is None:
        return [
            f"result_evidence.{category}: artifact path escapes the acceptance-evidence "
            f"root {root.resolve()}: {artifact_path}"
        ]
    if not resolved_artifact.is_file():
        return [f"result_evidence.{category}: recorded result artifact not found: {artifact_path}"]
    try:
        result = json.loads(resolved_artifact.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return [f"result_evidence.{category}: cannot read {artifact_path}: {exc}"]
    if not isinstance(result, dict):
        return [f"result_evidence.{category}: recorded result must be a JSON object"]

    require_head_sha_match = bool(spec.get("require_head_sha_match"))
    current_head_sha = _current_head_sha(root) if require_head_sha_match else None
    if require_head_sha_match and current_head_sha is None:
        # Fail closed: the spec explicitly asked for staleness verification,
        # but git is unavailable (no .git checkout, git not installed, or
        # the command failed) -- silently skipping the head_sha comparison
        # here (current_head_sha=None) would let validate_recorded_result's
        # `current_head_sha is not None and ...` guard no-op, letting an
        # arbitrarily stale recorded-result artifact pass unnoticed. That is
        # exactly the failure mode require_head_sha_match exists to catch.
        return [
            f"result_evidence.{category}: require_head_sha_match is set but the current "
            f"git HEAD SHA could not be determined for {root.resolve()} "
            "(not a git checkout, git unavailable, or the command failed) -- cannot verify "
            "the recorded result isn't stale"
        ]
    return validate_recorded_result(
        result,
        label=f"result_evidence.{category}",
        current_head_sha=current_head_sha,
        max_high_findings=spec.get("max_high_findings"),
        require_image_digest=bool(spec.get("require_image_digest")),
    )


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

    # Recorded-result artifacts (declared under `result_evidence`) prove that
    # an evidence category's underlying check actually ran, passed, and ran
    # against the current commit -- not just that its source file exists.
    # Each producing CI job (backup-restore, performance-acceptance,
    # containers) writes its artifact and, immediately after, invokes this
    # checker itself, guaranteeing the artifact is present at validation
    # time in that job. Outside those jobs (a fresh checkout, local dev
    # before running the underlying command, or the standalone
    # acceptance-contract job) the artifact is legitimately absent -- that
    # is not itself a failure here, only stale or failing *content* is.
    result_evidence = document.get("result_evidence", {})
    if result_evidence and not isinstance(result_evidence, dict):
        errors.append("result_evidence must be an object")
    elif isinstance(result_evidence, dict):
        for category, spec in sorted(result_evidence.items()):
            if not isinstance(spec, dict) or not spec.get("artifact"):
                errors.append(f"result_evidence.{category} must declare an artifact path")
                continue
            artifact_path = root / spec["artifact"]
            if not artifact_path.is_file():
                continue
            errors.extend(check_result_evidence(document, category, artifact_path, root=root))

    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "path",
        nargs="?",
        default="config/phase1-acceptance.json",
        type=Path,
    )
    parser.add_argument(
        "--check-result",
        nargs=2,
        metavar=("CATEGORY", "ARTIFACT"),
        help=(
            "Validate a single recorded-result artifact's content against "
            "the result_evidence.<CATEGORY> spec, instead of running the "
            "full acceptance-contract validation."
        ),
    )
    args = parser.parse_args()
    document = json.loads(args.path.read_text())

    if args.check_result:
        category, artifact = args.check_result
        errors = check_result_evidence(document, category, Path(artifact))
        if errors:
            for error in errors:
                print(f"ERROR: {error}")
            return 1
        print(f"Phase 1 recorded result for {category} is valid")
        return 0

    errors = validate(document)
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print("Phase 1 acceptance contract and executable evidence are valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
