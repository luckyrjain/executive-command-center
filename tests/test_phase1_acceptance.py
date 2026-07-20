from __future__ import annotations

import importlib.util
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

CHECKER_PATH = Path("scripts/check_phase1_acceptance.py")
spec = importlib.util.spec_from_file_location("phase1_acceptance_checker", CHECKER_PATH)
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
validate: Callable[[dict[str, Any]], list[str]] = module.validate


def _document() -> dict[str, Any]:
    return json.loads(Path("config/phase1-acceptance.json").read_text())


def test_phase1_acceptance_contract_is_valid() -> None:
    assert validate(_document()) == []


def test_phase1_acceptance_rejects_relaxed_medium_gate() -> None:
    document = _document()
    document["final_gate"]["medium_findings"] = 1
    assert "final_gate.medium_findings must be 0, got 1" in validate(document)


# --- Result-aware recorded-result validation --------------------------------
#
# The checker must not stop at "does the evidence source file exist" -- for
# categories that produce a recorded-result artifact (a JSON document written
# by the command that actually ran the check), it must validate that the
# recorded result actually passed, ran against the current commit, and (for
# security-scan categories) stayed within accepted finding thresholds and
# recorded the scanned artifact's identity. These four tests exercise
# `validate_recorded_result` directly with hand-built recorded-result
# fixtures, independent of any real CI-produced artifact.


def test_recorded_result_missing_status_is_rejected() -> None:
    result = {"head_sha": "abc123"}
    errors = module.validate_recorded_result(result, label="result_evidence.backup_restore")
    assert any("status" in error for error in errors)


def test_recorded_result_rejects_stale_head_sha() -> None:
    result = {"status": "passed", "head_sha": "deadbeef"}
    errors = module.validate_recorded_result(
        result,
        label="result_evidence.backup_restore",
        current_head_sha="cafefeed",
    )
    assert any("stale" in error.lower() for error in errors)


def test_recorded_result_rejects_high_findings_over_threshold() -> None:
    result = {"status": "passed", "head_sha": "abc123", "high_findings": 3}
    errors = module.validate_recorded_result(
        result,
        label="result_evidence.container_scan",
        max_high_findings=0,
    )
    assert any("high_findings" in error for error in errors)


def test_recorded_result_rejects_missing_image_digest() -> None:
    result = {"status": "passed", "head_sha": "abc123", "high_findings": 0}
    errors = module.validate_recorded_result(
        result,
        label="result_evidence.container_scan",
        max_high_findings=0,
        require_image_digest=True,
    )
    assert any("image_digest" in error for error in errors)


