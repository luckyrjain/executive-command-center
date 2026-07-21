"""Tests for scripts/phase1_performance_evidence.py.

Pure logic (report building, exit-code mapping, git-SHA resolution) is
tested without any external process besides the real `git` binary already
required by the CI job this script runs in -- matching the convention used
by tests/test_phase1_evidence.py for its sibling script.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest


def _load_module() -> ModuleType:
    path = Path("scripts/phase1_performance_evidence.py")
    spec = importlib.util.spec_from_file_location("phase1_performance_evidence", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


evidence_module = _load_module()


def test_build_report_rejects_an_unrecognized_status() -> None:
    with pytest.raises(ValueError, match="passed.*failed"):
        evidence_module.build_report(status="ok")


@pytest.mark.parametrize("status", ["passed", "failed"])
def test_build_report_has_required_fields_for_each_status(status: str) -> None:
    report = evidence_module.build_report(status=status)

    assert report["status"] == status
    assert "generated_at" in report
    assert "head_sha" in report


def test_build_report_resolves_the_real_git_head_sha() -> None:
    report = evidence_module.build_report(status="passed")

    expected = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert report["head_sha"] == expected


def test_git_head_sha_returns_none_when_git_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(*_args: object, **_kwargs: object) -> None:
        raise FileNotFoundError("git not found")

    monkeypatch.setattr(evidence_module.subprocess, "run", fake_run)

    assert evidence_module._git_head_sha() is None


def test_git_head_sha_returns_none_when_not_inside_a_git_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(*args: object, **_kwargs: object) -> None:
        raise subprocess.CalledProcessError(returncode=128, cmd=args[0])

    monkeypatch.setattr(evidence_module.subprocess, "run", fake_run)

    assert evidence_module._git_head_sha() is None


def test_write_report_creates_parent_directories_and_writes_valid_json(
    tmp_path: Path,
) -> None:
    report = evidence_module.build_report(status="passed")
    output_json = tmp_path / "nested" / "evidence.json"

    evidence_module.write_report(report, output_json)

    assert output_json.is_file()
    assert json.loads(output_json.read_text()) == report


def test_main_exits_zero_and_writes_evidence_when_status_is_passed(tmp_path: Path) -> None:
    output_json = tmp_path / "phase1-performance.json"

    exit_code = evidence_module.main(["--status", "passed", "--output-json", str(output_json)])

    assert exit_code == 0
    assert json.loads(output_json.read_text())["status"] == "passed"


def test_main_exits_nonzero_when_status_is_failed(tmp_path: Path) -> None:
    output_json = tmp_path / "phase1-performance.json"

    exit_code = evidence_module.main(["--status", "failed", "--output-json", str(output_json)])

    assert exit_code == 1
    assert json.loads(output_json.read_text())["status"] == "failed"


def test_main_rejects_a_status_outside_the_recognized_choices() -> None:
    with pytest.raises(SystemExit):
        evidence_module.main(["--status", "ok"])
