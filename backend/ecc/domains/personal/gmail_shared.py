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

from ecc.platform.connector_security import EmailConsentInactiveError


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


def require_email_consent_locked(
    session: Session,
    *,
    workspace_id: UUID,
    owner_id: UUID,
    connector_account_id: UUID | None,
) -> None:
    """Security Remediation FX5: the race-free consent re-check every Gmail
    write transaction runs before its first write. Raises
    `EmailConsentInactiveError` (writing nothing) unless the owner's
    `email` consent is active AND a non-`disconnected` `gmail` connector of
    theirs exists (`connector_account_id`, when the caller knows it).

    `connector_account_id` is required (no default): every Gmail write
    passes the specific connector; `None` (owner-wide, all of the owner's
    rows) must be an explicit, commented choice at the call site.

    Locks, in this order, both `FOR KEY SHARE` and held to commit:
    1. the owner's `email` `personal_domains` row -- disable/delete lock it
       `FOR UPDATE` first (`domains._disable_domain`, `export_deletion.
       delete_domain_endpoint`), and an `email_threads` insert already
       key-share locks it through its FK, so taking it here first keeps
       one order (domain row -> connector rows) on both sides;
    2. the connector row(s) -- ONLY `connector_account_id`'s row when the
       caller knows it (every current caller does), else all of the
       owner's `gmail` rows `ORDER BY id` -- which `gmail_revocation.
       cascade_email_revocation` (all of the owner's rows) and member
       removal lock `FOR UPDATE` before purging/disconnecting. Locking only
       the known row keeps writers from queueing behind an unrelated
       connector's `FOR UPDATE` (see below).
    A cascade that commits first is seen here: the lock wait returns the
    new row versions, and the consent read -- folded into the second
    statement, so the check costs two round trips -- runs on that
    statement's own READ COMMITTED snapshot, taken after the domain-row
    wait ended, i.e. after any disable/delete holding that row committed.
    No revocation can commit later in this transaction: every one holds
    the domain row `FOR UPDATE` (`_disable_domain`, `delete_domain_
    endpoint`), which now waits for this transaction. A cascade that starts
    later likewise waits, then purges what this transaction wrote. `KEY
    SHARE`, not `SHARE`: it conflicts with those `FOR UPDATE`s but not with
    ordinary non-key `UPDATE`s of the same rows (a plain token-refresh or
    bookkeeping `UPDATE`, domain settings). It DOES conflict with every
    `SELECT ... FOR UPDATE` of the row, both ways: notably sync phase 1
    (`connector_accounts._run_connector_sync`, `get_connector_account(...,
    for_update=True)`) holds the connector row `FOR UPDATE` across its
    token refresh (up to 10s) -- a concurrent writer for the same connector
    (e.g. action detection from an earlier sync, an on-demand thread body
    fetch) waits on it and can hit the 5s statement timeout (known
    limitation, see that phase-1 note). The price of `KEY SHARE`: a path
    that withdraws consent or disconnects a Gmail connector must take `FOR
    UPDATE` on the row first (all current ones do) -- a bare `UPDATE`
    would not wait for in-flight writers.

    Lock order: call AFTER the shared membership lock (and after any
    idempotency lock) of the transaction -- see `platform.
    connector_security`'s lock-ordering note.
    """
    params = {"workspace_id": workspace_id, "owner_id": owner_id}
    session.execute(
        text(
            "SELECT id FROM personal_domains WHERE workspace_id = :workspace_id "
            "AND owner_id = :owner_id AND domain_key = 'email' FOR KEY SHARE"
        ),
        params,
    ).all()
    # One row per locked connector (none -> no live connector); the
    # consent EXISTS is identical on each row. Must stay a separate
    # statement from the domain-row lock above (see docstring: snapshot
    # after that wait). `only_id` is a bound value, never interpolated.
    connectors = session.execute(
        text(
            "SELECT ca.id, ca.status, EXISTS ("
            "SELECT 1 FROM domain_consents dc WHERE dc.workspace_id = ca.workspace_id "
            "AND dc.owner_id = ca.owner_id AND dc.domain_key = 'email' "
            "AND dc.revoked_at IS NULL) "
            "FROM connector_accounts ca WHERE ca.workspace_id = :workspace_id "
            "AND ca.provider = 'gmail' AND ca.owner_id = :owner_id "
            "AND (CAST(:only_id AS uuid) IS NULL OR ca.id = CAST(:only_id AS uuid)) "
            "ORDER BY ca.id FOR KEY SHARE OF ca"
        ),
        {**params, "only_id": connector_account_id},
    ).all()
    live = any(
        consent_active and status != "disconnected"
        for _account_id, status, consent_active in connectors
    )
    if not live:
        raise EmailConsentInactiveError
