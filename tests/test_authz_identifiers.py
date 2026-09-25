"""`authz.require_safe_sql_identifier` -- pure unit tests, no database."""

from __future__ import annotations

import pytest

from ecc.platform import authz


@pytest.mark.parametrize("value", ["risks", "_risks", "risk_reviews2"])
def test_require_safe_sql_identifier_accepts_bare_identifiers(value: str) -> None:
    authz.require_safe_sql_identifier(value, label="table_alias")


@pytest.mark.parametrize("value", ["risks\n", "risks;", "risks x", "1risks", ""])
def test_require_safe_sql_identifier_rejects_non_bare_identifiers(value: str) -> None:
    """`$` also matches before a trailing newline, so the check must be a
    full match -- `"risks\n"` passed the old `re.match` + `^...$` pattern.
    """
    with pytest.raises(authz.UnknownResourceTypeError, match="not a safe SQL identifier"):
        authz.require_safe_sql_identifier(value, label="table_alias")
