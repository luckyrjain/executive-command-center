from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from uuid import UUID

from fastapi import Cookie, Depends, HTTPException, status
from sqlalchemy import text
from sqlalchemy.orm import Session

from ecc.database import get_session


@dataclass(frozen=True)
class AuthContext:
    workspace_id: UUID
    user_id: UUID


def require_auth_context(
    ecc_session: str | None = Cookie(default=None),
    session: Session = Depends(get_session),
) -> AuthContext:
    if not ecc_session:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")

    token_hash = sha256(ecc_session.encode("utf-8")).hexdigest()
    row = session.execute(
        text(
            """
            SELECT workspace_id, user_id
            FROM sessions
            WHERE token_hash = :token_hash
              AND revoked_at IS NULL
              AND expires_at > :now
            """
        ),
        {"token_hash": token_hash, "now": datetime.now(UTC)},
    ).mappings().one_or_none()

    if row is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid session")

    return AuthContext(workspace_id=row["workspace_id"], user_id=row["user_id"])
