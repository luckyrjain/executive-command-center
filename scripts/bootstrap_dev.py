"""Create a local development workspace, user, and browser session."""

from __future__ import annotations

import os
import secrets
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from hmac import new
from uuid import uuid4

import psycopg


def _database_url() -> str:
    value = os.getenv(
        "ECC_DATABASE_URL",
        "postgresql+psycopg://ecc:ecc@localhost:5432/ecc",
    )
    return value.replace("postgresql+psycopg://", "postgresql://", 1)


def main() -> None:
    session_secret = os.getenv("ECC_SESSION_SECRET", "")
    if len(session_secret) < 32:
        raise SystemExit("ECC_SESSION_SECRET must contain at least 32 characters.")

    workspace_id = uuid4()
    user_id = uuid4()
    session_token = secrets.token_urlsafe(32)
    token_hash = sha256(session_token.encode()).hexdigest()
    csrf_token = new(session_secret.encode(), session_token.encode(), "sha256").hexdigest()
    now = datetime.now(UTC)

    with psycopg.connect(_database_url()) as connection:
        with connection.cursor() as cursor:
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

    print("Local development identity created.\n")
    print(f"Workspace ID: {workspace_id}")
    print(f"User ID:      {user_id}")
    print("\nOpen http://localhost:5173 and run these in the browser console:\n")
    print(
        f'document.cookie = "ecc_session={session_token}; Path=/; SameSite=Lax";'
    )
    print(f'document.cookie = "ecc_csrf={csrf_token}; Path=/; SameSite=Lax";')
    print("\nReload the page after setting both cookies.")


if __name__ == "__main__":
    main()
