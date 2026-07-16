from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from hmac import new
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from ecc.config import get_settings
from ecc.database import get_session

router = APIRouter(prefix="/dev/bootstrap", tags=["development"])
SessionDep = Annotated[Session, Depends(get_session)]
_SESSION_MAX_AGE_SECONDS = 7 * 24 * 60 * 60


class BootstrapExchange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=32, max_length=256)


def _require_development() -> None:
    if get_settings().environment.casefold() != "development":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")


@router.get("", response_class=HTMLResponse, include_in_schema=False)
def bootstrap_page() -> HTMLResponse:
    _require_development()
    nonce = secrets.token_urlsafe(18)
    content = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Complete ECC local sign-in</title>
</head>
<body>
  <main>
    <h1>Completing local sign-in…</h1>
    <p id="status" role="status">Exchanging the one-time development code.</p>
  </main>
  <script nonce="{nonce}">
    const statusNode = document.getElementById('status');
    const code = new URLSearchParams(location.hash.slice(1)).get('code');
    history.replaceState(null, '', location.pathname);
    if (!code) {{
      statusNode.textContent = 'Missing bootstrap code. Run scripts/bootstrap_dev.py again.';
    }} else {{
      fetch('/dev/bootstrap/session', {{
        method: 'POST',
        credentials: 'same-origin',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{code}}),
      }}).then((response) => {{
        if (!response.ok) throw new Error('The bootstrap code is invalid or expired.');
        location.replace('http://localhost:5173');
      }}).catch((error) => {{
        statusNode.textContent = error.message;
      }});
    }}
  </script>
</body>
</html>"""
    return HTMLResponse(
        content,
        headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": (
                "default-src 'none'; connect-src 'self'; "
                f"script-src 'nonce-{nonce}'; style-src 'none'; base-uri 'none'; "
                "frame-ancestors 'none'"
            ),
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/session", include_in_schema=False)
def exchange_bootstrap_code(
    payload: BootstrapExchange,
    session: SessionDep,
) -> JSONResponse:
    _require_development()
    now = datetime.now(UTC)
    bootstrap_hash = sha256(payload.code.encode()).hexdigest()
    session_token = secrets.token_urlsafe(32)
    session_hash = sha256(session_token.encode()).hexdigest()
    expires_at = now + timedelta(seconds=_SESSION_MAX_AGE_SECONDS)

    with session.begin():
        row = (
            session.execute(
                text(
                    """
                    SELECT id
                    FROM sessions
                    WHERE token_hash = :bootstrap_hash
                      AND revoked_at IS NULL
                      AND expires_at > :now
                    FOR UPDATE
                    """
                ),
                {"bootstrap_hash": bootstrap_hash, "now": now},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="BOOTSTRAP_CODE_INVALID_OR_EXPIRED",
            )
        session.execute(
            text(
                """
                UPDATE sessions
                SET token_hash = :session_hash,
                    expires_at = :expires_at,
                    last_seen_at = :now
                WHERE id = :session_id
                """
            ),
            {
                "session_hash": session_hash,
                "expires_at": expires_at,
                "now": now,
                "session_id": row["id"],
            },
        )

    csrf_token = new(
        get_settings().session_secret.encode(),
        session_token.encode(),
        "sha256",
    ).hexdigest()
    response = JSONResponse({"status": "authenticated"})
    response.set_cookie(
        "ecc_session",
        session_token,
        max_age=_SESSION_MAX_AGE_SECONDS,
        httponly=True,
        secure=False,
        samesite="lax",
        path="/",
    )
    response.set_cookie(
        "ecc_csrf",
        csrf_token,
        max_age=_SESSION_MAX_AGE_SECONDS,
        httponly=False,
        secure=False,
        samesite="lax",
        path="/",
    )
    return response
