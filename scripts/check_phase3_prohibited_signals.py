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

Matching is word-boundary anchored, not a bare substring search: an
identifier/literal is split into lowercase word tokens on snake_case and
camelCase boundaries, and a prohibited term matches only a whole token (or
a token *prefix*, to keep deliberately-partial stems like ``"pregnan"``
catching ``"pregnant"``/``"pregnancy"``) -- never a substring spanning
into the middle of an unrelated word. Plain substring search previously
let a banned term match as a fragment of a longer, unrelated identifier
(e.g. ``"race"`` inside ``"trace_id"``).

The scan also reconstructs the literal text of ``ast.BinOp`` string
concatenation (``"raw_model" + "_score"``) and ``ast.JoinedStr`` f-strings
(``f"raw_model_{suffix}score"``) from their literal parts before matching,
in addition to matching each literal fragment on its own -- a term split
across a concatenation or across an f-string's interpolation previously
evaded detection entirely, since no single literal fragment alone
contained the full term.
"""

from __future__ import annotations

import argparse
import ast
import re
from pathlib import Path

DEFAULT_TARGET = Path("backend/ecc/domains/attention")

# Each entry is (category, [terms]). Terms are matched case-insensitively
# and word-boundary anchored against identifier names and string literal
# values (see module docstring): each term is tokenized on `_`/camelCase
# boundaries the same way the scanned text is, and matches a contiguous
# run of tokens whose prefixes equal the term's own tokens -- so
# multi-word terms like "sexual_orientation" must appear as adjacent whole
# words, and single terms match a whole word or a word that starts with
# it (deliberately, for partial stems like "pregnan").
PROHIBITED_SIGNALS: list[tuple[str, list[str]]] = [
    (
        "protected characteristic",
        [
            "race",
            "ethnicity",
            "gender",
            "religion",
            "disability",
            "sexual_orientation",
            "national_origin",
            "pregnan",
            "veteran_status",
            "marital_status",
            "age_bracket",
        ],
    ),
    (
        "inferred emotion or personality",
        ["emotion", "sentiment", "mood", "personality", "tone_score", "affect_score"],
    ),
    (
        "employee activity volume",
        [
            "activity_volume",
            "keystroke",
            "active_hours",
            "screen_time",
            "login_count",
            "idle_time",
            "clicks_per",
            "messages_sent_count",
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


# Splits on underscores directly, and inserts an underscore before an
# uppercase letter that follows a lowercase letter/digit (camelCase ->
# camel_Case) so both naming conventions tokenize the same way.
_CAMEL_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def _tokenize(text: str) -> list[str]:
    """Split into lowercase word tokens on snake_case/camelCase
    boundaries. See module docstring."""
    spaced = _CAMEL_BOUNDARY_RE.sub("_", text)
    return [token.lower() for token in spaced.split("_") if token]


def _token_sequence_matches(haystack: list[str], needle: list[str]) -> bool:
    """Whether ``needle``'s tokens appear as a contiguous run in
    ``haystack``, matching each position by prefix (``haystack[i]``
    starts with ``needle[i]``) rather than exact equality -- this is what
    lets a single-word stem like "pregnan" still catch "pregnant" while
    keeping the match anchored to a whole token's start, never to a
    substring in the middle of one (finding #16's false-positive half).
    """
    if not needle:
        return False
    span = len(needle)
    for start in range(len(haystack) - span + 1):
        if all(haystack[start + i].startswith(needle[i]) for i in range(span)):
            return True
    return False


# Second-pass threshold for the single-unbroken-token substring check
# below. A normal, well-intentioned snake_case/camelCase identifier is
# already caught by `_token_sequence_matches` above regardless of length
# -- this secondary pass exists only for a single token that has NO
# internal separators or camelCase boundaries at all (it didn't get split
# by `_tokenize`), where prefix-only matching would otherwise let a
# prohibited term hide anywhere but the start (e.g. "targetgenderscore").
# 10 is a deliberate, bounded trade-off: legitimately-named short common
# English words that coincidentally contain a banned term as a substring
# (`trace`=5, `workspace`=9, `embrace`=7 chars) stay under it and keep
# prefix-only matching (no false-positive regression), while a
# deliberately-concatenated multi-concept identifier like
# "targetgenderscore"(18), "flagethnicity"(13), "usermoodinferred"(16),
# "USERRACEID"(10) or "agebracket"(10) reaches it.
_SINGLE_TOKEN_SUBSTRING_MIN_LEN = 10


def _matches(text: str) -> list[str]:
    if text.lower() in ALLOWLIST:
        return []
    tokens = _tokenize(text)
    single_unbroken_token = (
        tokens[0]
        if len(tokens) == 1 and len(tokens[0]) >= _SINGLE_TOKEN_SUBSTRING_MIN_LEN
        else None
    )
    hits = []
    for category, terms in PROHIBITED_SIGNALS:
        for term in terms:
            term_tokens = _tokenize(term)
            if _token_sequence_matches(tokens, term_tokens):
                hits.append(f"{category} ({term!r})")
            elif (
                single_unbroken_token is not None and "".join(term_tokens) in single_unbroken_token
            ):
                hits.append(f"{category} ({term!r})")
    return hits


def _static_text(node: ast.AST) -> str | None:
    """Reconstruct the literal text an ``ast.BinOp`` string concatenation
    or an ``ast.JoinedStr`` f-string would produce, from its literal parts
    only -- an interpolated ``ast.FormattedValue`` contributes nothing
    (its runtime value isn't known statically). Returns ``None`` when the
    node isn't one of these shapes, or a concatenation involves a
    non-literal operand. See module docstring's false-negative half.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _static_text(node.left)
        right = _static_text(node.right)
        if left is None or right is None:
            return None
        return left + right
    if isinstance(node, ast.JoinedStr):
        parts = [
            value.value
            for value in node.values
            if isinstance(value, ast.Constant) and isinstance(value.value, str)
        ]
        return "".join(parts) if parts else None
    return None


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
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node not in docstrings
        ):
            name = node.value
        elif isinstance(node, ast.BinOp | ast.JoinedStr):
            name = _static_text(node)

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
