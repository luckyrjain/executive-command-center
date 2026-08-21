"""Gmail helpers used across module (and domain) boundaries.

`gmail_adapter.py` is the ~3k-line home of `GmailAdapter` itself plus every
sync-specific helper `backfill`/`incremental_sync`/`detect_actions_since`
need, importing `httpx`, `ai_runtime.ollama_client`, `ai_runtime.runtime`,
and `governance.recommendation_mutations` along the way. The handful of
helpers below have none of that: they're pure credential (de)serialization,
one `domain_consents` read, and a `str.strip().casefold()`. Everything here
used to live in `gmail_adapter.py` and get imported straight out of it by
`gmail_threads.py` (`bearer_headers`/`email_consent_active`) and by
`ecc.domains.attention.attention` (`normalize_email`, the one cross-domain
case) -- reaching a sibling/cross-domain module past its adapter class just
for a stateless helper, and pulling in that module's own heavy import graph
to do it. `gmail_adapter.py` itself now imports these back from here rather
than defining them, so its own internal call sites are unaffected.
`gmail_action_detection.py` (a later, separate architecture-review split)
is a third consumer of `bearer_headers`/`email_consent_active`, for the
identical reason.

Every helper below is public (no leading underscore) precisely because
every one of them is meant to be imported across module/domain boundaries
-- this whole module exists for that purpose, so a private name here would
only misrepresent it as an internal implementation detail. A private name
on a cross-module dependency is exactly the "fragile private import" bug
class that broke the build once already this session (a different
private helper deleted elsewhere silently broke an importer that had
reached past the underscore) -- see identity/accounts.py's and
connector_accounts.py's own near-identical fixes for the same class.
"""

from __future__ import annotations

from datetime import datetime
from json import dumps, loads
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session


def pack_credential(access_token: str, refresh_token: str, expires_at: datetime) -> str:
    return dumps(
        {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "expires_at": expires_at.isoformat(),
        }
    )


def unpack_credential(credential: str) -> dict[str, str]:
    """Every caller (`GmailAdapter.refresh_permissions`/`disconnect`, plus
    `bearer_headers` below) catches only `(ValueError, TypeError)` around
    this call -- `loads` itself only ever raises `ValueError` (malformed
    JSON), but valid JSON that isn't an object (a list, `null`, a bare
    number) would decode successfully and silently violate this function's
    own `dict[str, str]` return annotation, surfacing later as an uncaught
    `AttributeError` on the caller's first `.get(...)` instead. Raising
    `TypeError` here instead keeps every caller's existing narrow `except`
    sufficient, rather than requiring each call site to separately guard
    against a shape violation this function itself is responsible for.
    """
    data = loads(credential)
    if not isinstance(data, dict):
        raise TypeError(
            f"Gmail credential JSON must decode to an object, got {type(data).__name__}"
        )
    return data


def bearer_headers(credential: str) -> dict[str, str]:
    access_token = unpack_credential(credential).get("access_token", "")
    return {"Authorization": f"Bearer {access_token}"}


def email_consent_active(session: Session, workspace_id: UUID, owner_id: UUID) -> bool:
    """Plan Task 2: "each re-invocation re-verifies the `email` domain's
    `domain_consents` row is still active at call time, not merely at
    original connect time" -- `gmail_adapter.py` calls this both before a
    sync call starts fetching anything and again before writing each
    message it fetches, so a consent revoked mid-call halts further writes
    rather than only being checked once at the top; `gmail_threads.py`
    calls it once, synchronously, before its own live per-thread fetch.
    """
    row = session.execute(
        text(
            "SELECT 1 FROM domain_consents WHERE workspace_id = :workspace_id "
            "AND owner_id = :owner_id AND domain_key = 'email' AND revoked_at IS NULL"
        ),
        {"workspace_id": workspace_id, "owner_id": owner_id},
    ).one_or_none()
    return row is not None


def normalize_email(value: str) -> str:
    return value.strip().casefold()
