"""Spec A S1.2 (T07): owner check on the Gmail OAuth callback's conflict path.

When the Google account a caller just authorized is already connected in the
same workspace by a DIFFERENT member, the callback refuses with
`409 CONNECTOR_OWNED_BY_ANOTHER_MEMBER` -- never returning, reactivating or
overwriting the other member's row, and never exposing its data. The
business transaction rolls back; a `denied` refusal audit is written in its
own transaction afterwards; the freshly minted grant is revoked only when
`revoke_is_safe(minted_unpersisted)` allows it (never under the default
`global` scope while the owner's row is live, so the owner's grant is never
revoked). A same-owner conflict keeps the pre-existing behaviour.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any
from urllib.parse import parse_qs
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from gmail_sync_fixtures import cleanup_workspace, csrf_headers
from identity_fixtures import create_identity
from sqlalchemy import text

import ecc.domains.personal.gmail_oauth as gmail_oauth_module
from ecc import observability
from ecc.config import get_settings
from ecc.database import engine
from ecc.domains.engineering.connectors import ConnectorAccountContext
from ecc.domains.engineering.crypto import decrypt_credential
from ecc.domains.personal.gmail_adapter import REQUIRED_SCOPES, GmailAdapter
from ecc.logging import JsonFormatter
from ecc.main import app

_SCOPE_STRING = " ".join(sorted(REQUIRED_SCOPES))
_REFUSED_EVENT = "connector_account.enrollment_refused"


@dataclass
class _FakeGoogle:
    """Token endpoint minting a DISTINCT token pair per authorization code
    (`code-<k>` -> `access-<k>`/`refresh-<k>`), a profile endpoint resolving
    each access token to its Google account, and a `/revoke` recorder."""

    emails_by_key: dict[str, str]
    revoked_tokens: list[str]

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/token":
                form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
                key = form.get("code", "").removeprefix("code-")
                return httpx.Response(
                    200,
                    json={
                        "access_token": f"access-{key}",
                        "refresh_token": f"refresh-{key}",
                        "expires_in": 3600,
                        "scope": _SCOPE_STRING,
                    },
                )
            if request.url.path == "/gmail/v1/users/me/profile":
                key = request.headers["authorization"].removeprefix("Bearer access-")
                return httpx.Response(200, json={"emailAddress": self.emails_by_key[key]})
            if request.url.path == "/revoke":
                form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
                self.revoked_tokens.append(form.get("token", ""))
                return httpx.Response(200)
            raise AssertionError(f"unexpected request to {request.url}")

        return httpx.MockTransport(handler)


@dataclass
class _Member:
    user_id: UUID
    email: str
    client: TestClient
    token: str


@dataclass
class _World:
    workspace_id: UUID
    a: _Member  # workspace owner, connects the shared Google account first
    b: _Member  # a second member
    shared_google_email: str
    other_google_email: str
    google: _FakeGoogle


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> Iterator[_World]:
    workspace_id = uuid4()
    now = datetime.now(UTC)
    suffix = uuid4().hex[:8]
    members: dict[str, tuple[UUID, str, str]] = {}
    account_ids: list[UUID] = []
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, 'Gmail Owner Conflict Test', 'UTC', :now)"
            ),
            {"id": workspace_id, "now": now},
        )
        for key, role, offset in (("a", "owner", 0), ("b", "member", 1)):
            user_id = uuid4()
            email = f"member-{key}-{suffix}@example.test"
            account_ids.append(
                create_identity(
                    connection,
                    workspace_id=workspace_id,
                    user_id=user_id,
                    email=email,
                    now=now + timedelta(seconds=offset),
                    role=role,
                )
            )
            token = f"session-{uuid4()}"
            connection.execute(
                text(
                    "INSERT INTO sessions (id, workspace_id, user_id, token_hash, "
                    "expires_at, last_seen_at) "
                    "VALUES (:id, :workspace_id, :user_id, :token_hash, :expires_at, :now)"
                ),
                {
                    "id": uuid4(),
                    "workspace_id": workspace_id,
                    "user_id": user_id,
                    "token_hash": sha256(token.encode()).hexdigest(),
                    "expires_at": now + timedelta(hours=1),
                    "now": now,
                },
            )
            members[key] = (user_id, email, token)

    shared_google_email = f"shared-google-{suffix}@example.test"
    other_google_email = f"other-google-{suffix}@example.test"
    google = _FakeGoogle(
        emails_by_key={
            "a1": shared_google_email,
            "a2": shared_google_email,
            "b1": shared_google_email,
            "b2": other_google_email,
        },
        revoked_tokens=[],
    )
    monkeypatch.setenv(
        "ECC_GMAIL_OAUTH_ALLOWLIST",
        ",".join([members["a"][1], members["b"][1], shared_google_email, other_google_email]),
    )
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("ECC_GMAIL_OAUTH_REDIRECT_URI", "https://ecc.example.test/callback")
    monkeypatch.setenv("ECC_GMAIL_REVOKE_SCOPE", "global")
    get_settings.cache_clear()
    monkeypatch.setattr(gmail_oauth_module, "_adapter", GmailAdapter(transport=google.transport()))

    clients: list[TestClient] = []

    def member(key: str) -> _Member:
        user_id, email, token = members[key]
        client = TestClient(app)
        client.cookies.set("ecc_session", token)
        clients.append(client)
        return _Member(user_id=user_id, email=email, client=client, token=token)

    try:
        yield _World(
            workspace_id=workspace_id,
            a=member("a"),
            b=member("b"),
            shared_google_email=shared_google_email,
            other_google_email=other_google_email,
            google=google,
        )
    finally:
        for client in clients:
            client.close()
        cleanup_workspace(workspace_id, account_ids)
        get_settings.cache_clear()


def _set_revoke_scope(monkeypatch: pytest.MonkeyPatch, scope: str) -> None:
    monkeypatch.setenv("ECC_GMAIL_REVOKE_SCOPE", scope)
    get_settings.cache_clear()


def _consent(member: _Member, code: str, *, path: str = "callback") -> httpx.Response:
    start = member.client.post(
        "/api/v1/personal/gmail/oauth/start", headers=csrf_headers(member.token)
    )
    assert start.status_code == 200, start.text
    state = httpx.URL(start.json()["authorization_url"]).params["state"]
    return member.client.get(
        f"/api/v1/personal/gmail/oauth/{path}",
        params={"code": code, "state": state},
        follow_redirects=False,
    )


def _connector_rows(workspace_id: UUID) -> list[dict[str, Any]]:
    with engine.connect() as connection:
        return [
            dict(row)
            for row in connection.execute(
                text(
                    "SELECT * FROM connector_accounts WHERE workspace_id = :ws ORDER BY created_at"
                ),
                {"ws": workspace_id},
            ).mappings()
        ]


def _audit_rows(workspace_id: UUID, event_type: str) -> list[dict[str, Any]]:
    with engine.connect() as connection:
        return [
            dict(row)
            for row in connection.execute(
                text(
                    "SELECT aggregate_type, aggregate_id, authorization_result, failure_code, "
                    "metadata, actor_id FROM audit_events "
                    "WHERE workspace_id = :ws AND event_type = :event_type"
                ),
                {"ws": workspace_id, "event_type": event_type},
            ).mappings()
        ]


def _outbox_payloads(workspace_id: UUID, event_type: str) -> list[Any]:
    with engine.connect() as connection:
        return [
            row[0]
            for row in connection.execute(
                text(
                    "SELECT payload FROM event_outbox "
                    "WHERE workspace_id = :ws AND event_type = :event_type"
                ),
                {"ws": workspace_id, "event_type": f"{event_type}.v1"},
            )
        ]


def _refused_count() -> float:
    return observability.connector_enrollment_refused_total._values.get(
        ("gmail", "owned_by_another_member"), 0.0
    )


def _revoke_count(site: str, result: str) -> float:
    return observability.connector_revoke_total._values.get(("gmail", site, result), 0.0)


def _connect_owner(world: _World) -> dict[str, Any]:
    response = _consent(world.a, "code-a1")
    assert response.status_code == 200, response.text
    rows = _connector_rows(world.workspace_id)
    assert len(rows) == 1
    assert rows[0]["owner_id"] == world.a.user_id
    return rows[0]


def _assert_refusal(world: _World, response: httpx.Response, owner_row: dict[str, Any]) -> None:
    """409 with the bare code and nothing about the owner's row; owner's row
    byte-for-byte unchanged; one `denied` refusal audit (no email) that
    survived the business transaction's rollback."""
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "CONNECTOR_OWNED_BY_ANOTHER_MEMBER"
    for leaked in (
        str(owner_row["id"]),
        str(world.a.user_id),
        world.a.email,
        world.shared_google_email,
        owner_row["display_name"] or "<no display name>",
        "@",
    ):
        assert leaked not in response.text

    assert _connector_rows(world.workspace_id) == [owner_row]

    audits = _audit_rows(world.workspace_id, _REFUSED_EVENT)
    assert len(audits) == 1
    audit = audits[0]
    assert audit["authorization_result"] == "denied"
    assert audit["failure_code"] == "owned_by_another_member"
    assert audit["actor_id"] == world.b.user_id
    assert audit["aggregate_type"] == "connector_account"
    assert audit["aggregate_id"] == owner_row["id"]
    expected_payload = {"reason": "owned_by_another_member", "provider": "gmail"}
    assert audit["metadata"] == expected_payload
    payloads = _outbox_payloads(world.workspace_id, _REFUSED_EVENT)
    assert payloads == [expected_payload]
    assert "@" not in json.dumps([audit["metadata"], *payloads])
    # Nothing from the refused request's own writes survived.
    assert _audit_rows(world.workspace_id, "connector_account.reconnected") == []


