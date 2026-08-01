"""Shared test-fixture helper for creating a full Phase 8 identity (an
`accounts` row, a `users` row anchoring it to one workspace, and an
`active`/`owner` `workspace_memberships` row) in one call.

Phase 8 Task 1 (`docs/superpowers/specs/2026-08-01-phase-8-multi-user-
design.md` Decision 1) split what used to be a single `INSERT INTO users
(id, workspace_id, email, password_hash, created_at)` statement across
three tables. Every pre-existing `*_postgres.py` test file that seeds its
own fixture identity via raw SQL now calls `create_identity` here instead
of inlining that three-table sequence itself -- one shared implementation
rather than ~75 near-identical copies, the same reasoning `phase1_dataset.
py` already established for this test suite's larger fixture datasets.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID, uuid4

from sqlalchemy import text


class _Executable(Protocol):
    def execute(self, statement: Any, parameters: Any = None) -> Any: ...


def create_identity(
    conn: _Executable,
    *,
    workspace_id: UUID,
    user_id: UUID,
    email: str | None = None,
    now: datetime | None = None,
    password_hash: str = "test-password-hash",
    role: str = "owner",
    status: str = "active",
) -> UUID:
    """Insert `accounts`/`users`/`workspace_memberships` rows for one test
    identity and return the new `accounts.id`. `user_id` is caller-supplied
    (not generated here) so existing call sites that already generated a
    `user_id` local for later use as `owner_id`/`created_by`/session
    scoping need no further changes beyond this one call.
    """
    now = now if now is not None else datetime.now(UTC)
    email = email if email is not None else f"{user_id}@example.test"
    account_id = uuid4()

    conn.execute(
        text(
            """
            INSERT INTO accounts (id, email, password_hash, display_name, created_at)
            VALUES (:id, :email, :password_hash, :display_name, :created_at)
            """
        ),
        {
            "id": account_id,
            "email": email,
            "password_hash": password_hash,
            "display_name": email.split("@", 1)[0],
            "created_at": now,
        },
    )
    conn.execute(
        text(
            "INSERT INTO users (id, workspace_id, account_id, created_at) "
            "VALUES (:id, :workspace_id, :account_id, :created_at)"
        ),
        {"id": user_id, "workspace_id": workspace_id, "account_id": account_id, "created_at": now},
    )
    conn.execute(
        text(
            """
            INSERT INTO workspace_memberships (
                id, workspace_id, account_id, users_id, role, status,
                invited_by, created_at, updated_at
            ) VALUES (
                :id, :workspace_id, :account_id, :users_id, :role, :status,
                :users_id, :created_at, :created_at
            )
            """
        ),
        {
            "id": uuid4(),
            "workspace_id": workspace_id,
            "account_id": account_id,
            "users_id": user_id,
            "role": role,
            "status": status,
            "created_at": now,
        },
    )
    return account_id


def add_membership(
    conn: _Executable,
    *,
    workspace_id: UUID,
    account_id: UUID,
    users_id: UUID,
    role: str = "member",
    status: str = "active",
    now: datetime | None = None,
) -> None:
    """Attach an *already-existing* account to a second workspace: a new
    `users` row (that workspace's own FK anchor for this account, per
    Decision 1's "one `users` row per account per workspace" shape) plus
    its own `workspace_memberships` row. Use this, not a second
    `create_identity` call, whenever a test needs one account with active
    memberships in more than one workspace -- `create_identity` always
    mints a fresh `accounts` row, which would create two unrelated
    identities rather than one multi-workspace one.
    """
    now = now if now is not None else datetime.now(UTC)
    conn.execute(
        text(
            "INSERT INTO users (id, workspace_id, account_id, created_at) "
            "VALUES (:id, :workspace_id, :account_id, :created_at)"
        ),
        {"id": users_id, "workspace_id": workspace_id, "account_id": account_id, "created_at": now},
    )
    conn.execute(
        text(
            """
            INSERT INTO workspace_memberships (
                id, workspace_id, account_id, users_id, role, status,
                invited_by, created_at, updated_at
            ) VALUES (
                :id, :workspace_id, :account_id, :users_id, :role, :status,
                :users_id, :created_at, :created_at
            )
            """
        ),
        {
            "id": uuid4(),
            "workspace_id": workspace_id,
            "account_id": account_id,
            "users_id": users_id,
            "role": role,
            "status": status,
            "created_at": now,
        },
    )
