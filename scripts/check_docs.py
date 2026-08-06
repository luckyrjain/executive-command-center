"""Validate repository documentation structure and governance contracts."""

from __future__ import annotations

import argparse
import re
import sys
import unicodedata
from collections.abc import Iterable
from pathlib import Path
from typing import Final
from urllib.parse import unquote

try:
    from scripts import docs_status
except ModuleNotFoundError:  # Direct execution adds scripts/, not the repository root, to sys.path.
    import docs_status  # type: ignore[no-redef,import-not-found]

REQUIRED_METADATA: Final = {"id", "title", "status", "version", "owner"}
NORMATIVE_STATUSES: Final = {
    "Draft",
    "Review",
    "Approved",
    "Implemented",
    "Deprecated",
    "Archived",
}
PHASE_STATUSES: Final = {
    "Draft",
    "Review",
    "Approved for Implementation",
    "Implemented",
    "Deprecated",
    "Archived",
}
ADR_STATUSES: Final = {"Proposed", "Accepted", "Implemented", "Superseded", "Archived"}
IMPLEMENTATION_STATUSES: Final = {"Planned", "Active", "Closed", "Archived"}
RUNBOOK_STATUSES: Final = {"Draft", "Active", "Open", "Closed", "Archived"}
PLANNING_STATUSES: Final = {"Draft", "Approved", "Implemented", "Superseded", "Archived"}

LEGACY_AUTHORITY_TARGETS: Final = {
    "01-product-definition.md",
    "02-design-principles.md",
    "03-system-architecture.md",
    "04-approved-technology-registry.md",
    "05-repository-standards.md",
    "06-ai-architecture.md",
    "07-security-architecture.md",
    "08-local-first-architecture.md",
    "09-observability.md",
    "10-development-process.md",
}

LINK_RE: Final = re.compile(r"(?<!!)\[[^\]]*\]\(([^)]+)\)|!\[[^\]]*\]\(([^)]+)\)")
HEADING_RE: Final = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$")
ENV_ALIAS_RE: Final = re.compile(r'validation_alias\s*=\s*["\'](ECC_[A-Z0-9_]+)["\']')
ENV_EXAMPLE_RE: Final = re.compile(r"^(ECC_[A-Z0-9_]+)=", re.MULTILINE)
DEAD_EVIDENCE_RE: Final = re.compile(r"\.superpowers/sdd/[A-Za-z0-9*?._/-]+\.md")


def parse_frontmatter(path: Path) -> dict[str, object]:
    """Parse the scalar/list subset of YAML used by governed Markdown."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    if not lines or lines[0].strip() != "---":
        return {}
    try:
        end = lines.index("---", 1)
    except ValueError:
        return {}

    metadata: dict[str, object] = {}
    current_list: str | None = None
    for raw_line in lines[1:end]:
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        if raw_line.startswith(("  - ", "- ")) and current_list:
            value = raw_line.split("-", 1)[1].strip().strip("\"'")
            existing = metadata.setdefault(current_list, [])
            if isinstance(existing, list):
                existing.append(value)
            continue
        if ":" not in raw_line or raw_line.startswith((" ", "\t")):
            current_list = None
            continue
        key, raw_value = raw_line.split(":", 1)
        key = key.strip()
        value = raw_value.strip()
        if value:
            metadata[key] = value.strip("\"'")
            current_list = None
        else:
            metadata[key] = []
            current_list = key
    return metadata


def github_anchor(text: str) -> str:
    """Return GitHub's normalized base anchor for an ATX heading."""
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("`", "")
    text = unicodedata.normalize("NFKC", text).lower().strip()
    text = "".join(char for char in text if char.isalnum() or char in {" ", "-", "_"})
    return text.replace(" ", "-")


def _lines_outside_fences(text: str) -> Iterable[tuple[int, str]]:
    fence: str | None = None
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.lstrip()
        marker = stripped[:3]
        if marker in {"```", "~~~"}:
            if fence is None:
                fence = marker
            elif fence == marker:
                fence = None
            continue
        if fence is None:
            yield number, line