def test_foreign_owned_active_conflict_is_refused_without_revoking_under_global_scope(
    world: _World, caplog: pytest.LogCaptureFixture
) -> None:
    owner_row = _connect_owner(world)
    refused_before = _refused_count()
    skipped_before = _revoke_count("callback_failure", "skipped_unsafe")
    ok_before = _revoke_count("callback_failure", "ok")

    with caplog.at_level(logging.DEBUG):
        response = _consent(world.b, "code-b1")

    _assert_refusal(world, response, owner_row)
    # Neither the owner's grant nor the minted one is revoked: under the
    # (default) grant-wide `global` scope the minted token may BE the
    # owner's grant, and the owner's row is live.
    assert world.google.revoked_tokens == []
    assert _refused_count() == refused_before + 1
    assert _revoke_count("callback_failure", "skipped_unsafe") == skipped_before + 1
    assert _revoke_count("callback_failure", "ok") == ok_before
    assert "refresh-a1" in decrypt_credential(owner_row["encrypted_credentials"])

    app_records = [r for r in caplog.records if not r.name.startswith("httpx")]
    rendered = " ".join(JsonFormatter().format(r) for r in app_records)
    assert "gmail_oauth_callback_refused: reason=owned_by_another_member" in rendered
    for secret in (
        world.shared_google_email,
        world.a.email,
        world.b.email,
        str(owner_row["id"]),
        "refresh-a1",
        "refresh-b1",
    ):
        assert secret not in rendered


