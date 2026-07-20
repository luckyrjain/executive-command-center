from __future__ import annotations

import importlib.util
import json
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