def test_validate_flags_stale_recorded_result_when_wired_via_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Proves the wiring, not just the standalone function: validate()
    itself must read and validate recorded-result artifact content for any
    evidence category declared under `result_evidence`, when that artifact
    is present on disk -- not merely confirm the artifact file exists."""
    monkeypatch.setattr(module, "_current_head_sha", lambda root: "current-sha")
    document = _document()
    document["result_evidence"] = {
        "backup_restore": {
            "artifact": "phase1-recovery.json",
            "require_head_sha_match": True,
        }
    }
    (tmp_path / "phase1-recovery.json").write_text(
        json.dumps({"status": "passed", "head_sha": "stale-sha"})
    )
    errors = validate(document, root=tmp_path)
    assert any("stale" in error.lower() for error in errors)


# --- Documentation-assertion tests (Task 12) --------------------------------
#
# Task 12 is responsible for bringing five existing Phase 1 status documents
# (plus two new operational runbooks) into honest alignment with what Tasks
# 1-11 actually delivered, per .superpowers/sdd/progress.md and the
# corresponding .superpowers/sdd/task-N-review.md files. "Honest" cuts both
# ways: a document must never overclaim (say Phase 1, or either of its two
# still-open human gates -- the seven-day daily-use record and human change
# review -- is complete/done/shipped), and it must never underclaim (leave a
# release-gate checkbox unchecked when Tasks 1-11 already produced genuine,
# independently-reviewed evidence for it). These tests parse/grep the
# documents themselves; they do not import or execute any application code.

STATUS_DOCUMENTS = [
    Path("README.md"),
    Path("docs/ROADMAP.md"),
    Path("docs/phases/phase-001/IMPLEMENTATION-STATUS.md"),
    Path("docs/phases/phase-001/FINAL-ACCEPTANCE.md"),
    Path("docs/runbooks/PHASE-1-RELEASE-GATE.md"),
    Path("docs/runbooks/PHASE-1-DAILY-USE.md"),
    Path("docs/runbooks/PHASE-1-DEPLOYMENT.md"),
]

RELEASE_GATE_PATH = Path("docs/runbooks/PHASE-1-RELEASE-GATE.md")
DAILY_USE_PATH = Path("docs/runbooks/PHASE-1-DAILY-USE.md")

# Phrasing that would claim Phase 1 itself -- not just an individual
# engineering gate -- is finished. This is the hard, non-negotiable boundary
# from the task brief: no document may say this while the seven-day
# daily-use gate and human change review remain open.
_FORBIDDEN_COMPLETION_PATTERNS = [
    re.compile(r"phase\s*1\s+is\s+(?:now\s+)?(?:complete|done|shipped|finished)", re.IGNORECASE),
    re.compile(r"phase\s*1\s+has\s+(?:now\s+)?shipped", re.IGNORECASE),
    re.compile(r"ready\s+for\s+production\s+traffic", re.IGNORECASE),
    re.compile(r"phase\s*1\s+closure\s+is\s+complete", re.IGNORECASE),
]


def _parse_checklist(text: str) -> dict[str, bool]:
    """Map each markdown checklist item's label text to whether it is
    checked. A trailing evidence citation of the form "(Task N: ...)" or
    "(Tasks N-M: ...)" is stripped so the label matches the checklist text
    as originally authored, independent of whatever citation is appended."""
    items: dict[str, bool] = {}
    for match in re.finditer(r"^- \[([ xX])\] (.+)$", text, re.MULTILINE):
        checked = match.group(1).lower() == "x"
        label = re.sub(r"\s*\(Tasks? \d.*\)\s*$", "", match.group(2)).strip()
        items[label] = checked
    return items


def _parse_daily_use_rows(text: str) -> list[dict[str, str]]:
    """Parse the dated-row table body out of a PHASE-1-DAILY-USE.md-style
    markdown table. Returns one dict per data row (header and separator
    rows are skipped), keyed by column name."""
    table_lines = [line for line in text.splitlines() if line.strip().startswith("|")]
    if len(table_lines) < 3:
        return []
    header = [cell.strip() for cell in table_lines[0].strip("|").split("|")]
    rows = []
    for line in table_lines[2:]:
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) != len(header):
            continue
        rows.append(dict(zip(header, cells, strict=False)))
    return rows


_PLACEHOLDER_DATE_VALUES = {"", "-", "—", "tbd", "pending"}


def check_daily_use_gate(text: str) -> list[str]:
    """Standalone daily-use-gate validator, deliberately mirroring
    `validate_recorded_result`'s pattern above: reject a daily-use record
    that claims the gate is complete/closed/satisfied, or that has fewer
    than 7 genuinely dated (non-placeholder) rows filled in. Exercised both
    directly with hand-built fixtures and against the real runbook."""
    errors: list[str] = []
    if re.search(r"gate\s+is\s+(?:complete|closed|satisfied)\b", text, re.IGNORECASE):
        errors.append("daily-use record claims the gate is complete/closed/satisfied")
    rows = _parse_daily_use_rows(text)
    filled = [
        row
        for row in rows
        if row.get("Date", "").strip().casefold() not in _PLACEHOLDER_DATE_VALUES
        and not row.get("Date", "").strip().upper().startswith("YYYY")
    ]
    if len(filled) < 7:
        errors.append(f"only {len(filled)} of 7 required dated rows are filled in")
    return errors


def test_daily_use_gate_helper_rejects_fewer_than_seven_filled_rows() -> None:
    text = "\n".join(
        [
            "| Date | Notes |",
            "| --- | --- |",
            "| 2026-07-21 | ok |",
            "| 2026-07-22 | ok |",
        ]
    )
    errors = check_daily_use_gate(text)
    assert any("of 7" in error for error in errors)


def test_daily_use_gate_helper_rejects_claimed_completion_even_with_seven_rows() -> None:
    rows = "\n".join(f"| 2026-07-{20 + i:02d} | ok |" for i in range(7))
    text = "The seven-day daily-use gate is complete.\n\n| Date | Notes |\n| --- | --- |\n" + rows
    errors = check_daily_use_gate(text)
    assert any("complete" in error for error in errors)


def test_daily_use_gate_helper_accepts_seven_genuine_rows_with_no_completion_claim() -> None:
    rows = "\n".join(f"| 2026-07-{20 + i:02d} | ok |" for i in range(7))
    preamble = "The gate remains open until seven real days are recorded.\n\n"
    table_header = "| Date | Notes |\n| --- | --- |\n"
    text = preamble + table_header + rows
    assert check_daily_use_gate(text) == []


def test_daily_use_runbook_exists_and_remains_structurally_open() -> None:
    """The daily-use runbook must exist with the structure for seven dated
    rows, but Task 12 must not itself satisfy the gate: zero rows may be
    filled in, and the document must say the gate stays open until seven
    real days are recorded."""
    assert DAILY_USE_PATH.is_file(), "Task 12 must create docs/runbooks/PHASE-1-DAILY-USE.md"
    text = DAILY_USE_PATH.read_text()
    rows = _parse_daily_use_rows(text)
    assert len(rows) == 7, f"expected a 7-row table structure, found {len(rows)} rows"
    errors = check_daily_use_gate(text)
    assert errors, "the daily-use runbook must remain genuinely open (zero rows filled in)"
    assert any("0 of 7" in error for error in errors), errors
    assert re.search(
        r"remains open|not (?:yet )?(?:complete|satisfied|closed)", text, re.IGNORECASE
    )


def test_no_document_claims_completion_while_daily_use_gate_is_open() -> None:
    """Contradictory-status guard: nothing may claim Phase 1 (or its release)
    is complete/done/shipped while the seven-day daily-use gate has not
    independently closed. This is the "don't overclaim" direction."""
    gate_is_closed = DAILY_USE_PATH.is_file() and not check_daily_use_gate(
        DAILY_USE_PATH.read_text()
    )
    if gate_is_closed:
        pytest.skip("daily-use gate has closed; completion claims are no longer contradictory")
    violations = []
    for doc in STATUS_DOCUMENTS:
        if not doc.is_file():
            continue
        text = doc.read_text()
        for pattern in _FORBIDDEN_COMPLETION_PATTERNS:
            if pattern.search(text):
                violations.append(f"{doc}: matched {pattern.pattern!r}")
    assert violations == [], "\n".join(violations)


# Checklist items in PHASE-1-RELEASE-GATE.md that Tasks 1-11 (per
# .superpowers/sdd/progress.md and the corresponding task-N-review.md)
# produced genuine, independently-reviewed evidence for. Each must be
# checked, not left unchecked -- this is the "don't underclaim" direction.
RELEASE_GATE_ITEMS_WITH_EVIDENCE = (
    "Backend Ruff, formatting, mypy, Alembic and PostgreSQL tests pass.",
    "Frontend typecheck, unit tests, production build and Chromium acceptance pass.",
    "All lifecycle mutations preserve optimistic version checks, idempotency, CSRF and "
    "workspace isolation.",
    "Search, Audit, Today, Morning Brief, Recommendations and Work Actions pass acceptance "
    "coverage.",
    "Production configuration rejects insecure defaults.",
    "Security headers are emitted by the frontend and backend entry points.",
    "Request size and rate limits are defined for authenticated and mutation routes.",
    "Session cookies remain secure, HTTP-only and same-site constrained in production.",
    "Structured logs include request ID, correlation ID, workspace ID, route, status and duration.",
    "Health, readiness and version endpoints are documented and exercised.",
    "Metrics cover request count, latency, errors, database failures and outbox backlog.",
    "Sensitive note bodies, evidence payloads, session values and CSRF tokens never enter logs.",
    "PostgreSQL backup command and retention policy are documented.",
    "Restore is verified into an isolated database.",
    "Alembic head, row counts and representative workspace data are validated after restore.",
    "Recovery point objective and recovery time objective are recorded.",
    "A restore drill produces a timestamped evidence report.",
    "Keyboard navigation covers all interactive surfaces.",
    "Focus visibility, labels, landmarks, status and alert regions are validated.",
    "Automated accessibility checks report no serious or critical violations.",
    "Loading, empty, stale, degraded, conflict and error states remain recoverable.",
    "Deployment and rollback procedures are documented.",
    "Database migration rollback limitations are explicit.",
    "Environment variables and secret ownership are documented.",
    "Critical, High and Medium review findings are zero before merge.",
)

# Checklist items that genuinely cannot be proven live in this local
# environment, or that reflect a real currently-failing gate: Trivy's
# vulnerability-database scan results and `pnpm audit`'s network-dependent
# findings were actually run live in Task 12 (network access was available,
# contrary to task-11-review.md's assumption) and found real HIGH/CRITICAL
# findings in frontend dependencies and both container base images -- a
# genuine failing gate, not merely unverifiable; gitleaks and the SBOM step
# were not independently re-run locally. No live CI/CD pipeline exists yet
# to run post-deploy smoke checks automatically. These must stay unchecked,
# not be silently checked off.
#
# ("Backend Ruff, formatting, mypy, Alembic and PostgreSQL tests pass." was
# also in this list after Task 12 discovered a real, CI-config-reproducing
# test-isolation defect in tests/test_production_security.py's
# restore_main_module fixture -- fixed and independently re-reviewed in
# commit 87e12b2 (see task-ci-secret-fix-report.md), so it moved back to
# RELEASE_GATE_ITEMS_WITH_EVIDENCE above.)
RELEASE_GATE_ITEMS_STILL_OPEN = (
    "Dependency, secret, container and SBOM scans pass.",
    "Post-deployment smoke checks are automated.",
)


def test_release_gate_checks_items_with_genuine_evidence() -> None:
    text = RELEASE_GATE_PATH.read_text()
    items = _parse_checklist(text)
    missing = [label for label in RELEASE_GATE_ITEMS_WITH_EVIDENCE if label not in items]
    assert missing == [], f"checklist items not found verbatim in the document: {missing}"
    unchecked = [label for label in RELEASE_GATE_ITEMS_WITH_EVIDENCE if not items[label]]
    assert unchecked == [], (
        "these release-gate items have genuine Task 1-11 evidence recorded in "
        f".superpowers/sdd/ but are not checked off: {unchecked}"
    )


def test_release_gate_leaves_locally_unverifiable_items_open() -> None:
    text = RELEASE_GATE_PATH.read_text()
    items = _parse_checklist(text)
    missing = [label for label in RELEASE_GATE_ITEMS_STILL_OPEN if label not in items]
    assert missing == [], f"checklist items not found verbatim in the document: {missing}"
    wrongly_checked = [label for label in RELEASE_GATE_ITEMS_STILL_OPEN if items[label]]
    assert wrongly_checked == [], (
        "these items cannot be verified live in this environment (see "
        f"task-11-review.md) but are checked off: {wrongly_checked}"
    )


def test_release_gate_checked_items_cite_evidence() -> None:
    """Every checked item must cite the task/review that backs it -- a
    checkbox with no traceable source is exactly the kind of unverifiable
    claim this task exists to prevent."""
    text = RELEASE_GATE_PATH.read_text()
    checked_lines = [line for line in text.splitlines() if re.match(r"^- \[[xX]\] ", line)]
    assert len(checked_lines) == len(RELEASE_GATE_ITEMS_WITH_EVIDENCE)
    uncited = [line for line in checked_lines if not re.search(r"\(Tasks? \d", line)]
    assert uncited == [], f"checked items missing an evidence citation: {uncited}"
