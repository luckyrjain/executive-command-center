"""Whole-domain export and deletion: `POST /api/v1/personal/domains/
{domain_key}/export`, `POST .../delete` (`docs/phases/phase-007/
API-SCHEMAS.md`).

Both operate synchronously, in-request -- no durable worker exists in
this package (`habits.py`'s own docstring notes the identical gap for
insight recomputation). `deletion_jobs.status` is written `'completed'`
in the same transaction that performs the delete, not `'pending'` followed
by an async transition, since there is no later process that would ever
perform that transition. A later task adding a domain whose deletion
genuinely needs to be long-running (e.g. propagating to an external
connector) is the natural point to introduce a real `pending` -> worker
-> `completed` lifecycle; speculatively building that state machine now,
with nothing that ever produces a `pending` row lasting more than one
transaction, would be exactly the kind of premature machinery this
codebase avoids elsewhere.

**Deletion retains the `personal_domains` row itself** (set `enabled =
false`), rather than deleting it -- `deletion_jobs`/`domain_consents` both
hold a composite FK against `(workspace_id, owner_id, domain_key)` on that
table (migration `0054_phase7_personal_domains.py`), so deleting the
parent row would either cascade-delete the very audit trail
(`deletion_jobs`) this endpoint exists to write, or require inserting it
in a separate, unrelated table with no FK integrity at all. A domain a
user has deleted and later re-enables (`domains.py`'s `_enable_domain`)
starts genuinely empty either way, since every child row was actually
removed here -- the retained parent row is bookkeeping, not residual data.
"""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text
from sqlalchemy.orm import Session

from ecc.auth import AuthContext, AuthDep, CsrfDep

from .domains import (
    DomainKey,
    IdempotencyHeader,
    SessionDep,
    _decrypt_payload,
    get_domain,
    load_cached,
    lock_idempotency,
    request_hash,
    store_idempotency,
)
from .gmail_revocation import PendingGmailRevoke, cascade_email_revocation, finish_gmail_revocation

router = APIRouter(prefix="/api/v1/personal", tags=["personal"])


class _EmptyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


# Every table this task's own scope populates under a domain -- deleted in
# this order so no foreign key (`goals` <- `routines` <- `check_ins`,
# `domain_sources` <- `domain_records`) is ever violated. `personal_
# insights`/`cross_domain_grants`/`domain_consents` are NOT included in this
# generic loop -- each needs a query (or, for `domain_consents`, an UPDATE)
# this loop's uniform `DELETE ... WHERE domain_key = :domain_key` shape
# can't express (see `_delete_domain_data` and `delete_domain_endpoint`
# below).
_CHILD_TABLES_DELETE_ORDER = (
    "check_ins",
    "routines",
    "goals",
    "domain_records",
    "domain_sources",
)


class DomainExportResponse(BaseModel):
    domain_key: str
    exported_at: datetime
    goals: list[dict[str, Any]]
    routines: list[dict[str, Any]]
    check_ins: list[dict[str, Any]]
    domain_records: list[dict[str, Any]]


class DomainDeletionResponse(BaseModel):
    id: UUID
    domain_key: str
    status: str
    requested_at: datetime
    completed_at: datetime | None


