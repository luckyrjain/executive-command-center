from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from ecc.auth import AuthDep, CsrfDep
from ecc.database import get_session
from ecc.domains.knowledge.entities import EntityCreate, EntityResponse, create_entity_core

router = APIRouter(prefix="/api/v1/identity", tags=["identity"])
SessionDep = Annotated[Session, Depends(get_session)]
IdempotencyHeader = Annotated[
    str,
    Header(alias="Idempotency-Key", min_length=1, max_length=255),
]


class PersonOrganizationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    canonical_name: str = Field(min_length=1, max_length=500)
    summary: str | None = Field(default=None, max_length=5000)


@router.post("/people", response_model=EntityResponse, status_code=status.HTTP_201_CREATED)
def create_person(
    payload: PersonOrganizationCreate,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
    idempotency_key: IdempotencyHeader,
) -> EntityResponse:
    entity_payload = EntityCreate(kind="person", **payload.model_dump())
    return create_entity_core(entity_payload, request, auth, session, idempotency_key)


@router.post("/organizations", response_model=EntityResponse, status_code=status.HTTP_201_CREATED)
def create_organization(
    payload: PersonOrganizationCreate,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
    idempotency_key: IdempotencyHeader,
) -> EntityResponse:
    entity_payload = EntityCreate(kind="organization", **payload.model_dump())
    return create_entity_core(entity_payload, request, auth, session, idempotency_key)
