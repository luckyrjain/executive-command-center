from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
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
        connection.execute(
            text(
                "INSERT INTO users (id, workspace_id, email, password_hash, created_at) "
                "VALUES (:id, :workspace_id, :email, :password_hash, :created_at)"
            ),
            {
                "id": user_b,
                "workspace_id": workspace_b,
                "email": f"{user_b}@example.com",
                "password_hash": "hash",
                "created_at": datetime.now(UTC),
            },
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
