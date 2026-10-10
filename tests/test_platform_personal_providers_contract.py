"""The frontend's copy of `PERSONAL_PROVIDERS` matches the backend's.

`frontend/src/features/engineering/personalProviders.ts` filters personal
connectors (a member's own Gmail) out of every engineering view; the
backend's `connector_security.PERSONAL_PROVIDERS` decides which providers
are personal for authorization, sharing and removal. The frontend cannot
import the backend set, so a provider added to one list and not the other
would show a member's personal connector in shared engineering views (or
hide a workspace one). This test reads the TypeScript literal and fails on
any drift.
"""

from __future__ import annotations

import re
from pathlib import Path

from ecc.platform.connector_security import PERSONAL_PROVIDERS

_REPO_ROOT = Path(__file__).resolve().parents[1]
_FRONTEND_FILE = _REPO_ROOT / "frontend/src/features/engineering/personalProviders.ts"
_SET_LITERAL = re.compile(
    r"export const PERSONAL_PROVIDERS\b[^=]*=\s*new Set<[^>]*>\(\[(?P<items>[^\]]*)\]\)"
)


def _frontend_personal_providers() -> set[str]:
    match = _SET_LITERAL.search(_FRONTEND_FILE.read_text(encoding="utf-8"))
    assert match is not None, (
        f"could not find the PERSONAL_PROVIDERS Set literal in {_FRONTEND_FILE}; "
        "update this test's pattern if the declaration's shape changed"
    )
    items = match.group("items")
    leftover = re.sub(r"""['"][^'"]+['"]|//[^\n]*|/\*.*?\*/|[\s,]""", "", items, flags=re.S)
    assert not leftover, (
        f"PERSONAL_PROVIDERS holds something other than string literals ({leftover!r}); "
        "this test can only compare literal provider names"
    )
    return set(re.findall(r"""['"]([^'"]+)['"]""", items))


def test_frontend_personal_providers_match_the_backend() -> None:
    frontend = _frontend_personal_providers()
    assert frontend, "the frontend PERSONAL_PROVIDERS set parsed as empty"
    assert frontend == set(PERSONAL_PROVIDERS)
