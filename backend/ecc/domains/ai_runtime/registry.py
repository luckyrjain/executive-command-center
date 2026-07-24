"""Model/provider registry reads (`model_definitions`).

`docs/phases/phase-004/DATA-MODEL.md`: `model_definitions` is the platform's
approved model/provider catalog, **not** workspace-scoped user data (see
migration `0028_phase4_model_registry.py`'s module docstring for why) --
every workspace this deployment serves reads the same rows. This module
owns reads only; there is no write path in this activation (the sole row is
seeded by the migration, design doc Decision 1) and no endpoint accepts a
caller-supplied `model_id`/`provider` (`MODEL-ROUTING-CONTRACT.md`).
"""

from dataclasses import dataclass
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from ecc.auth import AuthDep
from ecc.database import get_session

Deployment = Literal["local", "remote"]
ModelStatus = Literal["active", "disabled"]

_MODEL_FIELDS = """
    id, provider, model_id, deployment, data_classes, capabilities,
    context_window_tokens, structured_output_supported, status
"""


@dataclass(frozen=True, slots=True)
class ModelDefinition:
    """A single registered model/provider capability row.

    `data_classes`/`capabilities` are tuples (not lists) so instances stay
    hashable and safely shareable across the router's cached snapshot
    (Decision 2: "cached registry ... snapshot refreshed on a short
    interval", never mutated in place by a caller).
    """

    id: UUID
    provider: str
    model_id: str
    deployment: Deployment
    data_classes: tuple[str, ...]
    capabilities: tuple[str, ...]
    context_window_tokens: int
    structured_output_supported: bool
    status: ModelStatus


def _row_to_model(row: dict[str, Any]) -> ModelDefinition:
    return ModelDefinition(
        id=row["id"],
        provider=row["provider"],
        model_id=row["model_id"],
        deployment=row["deployment"],
        data_classes=tuple(row["data_classes"]),
        capabilities=tuple(row["capabilities"]),
        context_window_tokens=row["context_window_tokens"],
        structured_output_supported=row["structured_output_supported"],
        status=row["status"],
    )


def list_models(session: Session, *, include_disabled: bool = False) -> list[ModelDefinition]:
    """List registered models, ordered deterministically by `model_id`.

    Ascending `model_id` ordering matches `MODEL-ROUTING-CONTRACT.md`'s
    preference-stage tie-break (Decision 2 step 5) -- callers that consume
    this list in order already see candidates in the router's own final
    tie-break order, without re-sorting.
    """
    clause = "" if include_disabled else "WHERE status = 'active'"
    rows = (
        session.execute(
            text(f"SELECT {_MODEL_FIELDS} FROM model_definitions {clause} ORDER BY model_id ASC")
        )
        .mappings()
        .all()
    )
    return [_row_to_model(dict(row)) for row in rows]


def get_model(session: Session, model_id: str) -> ModelDefinition | None:
    """Look up a single registered model by its opaque `model_id` string.

    Never raises on a missing row -- callers (the router, `GET /ai/models`)
    decide how to surface "not found", matching this codebase's convention
    of keeping lookup and HTTP-error-shaping separate.
    """
    row = (
        session.execute(
            text(f"SELECT {_MODEL_FIELDS} FROM model_definitions WHERE model_id = :model_id"),
            {"model_id": model_id},
        )
        .mappings()
        .one_or_none()
    )
    return _row_to_model(dict(row)) if row is not None else None


# --- GET /api/v1/ai/models -------------------------------------------------
#
# `API-SCHEMAS.md`: "GET /ai/models" is part of the "Resolved administrative/
# runtime surface" and requires "local-owner authority" like the rest of
# that surface. This codebase has no separate admin/owner role distinct
# from an authenticated session (`ecc.auth.AuthContext` carries only
# `workspace_id`/`user_id`/`timezone` -- verified against every existing
# Phase 1-3 endpoint, none of which checks a role beyond session validity)
# -- for this single-operator, local-first deployment, "the local owner" is
# exactly "whoever holds a valid session for this workspace", so `AuthDep`
# alone matches every other endpoint's authority check and is what "local-
# owner scoped" resolves to here. `model_definitions` is global platform
# catalog data (see this module's docstring), so there is nothing
# workspace-specific to further scope or 404 on -- every authenticated
# caller sees the same rows, matching how `ADR-0007`'s router itself never
# varies by workspace.

router = APIRouter(prefix="/api/v1/ai", tags=["ai-runtime"])
SessionDep = Annotated[Session, Depends(get_session)]


class ModelDefinitionResponse(BaseModel):
    provider: str
    model_id: str
    deployment: Deployment
    data_classes: list[str]
    capabilities: list[str]
    context_window_tokens: int
    structured_output_supported: bool
    status: ModelStatus


class ModelListResponse(BaseModel):
    models: list[ModelDefinitionResponse]


def _to_response(model: ModelDefinition) -> ModelDefinitionResponse:
    return ModelDefinitionResponse(
        provider=model.provider,
        model_id=model.model_id,
        deployment=model.deployment,
        data_classes=list(model.data_classes),
        capabilities=list(model.capabilities),
        context_window_tokens=model.context_window_tokens,
        structured_output_supported=model.structured_output_supported,
        status=model.status,
    )


@router.get("/models")
def list_models_endpoint(auth: AuthDep, session: SessionDep) -> ModelListResponse:
    """List every registered model, including disabled ones -- an
    administrative catalog view, not the router's own eligibility-filtered
    candidate list (`router.route()` applies `MODEL-ROUTING-CONTRACT.md`'s
    pipeline; this endpoint returns the raw registry).
    """
    # AuthDep's presence is the authority check itself (see the block
    # comment above) -- nothing further to scope by workspace.
    models = list_models(session, include_disabled=True)
    return ModelListResponse(models=[_to_response(model) for model in models])
