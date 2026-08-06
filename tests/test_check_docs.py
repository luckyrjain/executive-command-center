from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path("scripts").resolve()
sys.path.insert(0, str(SCRIPTS_DIR))
CHECKER_PATH = SCRIPTS_DIR / "check_docs.py"
spec = importlib.util.spec_from_file_location("check_docs", CHECKER_PATH)
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)

github_anchor: Any = module.github_anchor
parse_frontmatter: Any = module.parse_frontmatter
validate_repository: Any = module.validate_repository


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _frontmatter(*, document_id: str, status: str, title: str = "Example") -> str:
    return (
        "---\n"
        f"id: {document_id}\n"
        f"title: {title}\n"
        f"status: {status}\n"
        "version: 1.0.0\n"
        "owner: Lucky Jain\n"
        "---\n\n"
    )


def test_parse_frontmatter_reads_scalars_and_lists(tmp_path: Path) -> None:
    path = tmp_path / "contract.md"
    _write(
        path,
        _frontmatter(document_id="PHASE-001", status="Approved for Implementation").replace(
            "---\n\n", "contracts:\n  - phase-001/DATA-MODEL.md\n---\n\n"
        )
        + "# Contract\n",
    )

    metadata = parse_frontmatter(path)

    assert metadata["id"] == "PHASE-001"
    assert metadata["contracts"] == ["phase-001/DATA-MODEL.md"]


def test_rejects_missing_governed_metadata(tmp_path: Path) -> None:
    _write(tmp_path / "docs/adr/ADR-0001-example.md", "# Example\n")

    errors = validate_repository(tmp_path)

    assert errors == [
        "docs/adr/ADR-0001-example.md: missing front matter fields: "
        "id, owner, status, title, version"
    ]


def test_rejects_invalid_lifecycle_status(tmp_path: Path) -> None:
    _write(
        tmp_path / "docs/adr/ADR-0001-example.md",
        _frontmatter(document_id="ADR-0001", status="Approved") + "# Example\n",
    )

    errors = validate_repository(tmp_path)

    assert errors == [
        "docs/adr/ADR-0001-example.md: status 'Approved' is invalid for ADR; "
        "allowed: Accepted, Archived, Implemented, Proposed, Superseded"
    ]


def test_rejects_broken_relative_heading_anchor(tmp_path: Path) -> None:
    _write(tmp_path / "docs/a.md", "[target](b.md#missing)\n")
    _write(tmp_path / "docs/b.md", "# Present\n")

    errors = validate_repository(tmp_path)

    assert "docs/a.md: broken anchor b.md#missing" in errors


def test_accepts_duplicate_github_heading_anchor(tmp_path: Path) -> None:
    _write(tmp_path / "docs/a.md", "[target](b.md#repeat-1)\n")
    _write(tmp_path / "docs/b.md", "# Repeat\n\n# Repeat\n")

    assert validate_repository(tmp_path) == []


def test_ignores_links_in_fences_and_external_links(tmp_path: Path) -> None:
    _write(
        tmp_path / "docs/a.md",
        "[web](https://example.com) [mail](mailto:owner@example.com)\n\n"
        "```markdown\n[not real](missing.md)\n```\n",
    )

    assert validate_repository(tmp_path) == []


def test_github_anchor_matches_punctuation_and_inline_code() -> None:
    assert github_anchor("API `GET /items`: Errors & Recovery") == "api-get-items-errors--recovery"


def test_rejects_missing_approved_phase_contract(tmp_path: Path) -> None:
    _write(
        tmp_path / "docs/phases/PHASE-001-example.md",
        _frontmatter(document_id="PHASE-001", status="Approved for Implementation").replace(
            "---\n\n", "contracts:\n  - phase-001/DATA-MODEL.md\n---\n\n"
        )
        + "# Phase\n",
    )

    errors = validate_repository(tmp_path)

    assert "docs/phases/PHASE-001-example.md: missing contract: phase-001/DATA-MODEL.md" in errors


def test_rejects_dead_local_agent_evidence_reference(tmp_path: Path) -> None:
    _write(tmp_path / "docs/a.md", "Evidence: `.superpowers/sdd/task-1-review.md`.\n")

    errors = validate_repository(tmp_path)

    assert errors == ["docs/a.md: dead local-agent evidence reference: .superpowers/sdd/"]


def test_allows_governance_rule_that_names_evidence_directory(tmp_path: Path) -> None:
    _write(
        tmp_path / "docs/a.md",
        "Do not cite `.superpowers/sdd/` as durable evidence.\n",
    )

    assert validate_repository(tmp_path) == []


def test_approved_phase_requires_contract_metadata(tmp_path: Path) -> None:
    _write(
        tmp_path / "docs/phases/PHASE-010-example.md",
        _frontmatter(document_id="PHASE-010", status="Approved for Implementation") + "# Phase\n",
    )

    errors = validate_repository(tmp_path)

    assert errors == ["docs/phases/PHASE-010-example.md: approved phase has no contracts metadata"]


def test_closed_consistency_review_is_a_validation_record(tmp_path: Path) -> None:
    _write(
        tmp_path / "docs/phases/phase-001/CONSISTENCY-REVIEW.md",
        _frontmatter(document_id="PHASE-001-CONSISTENCY-REVIEW", status="Closed") + "# Review\n",
    )

    assert validate_repository(tmp_path) == []


def test_rejects_missing_environment_alias(tmp_path: Path) -> None:
    _write(
        tmp_path / "backend/ecc/config.py",
        'feature: bool = Field(default=False, validation_alias="ECC_FEATURE")\n',
    )
    _write(tmp_path / ".env.example", "ECC_ENV=development\n")

    errors = validate_repository(tmp_path)

    assert errors == ["ECC_FEATURE is missing from .env.example"]


def test_rejects_legacy_authoritative_inheritance_target(tmp_path: Path) -> None:
    _write(
        tmp_path / "docs/00-document-control.md",
        "## Document Inheritance\n\n- `02-design-principles.md`\n",
    )

    errors = validate_repository(tmp_path)

    assert errors == [
        "docs/00-document-control.md: legacy authoritative inheritance target: "
        "02-design-principles.md"
    ]
