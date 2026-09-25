"""Invariants that keep the f-string SQL builders flagged by Snyk Code
(python/Sqli) safe -- pure unit tests, no database.

Each `PATCH` endpoint below builds `SET {field} = :{field}` from
`payload.model_fields_set`. That is only safe while the model forbids extra
keys: with `extra="allow"`, an arbitrary request key would land in
`model_fields_set` and be interpolated into the SQL text as a column name.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel, ValidationError

from ecc.domains.calendar.events import CalendarEventPatch
from ecc.domains.communication.commitments import CommitmentPatch
from ecc.domains.governance.risk_mutations import RiskPatch
from ecc.domains.knowledge.notes import NotePatch
from ecc.domains.planning.tasks import TaskPatch
from ecc.platform import authz

_SET_BUILDER_PATCH_MODELS: tuple[type[BaseModel], ...] = (
    CalendarEventPatch,
    CommitmentPatch,
    RiskPatch,
    NotePatch,
    TaskPatch,
)

_INJECTION_KEY = "version = 1, owner_id = NULL --"


@pytest.mark.parametrize("model", _SET_BUILDER_PATCH_MODELS, ids=lambda m: m.__name__)
def test_set_builder_patch_models_forbid_extra_keys(model: type[BaseModel]) -> None:
    assert model.model_config.get("extra") == "forbid"
    with pytest.raises(ValidationError, match="extra_forbidden"):
        model.model_validate({"expected_version": 1, _INJECTION_KEY: "x"})


@pytest.mark.parametrize("value", ["risks; DROP TABLE risks", "risks\n", "pg_catalog.pg_user"])
def test_grantable_resource_type_rejects_injection_shapes(value: str) -> None:
    """`authz_grants` interpolates `payload.resource_type` as a table name
    only after `require_grantable` accepts it."""
    with pytest.raises(authz.UnknownResourceTypeError):
        authz.require_grantable(value)