def test_foreign_owned_conflict_under_none_scope_revokes_only_the_minted_token_after_unlock(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`none` = D2 proved Google revocation per-token: `revoke_is_safe` is
    always true, so the refused request's own freshly minted token is
    revoked (it would otherwise be an orphaned, ECC-unrecorded grant), and
    only that token -- the owner's stored refresh token is untouched. The
    revoke runs after the business transaction rolled back: the owner's row
    is not locked at that point."""
    owner_row = _connect_owner(world)
    _set_revoke_scope(monkeypatch, "none")
    lock_free_during_revoke: list[bool] = []
    google = world.google

    class _LockProbingAdapter(GmailAdapter):
        def disconnect(self, account: ConnectorAccountContext) -> None:
            with engine.connect() as connection, connection.begin():
                connection.execute(text("SET LOCAL lock_timeout = '200ms'"))
                connection.execute(
                    text("SELECT id FROM connector_accounts WHERE id = :id FOR UPDATE NOWAIT"),
                    {"id": owner_row["id"]},
                )
                lock_free_during_revoke.append(True)
            super().disconnect(account)

    monkeypatch.setattr(
        gmail_oauth_module, "_adapter", _LockProbingAdapter(transport=google.transport())
    )
    ok_before = _revoke_count("callback_failure", "ok")

    response = _consent(world.b, "code-b1")

    _assert_refusal(world, response, owner_row)
    assert google.revoked_tokens == ["refresh-b1"]
    assert "refresh-a1" not in google.revoked_tokens
    assert lock_free_during_revoke == [True]
    assert _revoke_count("callback_failure", "ok") == ok_before + 1


def test_foreign_owned_disconnected_row_is_not_taken_over(world: _World) -> None:
    """A non-`active` row owned by another member is not reactivated with
    the caller's credential (the takeover case). No live row remains for
    that Google account, so even under `global` the minted grant is safe
    to revoke and is revoked -- the owner's stored credential is not."""
    _connect_owner(world)
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE connector_accounts SET status = 'disconnected', disconnected_at = :now "
                "WHERE workspace_id = :ws"
            ),
            {"ws": world.workspace_id, "now": datetime.now(UTC)},
        )
    owner_row = _connector_rows(world.workspace_id)[0]

    response = _consent(world.b, "code-b1")

    _assert_refusal(world, response, owner_row)
    assert owner_row["status"] == "disconnected"
    assert world.google.revoked_tokens == ["refresh-b1"]


