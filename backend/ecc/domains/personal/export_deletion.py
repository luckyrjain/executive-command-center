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
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from ecc.auth import AuthContext, AuthDep, CsrfDep

from .domains import DomainKey, SessionDep, _decrypt_payload, get_domain

router = APIRouter(prefix="/api/v1/personal", tags=["personal"])

# Every table this task's own scope populates under a domain -- deleted in
# this order so no foreign key (`goals` <- `routines` <- `check_ins`,
# `domain_sources` <- `domain_records`) is ever violated. `personal_
# insights`/`domain_consents` have no further children.
_CHILD_TABLES_DELETE_ORDER = (
    "check_ins",
    "routines",
    "goals",
    "domain_records",
    "domain_sources",
    "personal_insights",
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


def _delete_domain_data(session: Session, auth: AuthContext, domain_key: str) -> None:
    params = {"workspace_id": auth.workspace_id, "owner_id": auth.user_id, "domain_key": domain_key}
    for table in _CHILD_TABLES_DELETE_ORDER:
        session.execute(
            text(  # noqa: S608 -- table name is one of six fixed literals, never user input
                f"DELETE FROM {table} WHERE workspace_id = :workspace_id "
                "AND owner_id = :owner_id AND domain_key = :domain_key"
            ),
            params,
        )


@router.post("/domains/{domain_key}/delete", response_model=DomainDeletionResponse)
def delete_domain_endpoint(
    domain_key: DomainKey, auth: AuthDep, session: SessionDep, _csrf: CsrfDep
) -> DomainDeletionResponse:
    now = datetime.now(UTC)
    with session.begin():
        domain = get_domain(session, auth, domain_key)
        if domain is None:
            raise HTTPException(status_code=404, detail="DOMAIN_NOT_FOUND")

        _delete_domain_data(session, auth, domain_key)

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
        return DomainDeletionResponse(
            id=job_id, domain_key=domain_key, status="completed", requested_at=now, completed_at=now
        )
