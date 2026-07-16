from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any, Callable

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
