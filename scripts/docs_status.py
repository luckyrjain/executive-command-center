"""Render and validate the canonical phase-status projections."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Final

REGISTRY_PATH: Final = Path("docs/phases/status.json")
TARGETS: Final = (Path("README.md"), Path("docs/ROADMAP.md"), Path("docs/phases/README.md"))
BEGIN_MARKER: Final = "<!-- BEGIN GENERATED PHASE STATUS -->"
END_MARKER: Final = "<!-- END GENERATED PHASE STATUS -->"

STATE_VALUES: Final = {
    "specification_status": {
        "draft",
        "review",
        "approved_for_implementation",
        "implemented",
        "deprecated",
        "archived",
    },
    "engineering_status": {
        "not_started",
        "in_progress",
        "engineering_complete",
        "not_applicable",
    },
    "validation_status": {
        "not_started",
        "in_progress",
        "passed",
        "failed",
        "accepted_with_limitations",
        "not_applicable",
    },
    "promotion_status": {"not_promoted", "promoted", "blocked", "not_applicable"},
}

REQUIRED_PHASE_FIELDS: Final = {
    "id",
    "number",
    "name",
    "specification",
    "specification_status",
    "engineering_status",
    "engineering_summary",
    "validation_status",
    "promotion_status",
    "open_gates",
    "implementation_status",
    "last_verified_commit",
}


def load_registry(path: Path) -> dict[str, object]:
    """Load a registry JSON object or raise a user-readable ValueError."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot load {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path}: top-level value must be an object")
    return data


def _referenced_path_error(root: Path, phase_id: str, field: str, value: object) -> str | None:
    if value is None and field == "implementation_status":
        return None
    if not isinstance(value, str) or not value:
        return f"{phase_id}: {field} must be a non-empty repository-relative path"
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        return f"{phase_id}: {field} must be a repository-relative path: {value}"
    if not (root / path).is_file():
        return f"{phase_id}: missing {field}: {value}"
    return None


def validate_registry(
    data: object, repo_root: Path, *, require_full_coverage: bool = True
) -> list[str]:
    """Return all structural registry errors in deterministic order."""
    errors: list[str] = []
    if not isinstance(data, dict):
        return ["registry must be a JSON object"]
    if data.get("schema_version") != 1:
        errors.append("schema_version must equal 1")
    phases = data.get("phases")
    if not isinstance(phases, list):
        errors.append("phases must be an array")
        return errors

    seen_ids: set[str] = set()
    seen_numbers: set[int] = set()
    for index, raw_phase in enumerate(phases):
        if not isinstance(raw_phase, dict):
            errors.append(f"phase at index {index} must be an object")
            continue
        phase_id = raw_phase.get("id")
        label = phase_id if isinstance(phase_id, str) and phase_id else f"phase at index {index}"
        missing = sorted(REQUIRED_PHASE_FIELDS - raw_phase.keys())
        if missing:
            errors.append(f"{label}: missing fields: {', '.join(missing)}")

        if isinstance(phase_id, str):
            if phase_id in seen_ids:
                errors.append(f"duplicate phase id: {phase_id}")
            seen_ids.add(phase_id)
        else:
            errors.append(f"{label}: id must be a string")

        number = raw_phase.get("number")
        if isinstance(number, bool) or not isinstance(number, int):
            errors.append(f"{label}: number must be an integer")
        else:
            if number in seen_numbers:
                errors.append(f"duplicate phase number: {number}")
            seen_numbers.add(number)
            expected_id = f"PHASE-{number:03d}"
            if phase_id != expected_id:
                errors.append(f"{label}: id must equal {expected_id}")

        for field, allowed in STATE_VALUES.items():
            value = raw_phase.get(field)
            if value not in allowed:
                errors.append(f"{label}: invalid {field}: {value}")

        for field in ("name", "engineering_summary", "last_verified_commit"):
            value = raw_phase.get(field)
            if not isinstance(value, str) or not value:
                errors.append(f"{label}: {field} must be a non-empty string")

        open_gates = raw_phase.get("open_gates")
        if not isinstance(open_gates, list) or any(
            not isinstance(gate, str) or not gate for gate in open_gates
        ):
            errors.append(f"{label}: open_gates must be an array of non-empty strings")

        for field in ("specification", "implementation_status"):
            path_error = _referenced_path_error(repo_root, label, field, raw_phase.get(field))
            if path_error:
                errors.append(path_error)

    if require_full_coverage:
        expected = set(range(11))
        missing_numbers = sorted(expected - seen_numbers)
        extra_numbers = sorted(seen_numbers - expected)
        if missing_numbers or extra_numbers:
            detail: list[str] = []
            if missing_numbers:
                detail.append(f"missing: {', '.join(map(str, missing_numbers))}")
            if extra_numbers:
                detail.append(f"unexpected: {', '.join(map(str, extra_numbers))}")
            errors.append(f"phase coverage must be exactly 0-10; {'; '.join(detail)}")
    return sorted(set(errors))


