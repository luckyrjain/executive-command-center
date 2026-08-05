"""Create or rotate a local development workspace, user, and bootstrap code."""

from __future__ import annotations

import os
import secrets
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from urllib.parse import quote, urlsplit
from uuid import UUID, uuid4

import psycopg

_LOCAL_DATABASE_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _database_url() -> str:
    value = os.getenv(
        "ECC_DATABASE_URL",
        "postgresql+psycopg://ecc:ecc@localhost:5432/ecc",
    )
    if value.startswith("postgresql+psycopg://"):
        return value.replace("postgresql+psycopg://", "postgresql://", 1)
    if value.startswith("postgresql://"):
        return value
    raise SystemExit("ECC_DATABASE_URL must use postgresql or postgresql+psycopg.")


def _allow_remote_database() -> bool:
    return os.getenv("ECC_BOOTSTRAP_ALLOW_REMOTE_DATABASE", "").casefold() in {
        "1",
        "true",
        "yes",
    }


def _validate_environment(database_url: str) -> None:
    if os.getenv("ECC_ENV", "").casefold() != "development":
        raise SystemExit("bootstrap_dev.py may only run when ECC_ENV=development.")

    hostname = urlsplit(database_url).hostname
    if hostname not in _LOCAL_DATABASE_HOSTS and not _allow_remote_database():
        raise SystemExit(
            "Refusing to bootstrap a non-local database. Set "
            "ECC_BOOTSTRAP_ALLOW_REMOTE_DATABASE=1 only for an isolated development database."
        )


def _existing_identity(cursor: psycopg.Cursor[tuple[object, ...]]) -> tuple[UUID, UUID] | None:
    cursor.execute(
        """
        SELECT u.workspace_id, u.id
        FROM users AS u
        JOIN workspaces AS w ON w.id = u.workspace_id
        JOIN accounts AS a ON a.id = u.account_id
        WHERE w.name = %s AND a.email = %s
        ORDER BY w.created_at DESC
        LIMIT 1
        """,
        ("Local Development", "local@example.com"),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    return UUID(str(row[0])), UUID(str(row[1]))


def _create_identity(
    cursor: psycopg.Cursor[tuple[object, ...]],
    now: datetime,
) -> tuple[UUID, UUID]:
    # Phase 8 Task 1 (docs/superpowers/specs/2026-08-01-phase-8-multi-user-
    # design.md Decision 1): identity is now split across `accounts`
    # (workspace-independent, what a person authenticates as) and `users`
    # (unchanged FK anchor, gains account_id) plus `workspace_memberships`
    # (the mutable role/status this dev identity's own 'owner'/'active' row
    # records). This dev-only bootstrap account still never gets a real
    # password login -- `password_hash` stays the same placeholder string
    # `POST /identity/auth/login` can never verify against, since this
    # identity is only ever reached through the /dev/bootstrap code-exchange
    # flow below, not real credentials.
    workspace_id = uuid4()
    account_id = uuid4()
    user_id = uuid4()
    cursor.execute(
        "INSERT INTO workspaces (id, name, created_at, timezone) VALUES (%s, %s, %s, %s)",
        (workspace_id, "Local Development", now, "Asia/Kolkata"),
    )
    cursor.execute(
        """
        INSERT INTO accounts (id, email, password_hash, display_name, created_at)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (
            account_id,
            "local@example.com",
            "development-bootstrap-no-password-login",
            "Local Development",
            now,
        ),
    )
    cursor.execute(
        "INSERT INTO users (id, workspace_id, account_id, created_at) VALUES (%s, %s, %s, %s)",
        (user_id, workspace_id, account_id, now),
    )
    cursor.execute(
        """
        INSERT INTO workspace_memberships (
            id, workspace_id, account_id, users_id, role, status, invited_by, created_at, updated_at
        ) VALUES (%s, %s, %s, %s, 'owner', 'active', %s, %s, %s)
        """,
        (uuid4(), workspace_id, account_id, user_id, user_id, now, now),
    )
    return workspace_id, user_id


def main() -> None:
    session_secret = os.getenv("ECC_SESSION_SECRET", "")
    if len(session_secret) < 32:
        raise SystemExit("ECC_SESSION_SECRET must contain at least 32 characters.")

    database_url = _database_url()
    _validate_environment(database_url)

    bootstrap_code = secrets.token_urlsafe(32)
    bootstrap_hash = sha256(bootstrap_code.encode()).hexdigest()
    now = datetime.now(UTC)

    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            identity = _existing_identity(cursor)
            if identity is None:
                workspace_id, user_id = _create_identity(cursor, now)
                action = "created"
            else:
                workspace_id, user_id = identity
                action = "reused"

            cursor.execute(
                """
                UPDATE sessions
                SET revoked_at = %s
                WHERE workspace_id = %s AND user_id = %s AND revoked_at IS NULL
                """,
                (now, workspace_id, user_id),
            )
            cursor.execute(
                """
                INSERT INTO sessions (
                    id, workspace_id, user_id, token_hash, expires_at,
                    last_seen_at, revoked_at
                ) VALUES (%s, %s, %s, %s, %s, %s, NULL)
                """,
                (
                    uuid4(),
                    workspace_id,
                    user_id,
                    bootstrap_hash,
                    now + timedelta(minutes=15),
                    now,
                ),
            )
        connection.commit()

    bootstrap_url = f"http://localhost:8000/dev/bootstrap#code={quote(bootstrap_code, safe='')}"
    print(f"Local development identity {action}; previous active sessions were revoked.\n")
    print(f"Workspace ID: {workspace_id}")
    print(f"User ID:      {user_id}")
    print("\nStart the backend, then open this one-time URL within 15 minutes:\n")
    print(bootstrap_url)
    print("\nThe backend will exchange the code for an HttpOnly seven-day session cookie.")

    print("\n--- AI runtime prerequisite check ---")
    # Subprocess, not import: keeps check_ollama_models.py's own standalone-
    # script shape (see its docstring) and sidesteps mypy_path=backend not
    # covering scripts/-to-scripts/ imports. Non-fatal either way -- AI
    # enrichment is opt-in and off by default (ECC_MEETING_PREP_AI_
    # ENRICHMENT_ENABLED etc., ecc/config.py), so a missing/unreachable
    # Ollama install must not block bootstrap of the deterministic core.
    sys.stdout.flush()
    subprocess.run(
        [sys.executable, str(Path(__file__).parent / "check_ollama_models.py")], check=False
    )


if __name__ == "__main__":
    main()