def _heading_anchors(path: Path) -> set[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return set()
    anchors: set[str] = set()
    counts: dict[str, int] = {}
    for _, line in _lines_outside_fences(text):
        match = HEADING_RE.match(line)
        if not match:
            continue
        base = github_anchor(match.group(1))
        count = counts.get(base, 0)
        anchor = base if count == 0 else f"{base}-{count}"
        counts[base] = count + 1
        anchors.add(anchor)
    return anchors


def _markdown_files(root: Path) -> list[Path]:
    files = list((root / "docs").rglob("*.md")) if (root / "docs").is_dir() else []
    if (root / "README.md").is_file():
        files.append(root / "README.md")
    return sorted(files)


def _governed_class(relative: Path, metadata: dict[str, object]) -> tuple[str, set[str]] | None:
    value = relative.as_posix()
    name = relative.name
    if re.fullmatch(r"docs/adr/ADR-\d{4}-.+\.md", value):
        return "ADR", ADR_STATUSES
    if re.fullmatch(r"docs/RFC-\d{3}\.md", value):
        return "RFC", NORMATIVE_STATUSES
    normative_prefixes = (
        "docs/standards/",
        "docs/specifications/",
        "docs/domain/",
        "docs/security/",
    )
    if value.startswith(normative_prefixes):
        return "normative document", NORMATIVE_STATUSES
    if re.fullmatch(r"docs/phases/PHASE-\d{3}-.+\.md", value):
        return "phase specification", PHASE_STATUSES
    if value.startswith("docs/phases/phase-"):
        if name == "IMPLEMENTATION-STATUS.md":
            return "implementation status", IMPLEMENTATION_STATUSES
        if name in {"CONSISTENCY-REVIEW.md", "FINAL-ACCEPTANCE.md"}:
            return "validation record", RUNBOOK_STATUSES
        return "phase contract", PHASE_STATUSES
    if value.startswith(("docs/runbooks/", "docs/operations/", "docs/evidence/")):
        return "runbook", RUNBOOK_STATUSES
    if value.startswith("docs/product/"):
        return "product contract", PHASE_STATUSES
    if value.startswith("docs/superpowers/") and metadata:
        return "planning artifact", PLANNING_STATUSES
    return None


def _validate_metadata(root: Path, files: list[Path]) -> list[str]:
    errors: list[str] = []
    for path in files:
        relative = path.relative_to(root)
        metadata = parse_frontmatter(path)
        governed = _governed_class(relative, metadata)
        if governed is None:
            continue
        document_class, allowed = governed
        missing = sorted(REQUIRED_METADATA - metadata.keys())
        if missing:
            errors.append(
                f"{relative.as_posix()}: missing front matter fields: {', '.join(missing)}"
            )
            continue
        status = metadata.get("status")
        if status not in allowed:
            errors.append(
                f"{relative.as_posix()}: status '{status}' is invalid for {document_class}; "
                f"allowed: {', '.join(sorted(allowed))}"
            )
    return errors


def _clean_destination(raw: str) -> str:
    destination = raw.strip()
    if destination.startswith("<") and ">" in destination:
        return destination[1 : destination.index(">")]
    if " " in destination:
        destination = destination.split(" ", 1)[0]
    return destination


def _validate_links(root: Path, files: list[Path]) -> list[str]:
    errors: list[str] = []
    anchor_cache: dict[Path, set[str]] = {}
    for source in files:
        relative_source = source.relative_to(root).as_posix()
        text = source.read_text(encoding="utf-8")
        for _, line in _lines_outside_fences(text):
            for match in LINK_RE.finditer(line):
                raw_destination = match.group(1) or match.group(2) or ""
                destination = unquote(_clean_destination(raw_destination))
                if not destination or destination.startswith(
                    ("http://", "https://", "mailto:", "tel:", "data:", "/")
                ):
                    continue
                file_part, separator, fragment = destination.partition("#")
                target = source if not file_part else (source.parent / file_part).resolve()
                try:
                    target.relative_to(root.resolve())
                except ValueError:
                    errors.append(f"{relative_source}: link escapes repository: {destination}")
                    continue
                if not target.exists():
                    errors.append(f"{relative_source}: broken link: {destination}")
                    continue
                if separator and fragment and target.suffix.lower() in {".md", ""}:
                    anchors = anchor_cache.setdefault(target, _heading_anchors(target))
                    if fragment not in anchors:
                        errors.append(f"{relative_source}: broken anchor {destination}")
    return errors


def _validate_phase_contracts(root: Path) -> list[str]:
    errors: list[str] = []
    phase_root = root / "docs/phases"
    if not phase_root.is_dir():
        return errors
    for path in sorted(phase_root.glob("PHASE-[0-9][0-9][0-9]-*.md")):
        metadata = parse_frontmatter(path)
        if metadata.get("status") not in {"Approved for Implementation", "Implemented"}:
            continue
        phase_id = metadata.get("id")
        if "contracts" not in metadata and phase_id != "PHASE-000":
            errors.append(f"{path.relative_to(root)}: approved phase has no contracts metadata")
            continue
        contracts = metadata.get("contracts", [])
        if not isinstance(contracts, list):
            errors.append(f"{path.relative_to(root)}: contracts must be a list")
            continue
        for contract in contracts:
            contract_path = phase_root / str(contract)
            if not contract_path.is_file():
                errors.append(f"{path.relative_to(root)}: missing contract: {contract}")
    return errors


def _validate_evidence(files: list[Path], root: Path) -> list[str]:
    errors: list[str] = []
    for path in files:
        if DEAD_EVIDENCE_RE.search(path.read_text(encoding="utf-8")):
            errors.append(
                f"{path.relative_to(root).as_posix()}: "
                "dead local-agent evidence reference: .superpowers/sdd/"
            )
    return errors


def _validate_environment(root: Path) -> list[str]:
    config_path = root / "backend/ecc/config.py"
    example_path = root / ".env.example"
    if not config_path.is_file() or not example_path.is_file():
        return []
    aliases = set(ENV_ALIAS_RE.findall(config_path.read_text(encoding="utf-8")))
    examples = set(ENV_EXAMPLE_RE.findall(example_path.read_text(encoding="utf-8")))
    return [f"{alias} is missing from .env.example" for alias in sorted(aliases - examples)]


def _validate_legacy_authority(root: Path) -> list[str]:
    path = root / "docs/00-document-control.md"
    if not path.is_file():
        return []
    text = path.read_text(encoding="utf-8")
    return [
        f"docs/00-document-control.md: legacy authoritative inheritance target: {target}"
        for target in sorted(LEGACY_AUTHORITY_TARGETS)
        if target in text
    ]


def _validate_registry_and_projections(root: Path) -> list[str]:
    registry_path = root / docs_status.REGISTRY_PATH
    if not registry_path.is_file():
        return []
    try:
        data = docs_status.load_registry(registry_path)
    except ValueError as exc:
        return [str(exc)]
    errors = docs_status.validate_registry(data, root)
    phase_index = root / "docs/phases/README.md"
    if phase_index.is_file():
        index_text = phase_index.read_text(encoding="utf-8")
        phases = data.get("phases", [])
        if isinstance(phases, list):
            for phase in phases:
                if not isinstance(phase, dict):
                    continue
                specification = phase.get("specification")
                if isinstance(specification, str) and Path(specification).name not in index_text:
                    errors.append(
                        f"docs/phases/README.md: phase index omits {Path(specification).name}"
                    )
    for target in docs_status.TARGETS:
        path = root / target
        if not path.is_file():
            errors.append(f"{target}: generated phase status target is missing")
            continue
        try:
            expected = docs_status._expected_target(root, target, data)
        except ValueError as exc:
            errors.append(str(exc))
            continue
        if path.read_text(encoding="utf-8") != expected:
            errors.append(f"{target}: generated phase status is stale")
    return errors


def validate_repository(root: Path) -> list[str]:
    """Return all repository documentation violations in stable order."""
    root = root.resolve()
    files = _markdown_files(root)
    errors: list[str] = []
    errors.extend(_validate_registry_and_projections(root))
    errors.extend(_validate_metadata(root, files))
    errors.extend(_validate_phase_contracts(root))
    errors.extend(_validate_links(root, files))
    errors.extend(_validate_evidence(files, root))
    errors.extend(_validate_environment(root))
    errors.extend(_validate_legacy_authority(root))
    return sorted(set(errors))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).parents[1])
    args = parser.parse_args(argv)
    errors = validate_repository(args.root)
    for error in errors:
        print(error, file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