def _display_state(value: object) -> str:
    return str(value).replace("_", " ").capitalize()


def _phase_rows(data: dict[str, object]) -> list[dict[str, object]]:
    phases = data.get("phases", [])
    if not isinstance(phases, list):
        return []
    rows = [phase for phase in phases if isinstance(phase, dict)]

    def phase_number(phase: dict[str, object]) -> int:
        number = phase.get("number")
        return number if isinstance(number, int) and not isinstance(number, bool) else -1

    return sorted(rows, key=phase_number)


def _registry_link(target: str) -> str:
    if target == "README.md":
        return "docs/phases/status.json"
    if target == "docs/ROADMAP.md":
        return "phases/status.json"
    return "status.json"


def render_status_block(data: dict[str, object], target: str) -> str:
    """Render the generated status block for one Markdown target."""
    phases = _phase_rows(data)
    active = [phase for phase in phases if phase.get("engineering_status") == "in_progress"]
    current = active[-1] if active else phases[-1]
    lines = [
        BEGIN_MARKER,
        f"**Current engineering phase:** Phase {current['number']} — {current['name']} "
        f"({_display_state(current['engineering_status'])}; {current['engineering_summary']}).",
        "",
        "| Phase | Name | Engineering | Validation | Promotion |",
        "|---:|---|---|---|---|",
    ]
    for phase in phases:
        lines.append(
            f"| {phase['number']} | {phase['name']} | "
            f"{_display_state(phase['engineering_status'])} | "
            f"{_display_state(phase['validation_status'])} | "
            f"{_display_state(phase['promotion_status'])} |"
        )

    lines.extend(["", "**Open gates:**", ""])
    for phase in phases:
        gates = phase.get("open_gates")
        if isinstance(gates, list) and gates:
            lines.append(f"- Phase {phase['number']}: {'; '.join(str(gate) for gate in gates)}.")
    lines.extend(
        [
            "",
            f"Source: [`docs/phases/status.json`]({_registry_link(target)}). "
            "Specification approval, engineering completion, validation, and promotion are "
            "independent states.",
            END_MARKER,
        ]
    )
    return "\n".join(lines)


def _replace_generated_block(text: str, rendered: str, target: Path) -> str:
    if text.count(BEGIN_MARKER) != 1 or text.count(END_MARKER) != 1:
        raise ValueError(f"{target}: expected exactly one generated phase status marker pair")
    before, rest = text.split(BEGIN_MARKER, 1)
    _, after = rest.split(END_MARKER, 1)
    return f"{before}{rendered}{after}"


def _expected_target(root: Path, target: Path, data: dict[str, object]) -> str:
    path = root / target
    try:
        current = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"{target}: cannot read target: {exc}") from exc
    return _replace_generated_block(current, render_status_block(data, target.as_posix()), target)


def run(command: str, root: Path) -> int:
    try:
        data = load_registry(root / REGISTRY_PATH)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    errors = validate_registry(data, root)
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1

    stale: list[Path] = []
    for target in TARGETS:
        try:
            expected = _expected_target(root, target, data)
        except ValueError as exc:
            print(exc, file=sys.stderr)
            return 1
        path = root / target
        current = path.read_text(encoding="utf-8")
        if current == expected:
            continue
        if command == "render":
            path.write_text(expected, encoding="utf-8")
        else:
            stale.append(target)

    for target in stale:
        print(f"{target}: generated phase status is stale", file=sys.stderr)
    return 1 if stale else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("render", "check"))
    parser.add_argument("--root", type=Path, default=Path(__file__).parents[1])
    args = parser.parse_args(argv)
    return run(args.command, args.root.resolve())


if __name__ == "__main__":
    raise SystemExit(main())
