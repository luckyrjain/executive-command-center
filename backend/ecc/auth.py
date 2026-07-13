from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from hmac import compare_digest, new
from typing import Annotated
from uuid import UUID

from fastapi import Cookie, Depends, Header, HTTPException, status
from sqlalchemy import text
from sqlalchemy.orm import Session

from ecc.config import get_settings
from ecc.database import get_session

SessionDep = Annotated[Session, Depends(get_session)]
SessionCookie = Annotated[str | None, Cookie(alias="ecc_session")]
CsrfHeader = Annotated[str | None, Header(alias="X-CSRF-Token")]


@dataclass(frozen=True)
class AuthContext:
    workspace_id: UUID
    user_id: UUID


def require_auth_context(
    session: SessionDep,
    ecc_session: SessionCookie = None,
) -> AuthContext:
    if not ecc_session:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
        )

    token_hash = sha256(ecc_session.encode("utf-8")).hexdigest()
    row = (
        session.execute(
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
        )
        .mappings()
        .one_or_none()
    )
    session.rollback()

    if row is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid session",
        )

    return AuthContext(workspace_id=row["workspace_id"], user_id=row["user_id"])


def require_csrf(
    ecc_session: SessionCookie = None,
    csrf_token: CsrfHeader = None,
) -> None:
    if not ecc_session or not csrf_token:
        raise HTTPException(status_code=403, detail="CSRF_TOKEN_REQUIRED")

    settings = get_settings()
    expected = new(
        settings.session_secret.encode("utf-8"),
        ecc_session.encode("utf-8"),
        "sha256",
    ).hexdigest()
    if not compare_digest(csrf_token, expected):
        raise HTTPException(status_code=403, detail="CSRF_TOKEN_INVALID")


AuthDep = Annotated[AuthContext, Depends(require_auth_context)]
CsrfDep = Annotated[None, Depends(require_csrf)]
