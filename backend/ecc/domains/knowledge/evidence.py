from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from ecc.auth import AuthDep
from ecc.database import get_session

router = APIRouter(prefix="/api/v1/evidence", tags=["evidence"])
SessionDep = Annotated[Session, Depends(get_session)]
IdsQuery = Annotated[list[UUID] | None, Query(alias="id")]

EvidenceStatus = Literal["available", "missing"]


class EvidenceItem(BaseModel):
    id: UUID
    status: EvidenceStatus
    source_type: str | None
    label: str | None
    captured_at: datetime | None


class EvidenceListResponse(BaseModel):
    items: list[EvidenceItem]


@router.get("", response_model=EvidenceListResponse)
def resolve_evidence(
    auth: AuthDep,
    session: SessionDep,
    ids: IdsQuery = None,
) -> EvidenceListResponse:
    requested = ids or []
    if not requested:
        return EvidenceListResponse(items=[])

    rows = (
        session.execute(
            text(
                """
                SELECT e.id AS id, e.source_type AS source_type,
                       e.captured_at AS captured_at, n.canonical_name AS label
                FROM pkos_evidence AS e
                JOIN pkos_nodes AS n
                  ON n.workspace_id = e.workspace_id AND n.id = e.node_id
                WHERE e.workspace_id = :workspace_id
                  AND e.id = ANY(CAST(:ids AS uuid[]))
                """
            ),
            {"workspace_id": auth.workspace_id, "ids": requested},
        )
        .mappings()
        .all()
    )
    found = {row["id"]: row for row in rows}

    items: list[EvidenceItem] = []
    for evidence_id in requested:
        row = found.get(evidence_id)
        if row is None:
            items.append(
                EvidenceItem(
                    id=evidence_id,
                    status="missing",
                    source_type=None,
                    label=None,
                    captured_at=None,
                )
            )
        else:
            items.append(
                EvidenceItem(
                    id=evidence_id,
                    status="available",
                    source_type=row["source_type"],
                    label=row["label"],
                    captured_at=row["captured_at"],
                )
            )
    return EvidenceListResponse(items=items)
