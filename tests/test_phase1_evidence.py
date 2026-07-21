"""Tests for scripts/phase1_evidence.py.

The pure invariant-evaluation logic is tested without a database. The
end-to-end report-building test requires real PostgreSQL (it queries
alembic_version, table counts, and fixture checksums), so it is skipped
unless ECC_DATABASE_URL points at PostgreSQL, matching the convention used
throughout tests/test_*_postgres.py.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from hashlib import sha256
from pathlib import Path
from types import ModuleType

import pytest

from ecc.config import get_settings


def _load_module() -> ModuleType:
    path = Path("scripts/phase1_evidence.py")
    spec = importlib.util.spec_from_file_location("phase1_evidence", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


evidence_module = _load_module()


def _passing_kwargs() -> dict:
    counts = {"tasks": 4, "notes": 4}
    checksums = {
        "tasks": "abc",
        "audit_events": "def",
        "pkos_nodes": "ghi",
        "pkos_edges": "jkl",
        "pkos_evidence": "mno",
    }
    return {
        "archive_checksum_ok": True,
        "archive_checksum_detail": "sha256=deadbeef",
        "source_revision": "0009_phase1_recommendations",
        "target_revision": "0009_phase1_recommendations",
        "source_counts": dict(counts),
        "target_counts": dict(counts),
        "source_checksums": dict(checksums),
        "target_checksums": dict(checksums),
        "elapsed_seconds": 12.5,
    }


def test_evaluate_invariants_all_pass_when_source_and_target_agree() -> None:
    invariants = evidence_module.evaluate_invariants(**_passing_kwargs())
    assert invariants
    assert all(item["passed"] for item in invariants)
    names = {item["name"] for item in invariants}
    assert {
        "archive_sha256",
        "alembic_head_matches",
        "row_counts_match",
        "representative_record_checksums_match",
        "audit_events_append_only",
        "pkos_mapped_columns_match",
        "rto_within_budget",
    } <= names


def test_evaluate_invariants_flags_row_count_mismatch() -> None:
    kwargs = _passing_kwargs()
    kwargs["target_counts"]["tasks"] = 3
    invariants = evidence_module.evaluate_invariants(**kwargs)
    by_name = {item["name"]: item for item in invariants}
    assert by_name["row_counts_match"]["passed"] is False
    assert not all(item["passed"] for item in invariants)


def test_evaluate_invariants_flags_audit_events_checksum_mismatch() -> None:
    kwargs = _passing_kwargs()
    kwargs["target_checksums"]["audit_events"] = "tampered"
    invariants = evidence_module.evaluate_invariants(**kwargs)
    by_name = {item["name"]: item for item in invariants}
    assert by_name["audit_events_append_only"]["passed"] is False
    assert by_name["representative_record_checksums_match"]["passed"] is False


def test_evaluate_invariants_flags_vacuous_checksums_when_both_sides_are_empty() -> None:
    """Regression test: fixture_row_checksums emits 'empty' when a table has
    no seeded rows. Two databases both missing seed data would both show
    'empty' for every table -- source == target -- which must NOT be
    treated as a passing checksum comparison, since it proves nothing about
    whether the restore actually preserved data."""
    kwargs = _passing_kwargs()
    kwargs["source_checksums"]["tasks"] = "empty"
    kwargs["target_checksums"]["tasks"] = "empty"
    invariants = evidence_module.evaluate_invariants(**kwargs)
    by_name = {item["name"]: item for item in invariants}
    assert by_name["representative_record_checksums_match"]["passed"] is False
    # audit_events/pkos checksums are still real, non-empty values in this
    # scenario, so those two invariants are unaffected by the "tasks" gap.
    assert by_name["audit_events_append_only"]["passed"] is True
    assert by_name["pkos_mapped_columns_match"]["passed"] is True


def test_evaluate_invariants_flags_vacuous_audit_events_checksum() -> None:
    kwargs = _passing_kwargs()
    kwargs["source_checksums"]["audit_events"] = "empty"
    kwargs["target_checksums"]["audit_events"] = "empty"
    invariants = evidence_module.evaluate_invariants(**kwargs)
    by_name = {item["name"]: item for item in invariants}
    assert by_name["audit_events_append_only"]["passed"] is False


def test_evaluate_invariants_flags_vacuous_pkos_checksum() -> None:
    kwargs = _passing_kwargs()
    kwargs["source_checksums"]["pkos_nodes"] = "empty"
    kwargs["target_checksums"]["pkos_nodes"] = "empty"
    invariants = evidence_module.evaluate_invariants(**kwargs)
    by_name = {item["name"]: item for item in invariants}
    assert by_name["pkos_mapped_columns_match"]["passed"] is False


def test_evaluate_invariants_flags_rto_budget_exceeded() -> None:
    kwargs = _passing_kwargs()
    kwargs["elapsed_seconds"] = 601
    invariants = evidence_module.evaluate_invariants(**kwargs)
    by_name = {item["name"]: item for item in invariants}
    assert by_name["rto_within_budget"]["passed"] is False


def test_evaluate_invariants_flags_revision_mismatch() -> None:
    kwargs = _passing_kwargs()
    kwargs["target_revision"] = "0008_phase1_morning_briefs"
    invariants = evidence_module.evaluate_invariants(**kwargs)
    by_name = {item["name"]: item for item in invariants}
    assert by_name["alembic_head_matches"]["passed"] is False


def test_render_markdown_never_includes_a_content_placeholder_marker() -> None:
    invariants = evidence_module.evaluate_invariants(**_passing_kwargs())
    report = {
        "generated_at": "2026-07-17T00:00:00+00:00",
        "elapsed_seconds": 12.5,
        "rto_budget_seconds": 600,
        "revisions": {"source": "x", "target": "x"},
        "row_counts": {"source": {"tasks": 4}, "target": {"tasks": 4}},
        "checksums": {"source": {"tasks": "abc"}, "target": {"tasks": "abc"}},
        "invariants": invariants,
        "passed": True,
    }
    markdown = evidence_module.render_markdown(report)
    json_blob = json.dumps(report)
    # The report must never contain seeded content -- only counts,
    # checksums, revisions, and timestamps. There is no seed-marker
    # constant anywhere in either serialization.
    assert "Phase1SeedMarker" not in markdown
    assert "Phase1SeedMarker" not in json_blob


def test_write_reports_writes_both_json_and_markdown(tmp_path: Path) -> None:
    invariants = evidence_module.evaluate_invariants(**_passing_kwargs())
    report = {
        "generated_at": "2026-07-17T00:00:00+00:00",
        "elapsed_seconds": 12.5,
        "rto_budget_seconds": 600,
        "revisions": {"source": "x", "target": "x"},
        "row_counts": {"source": {"tasks": 4}, "target": {"tasks": 4}},
        "checksums": {"source": {"tasks": "abc"}, "target": {"tasks": "abc"}},
        "invariants": invariants,
        "passed": True,
    }
    json_path = tmp_path / "nested" / "evidence.json"
    md_path = tmp_path / "nested" / "evidence.md"
    evidence_module.write_reports(report, json_path, md_path)

    assert json_path.is_file()
    assert md_path.is_file()
    loaded = json.loads(json_path.read_text())
    required_fields = (
        "generated_at",
        "elapsed_seconds",
        "revisions",
        "row_counts",
        "checksums",
        "invariants",
    )
    for field in required_fields:
        assert field in loaded


def test_main_exits_nonzero_when_an_invariant_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    failing_report = {
        "generated_at": "2026-07-17T00:00:00+00:00",
        "elapsed_seconds": 601,
        "rto_budget_seconds": 600,
        "revisions": {"source": "a", "target": "b"},
        "row_counts": {"source": {}, "target": {}},
        "checksums": {"source": {}, "target": {}},
        "invariants": [{"name": "rto_within_budget", "passed": False, "detail": "too slow"}],
        "passed": False,
    }
    monkeypatch.setattr(evidence_module, "build_report", lambda **_kwargs: failing_report)
    archive = tmp_path / "archive.dump"
    archive.write_bytes(b"irrelevant-for-this-test")

    exit_code = evidence_module.main(
        [
            "--source-url",
            "postgresql://ignored",
            "--target-url",
            "postgresql://ignored",
            "--archive",
            str(archive),
            "--elapsed-seconds",
            "601",
            "--output-json",
            str(tmp_path / "out.json"),
            "--output-md",
            str(tmp_path / "out.md"),
        ]
    )
    assert exit_code == 1


def test_main_exits_zero_when_every_invariant_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    passing_report = {
        "generated_at": "2026-07-17T00:00:00+00:00",
        "elapsed_seconds": 5,
        "rto_budget_seconds": 600,
        "revisions": {"source": "a", "target": "a"},
        "row_counts": {"source": {}, "target": {}},
        "checksums": {"source": {}, "target": {}},
        "invariants": [{"name": "rto_within_budget", "passed": True, "detail": "fine"}],
        "passed": True,
    }
    monkeypatch.setattr(evidence_module, "build_report", lambda **_kwargs: passing_report)
    archive = tmp_path / "archive.dump"
    archive.write_bytes(b"irrelevant-for-this-test")

    exit_code = evidence_module.main(
        [
            "--source-url",
            "postgresql://ignored",
            "--target-url",
            "postgresql://ignored",
            "--archive",
            str(archive),
            "--elapsed-seconds",
            "5",
            "--output-json",
            str(tmp_path / "out.json"),
            "--output-md",
            str(tmp_path / "out.md"),
        ]
    )
    assert exit_code == 0


settings = get_settings()
pytestmark_pg = pytest.mark.skipif(
    not settings.database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)


@pytestmark_pg
def test_build_report_against_real_database_has_required_fields(tmp_path: Path) -> None:
    pg_url = settings.database_url
    if pg_url.startswith("postgresql+psycopg://"):
        pg_url = pg_url.replace("postgresql+psycopg://", "postgresql://", 1)

    archive = tmp_path / "archive.dump"
    archive.write_bytes(b"fake archive contents for evidence field test")
    digest = sha256(archive.read_bytes()).hexdigest()
    archive.with_name(archive.name + ".sha256").write_text(f"{digest}  {archive.name}\n")

    report = evidence_module.build_report(
        source_url=pg_url,
        target_url=pg_url,
        archive_path=archive,
        elapsed_seconds=3.14,
    )

    for field in (
        "generated_at",
        "elapsed_seconds",
        "rto_budget_seconds",
        "revisions",
        "row_counts",
        "checksums",
        "invariants",
        "passed",
    ):
        assert field in report

    assert report["revisions"]["source"] == report["revisions"]["target"]
    assert report["passed"] is True
    serialized = json.dumps(report)
    assert "Phase1SeedMarker" not in serialized
