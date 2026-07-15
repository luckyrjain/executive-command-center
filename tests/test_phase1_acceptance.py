from __future__ import annotations

import json
from pathlib import Path

from scripts.check_phase1_acceptance import validate


def test_phase1_acceptance_contract_is_valid() -> None:
    document = json.loads(Path("config/phase1-acceptance.json").read_text())
    assert validate(document) == []


def test_phase1_acceptance_rejects_relaxed_medium_gate() -> None:
    document = json.loads(Path("config/phase1-acceptance.json").read_text())
    document["final_gate"]["medium_findings"] = 1
    assert "final_gate.medium_findings must be 0, got 1" in validate(document)
