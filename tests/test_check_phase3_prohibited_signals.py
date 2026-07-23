"""Unit coverage for scripts/check_phase3_prohibited_signals.py's matching
logic (finding #16): word-boundary anchoring (no false positives from a
banned term appearing as a mid-word substring) and reconstruction of
``ast.BinOp`` string concatenation / ``ast.JoinedStr`` f-strings (no
false negatives from a banned term split across either).
"""

from __future__ import annotations

import ast
import importlib.util
import textwrap
from collections.abc import Callable
from pathlib import Path
from typing import Any

CHECKER_PATH = Path("scripts/check_phase3_prohibited_signals.py")
spec = importlib.util.spec_from_file_location("phase3_prohibited_signals_checker", CHECKER_PATH)
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

matches: Callable[[str], list[str]] = module._matches
static_text: Callable[[ast.AST], str | None] = module._static_text
validate: Callable[[Path], list[str]] = module.validate


def _scan_source(tmp_path: Path, source: str) -> list[str]:
    target = tmp_path / "attention"
    target.mkdir()
    (target / "example.py").write_text(textwrap.dedent(source))
    return validate(target)


# --- Anchoring: no false positives from a mid-word substring ---------------


def test_race_does_not_match_inside_unrelated_identifier() -> None:
    # "trace" and "workspace" both contain "race" as a bare substring, but
    # neither is (or contains) the word "race" at a token boundary.
    assert matches("trace_id") == []
    assert matches("workspace") == []
    assert matches("embrace_change") == []


def test_race_matches_as_a_whole_word() -> None:
    assert any("race" in hit for hit in matches("race"))
    assert any("race" in hit for hit in matches("candidate_race"))


def test_stem_term_still_matches_as_a_prefix() -> None:
    # "pregnan" is a deliberate partial stem (see PROHIBITED_SIGNALS) meant
    # to catch "pregnant"/"pregnancy" as whole words, not mid-word.
    assert matches("is_pregnant")
    assert matches("pregnancy_leave")


def test_multi_word_term_requires_adjacent_whole_words() -> None:
    assert matches("sexual_orientation")
    assert matches("national_origin_code")
    # Reordered/non-adjacent words must not match.
    assert matches("origin_national") == []


# --- Reconstruction: BinOp concatenation and f-strings ----------------------


def test_static_text_reconstructs_binop_concatenation() -> None:
    tree = ast.parse('"raw_model" + "_score"', mode="eval")
    assert static_text(tree.body) == "raw_model_score"


def test_static_text_reconstructs_joinedstr_literal_parts() -> None:
    tree = ast.parse('f"raw_model_{suffix}score"', mode="eval")
    assert static_text(tree.body) == "raw_model_score"


def test_static_text_returns_none_for_non_literal_binop() -> None:
    tree = ast.parse('"raw_model" + suffix', mode="eval")
    assert static_text(tree.body) is None


def test_scan_catches_prohibited_term_split_across_binop_concatenation(
    tmp_path: Path,
) -> None:
    errors = _scan_source(
        tmp_path,
        """
        def build_key() -> str:
            return "raw_model" + "_score"
        """,
    )
    assert errors, "expected a hit for a term split across string concatenation"


def test_scan_catches_prohibited_term_split_across_fstring_interpolation(
    tmp_path: Path,
) -> None:
    errors = _scan_source(
        tmp_path,
        """
        def build_key(suffix: str) -> str:
            return f"raw_model_{suffix}score"
        """,
    )
    assert errors, "expected a hit for a term split across an f-string interpolation"


def test_scan_stays_clean_for_unrelated_code(tmp_path: Path) -> None:
    errors = _scan_source(
        tmp_path,
        '''
        def workspace_trace_id() -> str:
            """Docstring mentioning race, gender and emotion is exempt."""
            return "trace" + "_id"
        ''',
    )
    assert errors == []


def test_real_attention_directory_scans_clean() -> None:
    assert validate(Path("backend/ecc/domains/attention")) == []


def test_manifest_types(module_: Any = module) -> None:
    # Guard against an accidental signature drift silently no-op'ing the
    # gate (e.g. PROHIBITED_SIGNALS becoming an empty list).
    assert module_.PROHIBITED_SIGNALS
    assert all(terms for _category, terms in module_.PROHIBITED_SIGNALS)
