"""Create or rotate a local development workspace, user, and browser session."""

from __future__ import annotations

import os
import secrets
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from hmac import new
from urllib.parse import urlsplit
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
    if os.getenv("ECC_ENV", "development").casefold() != "development":
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
        WHERE w.name = %s AND u.email = %s
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
    workspace_id = uuid4()
    user_id = uuid4()
    cursor.execute(
        "INSERT INTO workspaces (id, name, created_at, timezone) VALUES (%s, %s, %s, %s)",
        (workspace_id, "Local Development", now, "Asia/Kolkata"),
    )
    cursor.execute(
        """
        INSERT INTO users (id, workspace_id, email, password_hash, created_at)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (
            user_id,
            workspace_id,
            "local@example.com",
            "development-bootstrap-no-password-login",
            now,
        ),
    )
    return workspace_id, user_id


def main() -> None:
    session_secret = os.getenv("ECC_SESSION_SECRET", "")
    if len(session_secret) < 32:
        raise SystemExit("ECC_SESSION_SECRET must contain at least 32 characters.")

    database_url = _database_url()
    _validate_environment(database_url)

    session_token = secrets.token_urlsafe(32)
    token_hash = sha256(session_token.encode()).hexdigest()
    csrf_token = new(session_secret.encode(), session_token.encode(), "sha256").hexdigest()
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
                    token_hash,
                    now + timedelta(days=30),
                    now,
                ),
            )
        connection.commit()

    print(f"Local development identity {action}; previous active sessions were revoked.\n")
    print(f"Workspace ID: {workspace_id}")
    print(f"User ID:      {user_id}")
    print("\nOpen http://localhost:5173 and run these in the browser console:\n")
    print(
        f'document.cookie = "ecc_session={session_token}; Path=/; SameSite=Strict";'
    )
    print(f'document.cookie = "ecc_csrf={csrf_token}; Path=/; SameSite=Strict";')
    print("\nThese JavaScript-set cookies are for local development only. Reload the page.")


if __name__ == "__main__":
    main()
