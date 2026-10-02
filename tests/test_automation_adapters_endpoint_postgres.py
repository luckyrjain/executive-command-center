"""`GET /api/v1/automations/adapters` (scope-enforcement design, Decision 5
surfacing table): every registered adapter's static declarations plus the
closed scope vocabularies, for an active member only.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from automation_scope_support import World, client_for, make_world
from sqlalchemy import text

from ecc.config import get_settings
from ecc.database import engine
from ecc.domains.automation.adapter_contract import ACTION_TYPES, DATA_CLASSES
from ecc.domains.automation.adapters import registry as production_registry

settings = get_settings()
pytestmark = pytest.mark.skipif(
    not settings.database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)


@pytest.fixture
def world() -> Iterator[World]:
    yield from make_world()


def test_lists_every_registered_adapter_and_the_vocabularies(world: World) -> None:
    with client_for(world) as client:
        response = client.get("/api/v1/automations/adapters")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["action_types"] == sorted(ACTION_TYPES)
    assert body["data_classes"] == list(DATA_CLASSES)
    by_id = {a["adapter_id"]: a for a in body["adapters"]}
    assert set(by_id) == set(production_registry.adapter_ids())
    assert by_id["github.add_issue_comment"] == {
        "adapter_id": "github.add_issue_comment",
        "action_type": "comment.create",
        "data_class": "sensitive",
        "reversible": True,
        "high_impact_categories": ["public"],
        "has_dispatch_value": False,
    }


def test_rejects_a_caller_without_an_active_membership(world: World) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE workspace_memberships SET status = 'removed' "
                "WHERE workspace_id = :ws AND users_id = :uid"
            ),
            {"ws": world.workspace_id, "uid": world.user_id},
        )
    with client_for(world) as client:
        response = client.get("/api/v1/automations/adapters")
    assert response.status_code in (401, 403)


def test_requires_a_session() -> None:
    from fastapi.testclient import TestClient

    from ecc.main import app

    with TestClient(app) as client:
        assert client.get("/api/v1/automations/adapters").status_code == 401