def _rows(session: Session, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    return [dict(r) for r in session.execute(text(sql), params).mappings().all()]


@router.post("/domains/{domain_key}/export", response_model=DomainExportResponse)
def export_domain_endpoint(
    domain_key: DomainKey, auth: AuthDep, session: SessionDep, _csrf: CsrfDep
) -> DomainExportResponse:
    """Machine-readable JSON export -- see `DOMAIN-PRIVACY-CONTRACT.md`'s
    "human-readable plus machine-readable" requirement; a dedicated
    human-readable rendering (e.g. a printable summary) is deferred to a
    later task, disclosed rather than silently substituted by this one
    JSON shape (clear field names, not a compressed/internal format, keep
    it reasonably legible in the meantime).

    Each `domain_records` row's `payload` is decrypted (Task 4,
    `ecc.domains.personal.domains._decrypt_payload`) before being included
    here -- an explicit, user-initiated export of the owner's own data is
    exactly the kind of single-purpose request design doc Decision 3 means
    by "only a single-record fetch returns the decrypted value"; returning
    ciphertext in a "human-readable" export would defeat the export's own
    purpose.
    """
    if get_domain(session, auth, domain_key) is None:
        raise HTTPException(status_code=404, detail="DOMAIN_NOT_FOUND")
    params = {"workspace_id": auth.workspace_id, "owner_id": auth.user_id, "domain_key": domain_key}
    return DomainExportResponse(
        domain_key=domain_key,
        exported_at=datetime.now(UTC),
        goals=_rows(
            session,
            "SELECT id, title, target_count, target_at, status, created_at FROM goals "
            "WHERE workspace_id = :workspace_id AND owner_id = :owner_id "
            "AND domain_key = :domain_key ORDER BY created_at ASC",
            params,
        ),
        routines=_rows(
            session,
            "SELECT id, goal_id, title, cadence, created_at FROM routines "
            "WHERE workspace_id = :workspace_id AND owner_id = :owner_id "
            "AND domain_key = :domain_key ORDER BY created_at ASC",
            params,
        ),
        check_ins=_rows(
            session,
            "SELECT c.id, c.routine_id, c.occurred_at, c.note FROM check_ins c "
            "WHERE c.workspace_id = :workspace_id AND c.domain_key = :domain_key "
            "AND c.owner_id = :owner_id ORDER BY c.occurred_at ASC",
            params,
        ),
        domain_records=[
            {**record, "payload": _decrypt_payload(record["record_type"], record["payload"])}
            for record in _rows(
                session,
                "SELECT id, record_type, payload, effective_at, created_at FROM domain_records "
                "WHERE workspace_id = :workspace_id AND owner_id = :owner_id "
                "AND domain_key = :domain_key ORDER BY effective_at ASC",
                params,
            )
        ],
    )


def _delete_domain_data(
    session: Session, auth: AuthContext, domain_key: str, now: datetime
) -> list[PendingGmailRevoke]:
    """Returns pending Google-side revoke info when `domain_key ==
    "email"` -- see `gmail_revocation.py`'s own module docstring. `now` is
    the caller's own single timestamp for this request, reused here so
    every row this cascade touches shares the identical `updated_at`/
    `disconnected_at` moment the rest of `delete_domain_endpoint`'s own
    writes use.
    """
    params = {"workspace_id": auth.workspace_id, "owner_id": auth.user_id, "domain_key": domain_key}
    for table in _CHILD_TABLES_DELETE_ORDER:
        session.execute(
            text(  # noqa: S608 -- table name is one of five fixed literals, never user input
                f"DELETE FROM {table} WHERE workspace_id = :workspace_id "
                "AND owner_id = :owner_id AND domain_key = :domain_key"
            ),
            params,
        )
    # Matches an insight recorded under this domain (`domain_key` column,
    # the *first* requested source per `ai_insights.py`'s own docstring) OR
    # one whose `evidence.source_domain_keys` JSONB array names this domain
    # as ANY of its sources -- "removes ... derived content" (`DOMAIN-
    # PRIVACY-CONTRACT.md`) means every insight this domain's now-deleted
    # data contributed to, not only the one column a cross-domain insight
    # happens to be filed under. A deterministic (non-AI, Task 1) insight's
    # own `evidence` has no `source_domain_keys` key at all, so the JSONB
    # lookup is simply absent/falsy there and the `domain_key =` match alone
    # still governs it correctly. `personal_insight_feedback` cascades via
    # its own FK (migration `0059`'s `ondelete="CASCADE"`), so no separate
    # delete is needed for it here.
    session.execute(
        text(
            "DELETE FROM personal_insights WHERE workspace_id = :workspace_id "
            "AND owner_id = :owner_id "
            "AND (domain_key = :domain_key "
            "OR evidence -> 'source_domain_keys' ? :domain_key)"
        ),
        params,
    )
    # A grant naming this domain as its `source_domain_key` is meaningless
    # once the domain's own data is gone -- left active, it would silently
    # re-apply to new records the moment the user re-enables the domain,
    # contradicting this endpoint's own "clean slate" intent.
    session.execute(
        text(
            "DELETE FROM cross_domain_grants WHERE workspace_id = :workspace_id "
            "AND owner_id = :owner_id AND source_domain_key = :domain_key"
        ),
        params,
    )

    # Phase 10 Task 7: `email` is the one domain whose deletion needs to
    # reach beyond this module's own generic tables -- see `gmail_
    # revocation.py`'s own module docstring for the full cascade (Gmail
    # connector disconnect, `email_threads`/`email_messages`, and every
    # derived `attention_items`/`recommendations`/`pkos_evidence` row).
    if domain_key == "email":
        return cascade_email_revocation(session, auth, now)
    return []


@router.post("/domains/{domain_key}/delete", response_model=DomainDeletionResponse)
def delete_domain_endpoint(
    domain_key: DomainKey,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
    idempotency_key: IdempotencyHeader,
) -> DomainDeletionResponse:
    """Idempotency-key handling matches every other mutating endpoint in
    this package -- the underlying `DELETE`s are individually idempotent
    (deleting already-gone rows is a no-op), but the `personal_domains`
    version bump and the `deletion_jobs` audit INSERT below are not: a
    retried request without this guard would bump `version` again and
    insert a second `'completed'` `deletion_jobs` row for the same domain
    and moment, adding audit-log noise no other mutation in this phase
    tolerates.
    """
    req_hash = request_hash(_EmptyBody(), f"delete_domain:{domain_key}")
    now = datetime.now(UTC)
    pending_gmail_revokes: list[PendingGmailRevoke] = []
    with session.begin():
        lock_idempotency(session, auth, idempotency_key)
        cached = load_cached(
            session, auth, idempotency_key, req_hash, domain="personal_domain_deletion"
        )
        if cached is not None:
            return DomainDeletionResponse.model_validate(cached)

        domain = get_domain(session, auth, domain_key)
        if domain is None:
            raise HTTPException(status_code=404, detail="DOMAIN_NOT_FOUND")

        pending_gmail_revokes = _delete_domain_data(session, auth, domain_key, now)

        session.execute(
            text(
                """
                UPDATE domain_consents SET revoked_at = :now
                WHERE workspace_id = :workspace_id AND owner_id = :owner_id
                  AND domain_key = :domain_key AND revoked_at IS NULL
                """
            ),
            {
                "now": now,
                "workspace_id": auth.workspace_id,
                "owner_id": auth.user_id,
                "domain_key": domain_key,
            },
        )
        session.execute(
            text(
                """
                UPDATE personal_domains SET enabled = false, updated_at = :now,
                    updated_by = :actor_id, version = version + 1
                WHERE id = :id
                """
            ),
            {"now": now, "actor_id": auth.user_id, "id": domain.id},
        )

        job_id = uuid4()
        session.execute(
            text(
                """
                INSERT INTO deletion_jobs (
                    id, workspace_id, owner_id, domain_key, scope, status,
                    requested_at, completed_at, created_by
                ) VALUES (
                    :id, :workspace_id, :owner_id, :domain_key, 'domain', 'completed',
                    :now, :now, :actor_id
                )
                """
            ),
            {
                "id": job_id,
                "workspace_id": auth.workspace_id,
                "owner_id": auth.user_id,
                "domain_key": domain_key,
                "now": now,
                "actor_id": auth.user_id,
            },
        )
        response = DomainDeletionResponse(
            id=job_id, domain_key=domain_key, status="completed", requested_at=now, completed_at=now
        )
        store_idempotency(
            session, auth, idempotency_key, req_hash, response.model_dump(mode="json"), now
        )

    # Transaction above has committed. Release `session`'s pooled
    # connection before the deferred, best-effort Google-side revoke's
    # potentially slow, blocking network call -- see `gmail_revocation.py`'s
    # own module docstring, matching `domains.py:_disable_domain`'s
    # identical shape for the same reason.
    if pending_gmail_revokes:
        session.close()
        finish_gmail_revocation(pending_gmail_revokes)
    return response
