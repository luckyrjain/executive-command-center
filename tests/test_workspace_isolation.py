from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from identity_fixtures import create_identity
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from ecc.database import engine


def test_session_cannot_reference_user_from_another_workspace() -> None:
    workspace_a = uuid4()
    workspace_b = uuid4()
    user_b = uuid4()

    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO workspaces (id, name, created_at) VALUES (:id, :name, :created_at)"),
            [
                {"id": workspace_a, "name": "A", "created_at": datetime.now(UTC)},
                {"id": workspace_b, "name": "B", "created_at": datetime.now(UTC)},
            ],
        )
        create_identity(
            connection,
            workspace_id=workspace_b,
            user_id=user_b,
            email=f"{user_b}@example.com",
            now=datetime.now(UTC),
            password_hash="hash",
        )

    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO sessions "
                "(id, workspace_id, user_id, token_hash, expires_at, last_seen_at) "
                "VALUES (:id, :workspace_id, :user_id, :token_hash, :expires_at, :last_seen_at)"
            ),
            {
                "id": uuid4(),
                "workspace_id": workspace_a,
                "user_id": user_b,
                "token_hash": uuid4().hex + uuid4().hex,
                "expires_at": datetime.now(UTC) + timedelta(hours=1),
                "last_seen_at": datetime.now(UTC),
            },
        )


def test_pkos_edge_cannot_cross_workspaces() -> None:
    workspace_a = uuid4()
    workspace_b = uuid4()
    node_a = uuid4()
    node_b = uuid4()
    now = datetime.now(UTC)

    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO workspaces (id, name, created_at) VALUES (:id, :name, :created_at)"),
            [
                {"id": workspace_a, "name": "A", "created_at": now},
                {"id": workspace_b, "name": "B", "created_at": now},
            ],
        )
        create_identity(connection, workspace_id=workspace_a, user_id=uuid4(), now=now)
        create_identity(connection, workspace_id=workspace_b, user_id=uuid4(), now=now)
        connection.execute(
            text(
                "INSERT INTO pkos_nodes "
                "(id, workspace_id, node_type, canonical_name, created_at, updated_at) "
                "VALUES (:id, :workspace_id, 'person', :name, :created_at, :updated_at)"
            ),
            [
                {
                    "id": node_a,
                    "workspace_id": workspace_a,
                    "name": "Node A",
                    "created_at": now,
                    "updated_at": now,
                },
                {
                    "id": node_b,
                    "workspace_id": workspace_b,
                    "name": "Node B",
                    "created_at": now,
                    "updated_at": now,
                },
            ],
        )

    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO pkos_edges "
                "(id, workspace_id, source_node_id, target_node_id, edge_type) "
                "VALUES (:id, :workspace_id, :source_node_id, :target_node_id, 'related_to')"
            ),
            {
                "id": uuid4(),
                "workspace_id": workspace_a,
                "source_node_id": node_a,
                "target_node_id": node_b,
            },
        )
