from datetime import UTC, datetime, timedelta
from hashlib import sha256
from json import dumps
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from ecc.auth import AuthContext
from ecc.domains.governance.recommendation_models import RecommendationResponse

FIELDS = """
id, recommendation_type, target_type, target_id, proposed_action, expected_version,
rationale, confidence, status, evidence_ids, expires_at, confirmed_by, confirmed_at,
execution_result, source, pinned, deferred_until, created_by, updated_by, created_at,
updated_at, version, archived_at, pre_archive_status
"""


def project(row: dict[str, Any]) -> RecommendationResponse:
    payload = dict(row)
    payload["confidence"] = float(payload["confidence"])
    return RecommendationResponse(**payload)


def request_hash(payload: BaseModel, action: str) -> str:
    material = {"action": action, "payload": payload.model_dump(mode="json")}
    return sha256(dumps(material, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def lock_idempotency(session: Session, auth: AuthContext, key: str) -> None:
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"recommendation:{auth.workspace_id}:{auth.user_id}:{key}"},
    )


def load_cached(
    session: Session,
    auth: AuthContext,
    key: str,
    digest: str,
) -> RecommendationResponse | None:
    row = (
        session.execute(
            text(
                """
            SELECT request_hash, response_body
            FROM idempotency_records
            WHERE workspace_id=:workspace_id AND actor_id=:actor_id
              AND key=:key AND expires_at>:now
            """
            ),
            {
                "workspace_id": auth.workspace_id,
                "actor_id": auth.user_id,
                "key": key,
                "now": datetime.now(UTC),
            },
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        return None
    if row["request_hash"] != digest:
        session.rollback()
        raise HTTPException(status_code=409, detail="IDEMPOTENCY_CONFLICT")
    session.rollback()
    return RecommendationResponse.model_validate(row["response_body"])


def save_cached(
    session: Session,
    auth: AuthContext,
    key: str,
    digest: str,
    response: RecommendationResponse,
    status_code: int,
    now: datetime,
) -> None:
    session.execute(
        text(
            """
            INSERT INTO idempotency_records (
                workspace_id, actor_id, key, request_hash, response_status,
                response_body, created_at, expires_at
            ) VALUES (
                :workspace_id, :actor_id, :key, :request_hash, :response_status,
                CAST(:response_body AS jsonb), :created_at, :expires_at
            )
            """
        ),
        {
            "workspace_id": auth.workspace_id,
            "actor_id": auth.user_id,
            "key": key,
            "request_hash": digest,
            "response_status": status_code,
            "response_body": response.model_dump_json(),
            "created_at": now,
            "expires_at": now + timedelta(hours=24),
        },
    )


def get_row(
    session: Session,
    auth: AuthContext,
    recommendation_id: UUID,
    *,
    for_update: bool = False,
) -> dict[str, Any]:
    query = f"SELECT {FIELDS} FROM recommendations WHERE workspace_id=:workspace_id AND id=:recommendation_id"
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
                SET status='expired', version=version+1, updated_at=:now, updated_by=:actor
                WHERE workspace_id=:workspace_id AND id=:recommendation_id
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
        return dict(updated)
    return row
