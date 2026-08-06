from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

CHECKER_PATH = Path("scripts/docs_status.py")
spec = importlib.util.spec_from_file_location("docs_status", CHECKER_PATH)
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)

render_status_block: Any = module.render_status_block
validate_registry: Any = module.validate_registry


def _phase(number: int, **overrides: object) -> dict[str, object]:
    phase: dict[str, object] = {
        "id": f"PHASE-{number:03d}",
        "number": number,
        "name": f"Phase {number}",
        "specification": f"docs/phases/PHASE-{number:03d}.md",
        "specification_status": "approved_for_implementation",
        "engineering_status": "engineering_complete",
        "engineering_summary": "Engineering complete",
        "validation_status": "not_started",
        "promotion_status": "not_promoted",
        "open_gates": ["operator validation"],
        "implementation_status": None,
        "last_verified_commit": "0919ca4fd7a5fbd6c2199606a61652cdc3ff9ade",
    }
    phase.update(overrides)
    return phase


def _registry(*phases: dict[str, object]) -> dict[str, object]:
    return {"schema_version": 1, "phases": list(phases)}


def _write_registry_files(root: Path, data: dict[str, object]) -> None:
    for phase in data["phases"]:  # type: ignore[index]
        specification = root / str(phase["specification"])
        specification.parent.mkdir(parents=True, exist_ok=True)
        specification.write_text("# Phase\n", encoding="utf-8")
        implementation_status = phase.get("implementation_status")
        if implementation_status:
            status_path = root / str(implementation_status)
            status_path.parent.mkdir(parents=True, exist_ok=True)
            status_path.write_text("# Status\n", encoding="utf-8")


def test_validate_registry_rejects_duplicate_phase_number(tmp_path: Path) -> None:
    duplicate = _phase(2)
    duplicate["number"] = 1
    data = _registry(_phase(1), duplicate)
    _write_registry_files(tmp_path, data)

    errors = validate_registry(data, tmp_path, require_full_coverage=False)

    assert "duplicate phase number: 1" in errors


@pytest.mark.parametrize(
    ("field", "value", "diagnostic"),
    [
        ("specification_status", "signed-off", "invalid specification_status: signed-off"),
        ("engineering_status", "done", "invalid engineering_status: done"),
        ("validation_status", "unknown", "invalid validation_status: unknown"),
        ("promotion_status", "released", "invalid promotion_status: released"),
    ],
)
def test_validate_registry_rejects_unknown_states(
    tmp_path: Path, field: str, value: str, diagnostic: str
) -> None:
    data = _registry(_phase(0, **{field: value}))
    _write_registry_files(tmp_path, data)

    errors = validate_registry(data, tmp_path, require_full_coverage=False)

    assert f"PHASE-000: {diagnostic}" in errors


def test_validate_registry_requires_full_phase_coverage(tmp_path: Path) -> None:
    data = _registry(_phase(0), _phase(2))
    _write_registry_files(tmp_path, data)

    errors = validate_registry(data, tmp_path)

    assert "phase coverage must be exactly 0-10; missing: 1, 3, 4, 5, 6, 7, 8, 9, 10" in errors


def test_render_status_block_separates_state_dimensions() -> None:
    data = _registry(
        _phase(
            10,
            name="Gmail Connector",
            engineering_status="in_progress",
            engineering_summary="Tasks 1-2 of 8 complete",
            open_gates=["Tasks 3-8", "real Gmail account verification"],
        )
    )

    block = render_status_block(data, "README.md")

    assert "| 10 | Gmail Connector | In progress | Not started | Not promoted |" in block
    assert "Tasks 1-2 of 8 complete" in block
    assert "Tasks 3-8" in block
    assert "real Gmail account verification" in block


def test_check_returns_nonzero_when_generated_block_is_stale(tmp_path: Path) -> None:
    data = _registry(*[_phase(number) for number in range(11)])
    _write_registry_files(tmp_path, data)
    registry_path = tmp_path / "docs/phases/status.json"
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text(json.dumps(data), encoding="utf-8")
    for target in ("README.md", "docs/ROADMAP.md", "docs/phases/README.md"):
        path = tmp_path / target
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "# Document\n\n<!-- BEGIN GENERATED PHASE STATUS -->\nstale\n"
            "<!-- END GENERATED PHASE STATUS -->\n",
            encoding="utf-8",
        )

    completed = subprocess.run(
        [sys.executable, "scripts/docs_status.py", "check", "--root", str(tmp_path)],
        cwd=Path(__file__).parents[1],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 1
    assert "README.md: generated phase status is stale" in completed.stderr


def test_render_then_check_is_idempotent(tmp_path: Path) -> None:
    data = _registry(*[_phase(number) for number in range(11)])
    _write_registry_files(tmp_path, data)
    registry_path = tmp_path / "docs/phases/status.json"
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text(json.dumps(data), encoding="utf-8")
    for target in ("README.md", "docs/ROADMAP.md", "docs/phases/README.md"):
        path = tmp_path / target
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "# Document\n\n<!-- BEGIN GENERATED PHASE STATUS -->\n"
            "<!-- END GENERATED PHASE STATUS -->\n",
            encoding="utf-8",
        )

    render = subprocess.run(
        [sys.executable, "scripts/docs_status.py", "render", "--root", str(tmp_path)],
        cwd=Path(__file__).parents[1],
        check=False,
        capture_output=True,
        text=True,
    )
    check = subprocess.run(
        [sys.executable, "scripts/docs_status.py", "check", "--root", str(tmp_path)],
        cwd=Path(__file__).parents[1],
        check=False,
        capture_output=True,
        text=True,
    )

    assert render.returncode == 0, render.stderr
    assert check.returncode == 0, check.stderr
