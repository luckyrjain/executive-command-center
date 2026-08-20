from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from ecc.auth import AuthContext
from ecc.domains.governance.recommendation_events import record_event
from ecc.domains.governance.recommendation_models import RecommendationResponse
from ecc.platform import idempotency

FIELDS = """
id, recommendation_type, target_type, target_id, proposed_action, proposed_fields,
expected_version, rationale, confidence, status, evidence_ids, expires_at, confirmed_by,
confirmed_at, execution_result, source, pinned, deferred_until, created_by, updated_by,
created_at, updated_at, version, archived_at, pre_archive_status
"""


def project(row: dict[str, Any]) -> RecommendationResponse:
    payload = dict(row)
    payload["confidence"] = float(payload["confidence"])
    return RecommendationResponse(**payload)


def request_hash(payload: BaseModel, action: str) -> str:
    return idempotency.request_hash(payload, action)


def lock_idempotency(session: Session, auth: AuthContext, key: str) -> None:
    idempotency.lock_idempotency(session, auth, key)


def load_cached(
    session: Session,
    auth: AuthContext,
    key: str,
    digest: str,
) -> RecommendationResponse | None:
    """Thin wrapper, not a straight delegation -- every one of this
    module's own callers (`recommendation_mutations.py`'s `_start`) calls
    this immediately after `lock_idempotency`, with no `session.begin()`
    of its own anywhere in between: the `pg_advisory_xact_lock` and this
    read share one continuous implicit transaction that only ends at the
    caller's own explicit `session.commit()` after the real mutation
    runs. A cache HIT (or a conflict) means nothing further happens in
    this transaction, so `session.rollback()` closes the now-done
    read-only transaction out cleanly. A cache MISS must NOT roll back --
    that would release the just-acquired advisory lock immediately,
    before the mutation it exists to serialize ever runs, defeating the
    lock's entire purpose for two concurrent requests sharing the same
    `Idempotency-Key`. The shared `ecc.platform.idempotency.load_cached`
    itself never rolls back (most of its other callers are already
    inside their own `session.begin()` block, where a mid-block rollback
    would incorrectly discard work already done), so this conditional
    rollback has to stay here rather than move into the shared helper.
    """
    try:
        result = idempotency.load_cached(
            session,
            auth,
            key,
            digest,
            domain="recommendations",
            response_model=RecommendationResponse,
        )
    except HTTPException:
        session.rollback()
        raise
    if result is not None:
        session.rollback()
    return result


def save_cached(
    session: Session,
    auth: AuthContext,
    key: str,
    digest: str,
    response: RecommendationResponse,
    status_code: int,
    now: datetime,
) -> None:
    idempotency.store_idempotency(
        session,
        auth,
        key,
        digest,
        response.model_dump(mode="json"),
        now,
        response_status=status_code,
    )


def get_row(
    session: Session,
    auth: AuthContext,
    recommendation_id: UUID,
    *,
    for_update: bool = False,
) -> dict[str, Any]:
    query = (
        f"SELECT {FIELDS} FROM recommendations "
        "WHERE workspace_id=:workspace_id AND id=:recommendation_id"
    )
    if for_update:
        query += " FOR UPDATE"
    row = (
        session.execute(
            text(query),
            {"workspace_id": auth.workspace_id, "recommendation_id": recommendation_id},
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="RECOMMENDATION_NOT_FOUND")
    return dict(row)


def check_version(row: dict[str, Any], expected_version: int) -> None:
    if int(row["version"]) != expected_version:
        raise HTTPException(status_code=409, detail="VERSION_CONFLICT")


def expire_if_needed(
    session: Session,
    auth: AuthContext,
    row: dict[str, Any],
    *,
    request: Request | None = None,
) -> dict[str, Any]:
    if (
        row["expires_at"] is not None
        and row["expires_at"] <= datetime.now(UTC)
        and row["status"] in {"proposed", "pending_confirmation"}
    ):
        updated = (
            session.execute(
                text(
                    f"""
                    UPDATE recommendations
                    SET status='expired', version=version+1,
                        updated_at=:now, updated_by=:actor
                    WHERE workspace_id=:workspace_id
                      AND id=:recommendation_id
                    RETURNING {FIELDS}
                    """
                ),
                {
                    "now": datetime.now(UTC),
                    "actor": auth.user_id,
                    "workspace_id": auth.workspace_id,
                    "recommendation_id": row["id"],
                },
            )
            .mappings()
            .one()
        )
        current = dict(updated)
        if request is not None:
            record_event(
                request,
                session,
                auth,
                current,
                "recommendation.expired",
                {"status": row["status"]},
                ["status"],
            )
        return current
    return row
