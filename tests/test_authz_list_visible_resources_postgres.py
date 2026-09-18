"""`authz.list_visible_resources` -- the shared query helper behind 11 list
endpoints (architecture review, 2026-09-18). Tested directly against real
Postgres, using `risks` as the subject table, because the endpoint-level
suites only exercise it indirectly and a regression here is a cross-tenant /
cross-user data leak, not a cosmetic bug.

Pins its contract: visibility denial, grant honored/revoked, workspace
isolation, composition with extra filters/ordering/limit, and the failure
modes that must be loud (reserved bind-param override, unknown resource type,
a `limit_clause` whose `limit` param is missing).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from identity_fixtures import create_identity
from sqlalchemy import text
from sqlalchemy.exc import StatementError

from ecc.auth import AuthContext
from ecc.config import get_settings
from ecc.database import SessionFactory, engine
from ecc.platform import authz

settings = get_settings()
pytestmark = pytest.mark.skipif(
    not settings.database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)

_COLUMNS = "id, description, status, visibility"


@dataclass
class _World:
    workspace_id: UUID
    other_workspace_id: UUID
    owner_id: UUID
    member_id: UUID
    member_account_id: UUID
    workspace_risk: UUID
    private_risk: UUID
    shared_risk: UUID
    other_workspace_risk: UUID

    def auth(self, user_id: UUID) -> AuthContext:
        return AuthContext(workspace_id=self.workspace_id, user_id=user_id, timezone="UTC")


def _insert_risk(
    connection: object,
    *,
    workspace_id: UUID,
    owner_id: UUID,
    description: str,
    visibility: str,
    status: str = "identified",
    updated_at: datetime,
) -> UUID:
    risk_id = uuid4()
    connection.execute(  # type: ignore[attr-defined]
        text(
            """
            INSERT INTO risks (
                id, workspace_id, owner_id, description, probability, impact,
                status, pinned, created_by, updated_by, created_at, updated_at,
                version, visibility
            ) VALUES (
                :id, :workspace_id, :owner_id, :description, 3, 3,
                :status, false, :owner_id, :owner_id, :now, :now, 1, :visibility
            )
            """
        ),
        {
            "id": risk_id,
            "workspace_id": workspace_id,
            "owner_id": owner_id,
            "description": description,
            "status": status,
            "visibility": visibility,
            "now": updated_at,
        },
    )
    return risk_id


@pytest.fixture
def world() -> Iterator[_World]:
    workspace_id = uuid4()
    other_workspace_id = uuid4()
    owner_id = uuid4()
    member_id = uuid4()
    other_owner_id = uuid4()
    now = datetime.now(UTC)
    with engine.begin() as connection:
        for wid, name in ((workspace_id, "LVR A"), (other_workspace_id, "LVR B")):
            connection.execute(
                text(
                    "INSERT INTO workspaces (id, name, timezone, created_at) "
                    "VALUES (:id, :name, 'UTC', :now)"
                ),
                {"id": wid, "name": name, "now": now},
            )
        owner_account_id = create_identity(
            connection, workspace_id=workspace_id, user_id=owner_id, role="owner"
        )
        member_account_id = create_identity(
            connection, workspace_id=workspace_id, user_id=member_id, role="member"
        )
        other_account_id = create_identity(
            connection, workspace_id=other_workspace_id, user_id=other_owner_id, role="owner"
        )
        workspace_risk = _insert_risk(
            connection,
            workspace_id=workspace_id,
            owner_id=owner_id,
            description="workspace-visible",
            visibility="workspace",
            updated_at=now - timedelta(minutes=3),
        )
        private_risk = _insert_risk(
            connection,
            workspace_id=workspace_id,
            owner_id=owner_id,
            description="private",
            visibility="private",
            status="closed",
            updated_at=now - timedelta(minutes=2),
        )
        shared_risk = _insert_risk(
            connection,
            workspace_id=workspace_id,
            owner_id=owner_id,
            description="shared-explicitly",
            visibility="shared_explicitly",
            updated_at=now - timedelta(minutes=1),
        )
        other_workspace_risk = _insert_risk(
            connection,
            workspace_id=other_workspace_id,
            owner_id=other_owner_id,
            description="other-workspace",
            visibility="workspace",
            updated_at=now,
        )
    try:
        yield _World(
            workspace_id=workspace_id,
            other_workspace_id=other_workspace_id,
            owner_id=owner_id,
            member_id=member_id,
            member_account_id=member_account_id,
            workspace_risk=workspace_risk,
            private_risk=private_risk,
            shared_risk=shared_risk,
            other_workspace_risk=other_workspace_risk,
        )
    finally:
        with engine.begin() as connection:
            for wid in (workspace_id, other_workspace_id):
                for table in ("resource_grants", "risks", "workspace_memberships", "users"):
                    connection.execute(
                        text(f"DELETE FROM {table} WHERE workspace_id = :wid"),  # noqa: S608
                        {"wid": wid},
                    )
                connection.execute(text("DELETE FROM workspaces WHERE id = :wid"), {"wid": wid})
            connection.execute(
                text("DELETE FROM accounts WHERE id = ANY(:ids)"),
                {"ids": [owner_account_id, member_account_id, other_account_id]},
            )


def _visible_ids(world: _World, user_id: UUID, **kwargs: object) -> list[UUID]:
    with SessionFactory() as session:
        rows = authz.list_visible_resources(
            session,
            world.auth(user_id),
            resource_type="risks",
            columns=_COLUMNS,
            order_by="updated_at ASC, id ASC",
            **kwargs,  # type: ignore[arg-type]
        )
        return [row["id"] for row in rows]


def _grant(world: _World, *, revoked: bool = False, expired: bool = False) -> None:
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO resource_grants (
                    id, workspace_id, grantee_account_id, resource_type, resource_id,
                    actions, granted_by, expires_at, revoked_at, created_at
                ) VALUES (
                    :id, :workspace_id, :grantee, 'risks', :resource_id,
                    ARRAY['read'], :granted_by, :expires_at, :revoked_at, :now
                )
                """
            ),
            {
                "id": uuid4(),
                "workspace_id": world.workspace_id,
                "grantee": world.member_account_id,
                "resource_id": world.shared_risk,
                "granted_by": world.owner_id,
                "expires_at": now - timedelta(hours=1) if expired else None,
                "revoked_at": now if revoked else None,
                "now": now,
            },
        )


