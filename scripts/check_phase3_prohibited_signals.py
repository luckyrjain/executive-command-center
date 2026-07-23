"""Static gate enforcing ATTENTION-MODEL.md's excluded-inputs list.

Phase 3 Task 8, Step 3. "The score ranks work, not people" (ATTENTION-
MODEL.md's Principle) is a cross-phase invariant this repository has always
treated as something to enforce with a scriptable, CI-runnable check, not
just document -- matching check_phase1_acceptance.py's pattern. This scans
every Python source file under backend/ecc/domains/attention/ for
identifiers and non-docstring string literals matching any of
ATTENTION-MODEL.md's excluded-input categories:

  Protected characteristics, inferred emotion or personality, employee
  activity volume, message response speed as a performance proxy,
  private-source content without permission and opaque model-only scores.

Deliberately AST-based, not a plain text grep: it walks identifiers
(function/class/variable names, function arguments, attribute accesses)
and string constants that are not docstrings, so this module's own
docstrings and comments -- which necessarily discuss the excluded terms to
explain why they're excluded -- never trigger a false positive. Comments
are not part of the AST at all and are skipped automatically.
"""

from __future__ import annotations

import argparse
import ast
from pathlib import Path

DEFAULT_TARGET = Path("backend/ecc/domains/attention")

# Each entry is (category, [substrings]). Substrings are matched
# case-insensitively against identifier names and string literal values.
# Deliberately substrings, not whole-word terms: a scoring module has no
# legitimate reason to contain "race", "gender", "emotion", etc. as any
# part of an identifier or literal, so a substring match has no realistic
# false-positive surface here (unlike, say, a generic codebase grep).
PROHIBITED_SIGNALS: list[tuple[str, list[str]]] = [
    (
        "protected characteristic",
        [
            "race", "ethnicity", "gender", "religion", "disability",
            "sexual_orientation", "national_origin", "pregnan", "veteran_status",
            "marital_status", "age_bracket",
        ],
    ),
    (
        "inferred emotion or personality",
        ["emotion", "sentiment", "mood", "personality", "tone_score", "affect_score"],
    ),
    (
        "employee activity volume",
        [
            "activity_volume", "keystroke", "active_hours", "screen_time",
            "login_count", "idle_time", "clicks_per", "messages_sent_count",
        ],
    ),
    (
        "message response speed as a performance proxy",
        ["response_speed", "reply_speed", "response_time_score", "time_to_reply_score"],
    ),
    (
        "opaque model-only score",
        ["raw_model_score", "llm_score", "ai_only_score", "black_box_score"],
    ),
]

# Identifiers/literals that would otherwise false-positive against the
# substrings above but are legitimate, already-reviewed vocabulary in this
# domain (e.g. "age_bracket" as a substring would match nothing here, but
# kept as an explicit allowlist point in case future signal names collide).
ALLOWLIST: frozenset[str] = frozenset()


def _docstring_nodes(tree: ast.Module) -> set[ast.expr]:
    """Return every string-constant node that `ast.get_docstring` would
    treat as a docstring, for module/class/function bodies, so they're
    excluded from the scan (see module docstring)."""
    docstrings: set[ast.expr] = set()
    candidates: list[ast.AST] = [tree]
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            candidates.append(node)
    for node in candidates:
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            docstrings.add(first.value)
    return docstrings


def _matches(text: str) -> list[str]:
    lowered = text.lower()
    if lowered in ALLOWLIST:
        return []
    hits = []
    for category, substrings in PROHIBITED_SIGNALS:
        for substring in substrings:
            if substring in lowered:
                hits.append(f"{category} ({substring!r})")
    return hits


def scan_file(path: Path) -> list[str]:
    errors: list[str] = []
    tree = ast.parse(path.read_text(), filename=str(path))
    docstrings = _docstring_nodes(tree)

    for node in ast.walk(tree):
        name: str | None = None
        if isinstance(node, ast.Name):
            name = node.id
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            name = node.name
        elif isinstance(node, ast.arg):
            name = node.arg
        elif isinstance(node, ast.Attribute):
            name = node.attr
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and node not in docstrings:
            name = node.value

        if name is None:
            continue
        for hit in _matches(name):
            errors.append(f"{path}:{node.lineno}: {hit} -- {name!r}")

    return errors


def validate(target: Path) -> list[str]:
    errors: list[str] = []
    if not target.exists():
        return [f"target path does not exist: {target}"]
    for path in sorted(target.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        errors.extend(scan_file(path))
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "target",
        nargs="?",
        type=Path,
        default=DEFAULT_TARGET,
        help="Directory to scan (default: backend/ecc/domains/attention)",
    )
    args = parser.parse_args()

    errors = validate(args.target)
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print(f"No prohibited attention-scoring signals found under {args.target}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