def test_oauth_complete_redirects_with_the_owner_refusal_code(world: _World) -> None:
    owner_row = _connect_owner(world)

    response = _consent(world.b, "code-b1", path="complete")

    assert response.status_code == 302
    location = httpx.URL(response.headers["location"])
    assert location.params["gmail"] == "error"
    assert location.params["code"] == "CONNECTOR_OWNED_BY_ANOTHER_MEMBER"
    assert str(owner_row["id"]) not in response.headers["location"]
    assert _connector_rows(world.workspace_id) == [owner_row]


def test_same_owner_conflict_keeps_returning_the_existing_row(world: _World) -> None:
    owner_row = _connect_owner(world)
    refused_before = _refused_count()

    response = _consent(world.a, "code-a2")

    assert response.status_code == 200, response.text
    assert response.json()["id"] == str(owner_row["id"])
    assert _connector_rows(world.workspace_id) == [owner_row]
    assert _audit_rows(world.workspace_id, _REFUSED_EVENT) == []
    assert _refused_count() == refused_before
    # `global`: the duplicate grant is not revoked while the row is live
    # (T05 behaviour, unchanged).
    assert world.google.revoked_tokens == []


def test_same_owner_disconnected_row_is_still_reactivated(world: _World) -> None:
    owner_row = _connect_owner(world)
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE connector_accounts SET status = 'disconnected', disconnected_at = :now "
                "WHERE id = :id"
            ),
            {"id": owner_row["id"], "now": datetime.now(UTC)},
        )

    response = _consent(world.a, "code-a2")

    assert response.status_code == 200, response.text
    assert response.json()["id"] == str(owner_row["id"])
    assert response.json()["status"] == "active"
    rows = _connector_rows(world.workspace_id)
    assert len(rows) == 1
    assert "refresh-a2" in decrypt_credential(rows[0]["encrypted_credentials"])
    assert len(_audit_rows(world.workspace_id, "connector_account.reconnected")) == 1
    assert _audit_rows(world.workspace_id, _REFUSED_EVENT) == []


def test_non_conflicting_account_for_another_member_is_created(world: _World) -> None:
    owner_row = _connect_owner(world)

    response = _consent(world.b, "code-b2")

    assert response.status_code == 200, response.text
    rows = _connector_rows(world.workspace_id)
    assert len(rows) == 2
    assert rows[0] == owner_row
    assert rows[1]["owner_id"] == world.b.user_id
    assert rows[1]["external_account_id"] == world.other_google_email
    assert _audit_rows(world.workspace_id, _REFUSED_EVENT) == []
    assert world.google.revoked_tokens == []