def test_owner_sees_own_rows_of_every_visibility(world: _World) -> None:
    assert _visible_ids(world, world.owner_id) == [
        world.workspace_risk,
        world.private_risk,
        world.shared_risk,
    ]


def test_member_sees_only_workspace_visible_rows_not_private_or_ungranted_shared(
    world: _World,
) -> None:
    assert _visible_ids(world, world.member_id) == [world.workspace_risk]


def test_active_grant_makes_a_shared_row_visible(world: _World) -> None:
    _grant(world)
    assert _visible_ids(world, world.member_id) == [world.workspace_risk, world.shared_risk]


@pytest.mark.parametrize("state", ["revoked", "expired"])
def test_revoked_or_expired_grant_does_not_grant_visibility(world: _World, state: str) -> None:
    _grant(world, revoked=state == "revoked", expired=state == "expired")
    assert _visible_ids(world, world.member_id) == [world.workspace_risk]


def test_other_workspaces_rows_are_never_returned(world: _World) -> None:
    for user_id in (world.owner_id, world.member_id):
        assert world.other_workspace_risk not in _visible_ids(world, user_id)


def test_extra_clauses_narrow_never_widen_visibility(world: _World) -> None:
    # `status = 'closed'` matches only the private row -- which the member may
    # not see, so an extra filter must not surface it for them.
    closed = {"extra_clauses": ["status = :status"], "extra_params": {"status": "closed"}}
    assert _visible_ids(world, world.member_id, **closed) == []
    assert _visible_ids(world, world.owner_id, **closed) == [world.private_risk]


def test_order_by_and_limit_clause_are_honored(world: _World) -> None:
    ids = _visible_ids(
        world,
        world.owner_id,
        limit_clause="LIMIT :limit",
        extra_params={"limit": 2},
    )
    assert ids == [world.workspace_risk, world.private_risk]


def test_extra_params_cannot_override_reserved_bind_params(world: _World) -> None:
    for reserved in ("workspace_id", "__authz_user_id"):
        with pytest.raises(ValueError, match="reserved bind params"):
            _visible_ids(world, world.owner_id, extra_params={reserved: uuid4()})


def test_unknown_resource_type_raises_before_querying(world: _World) -> None:
    with SessionFactory() as session, pytest.raises(authz.UnknownResourceTypeError):
        authz.list_visible_resources(
            session,
            world.auth(world.owner_id),
            resource_type="not_a_real_table; DROP TABLE risks",
            columns="id",
            order_by="id",
        )


def test_limit_clause_without_its_limit_param_fails_loudly(world: _World) -> None:
    with pytest.raises(StatementError, match="bind parameter .limit."):
        _visible_ids(world, world.owner_id, limit_clause="LIMIT :limit")
