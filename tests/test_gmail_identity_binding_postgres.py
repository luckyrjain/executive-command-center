"""Spec A S1.10 (adapter reject hook) and S1.1 (identity binding) on the
Gmail OAuth callback (plan task T08).

S1.10 (unflagged): a callback that the adapter REJECTS after Google already
minted a grant (a scope unticked, the allowlist, a profile error...) used to
revoke that grant unconditionally. Google revocation may be grant-wide, so
anyone able to consent as a Google account that another member has
connected could end that member's live grant (deep review W2-1). The router
now passes a `revoke_on_reject` hook: the minted grant is revoked only when
`revoke_is_safe(minted_unpersisted)` allows it. Under the default `global`
scope that means "never while a live row uses that account, never when the
account is unknown".

S1.1 (`ECC_GMAIL_REQUIRE_IDENTITY_MATCH`, default off): the Google account
must be the caller's own ECC account email (normalized), else
`403 GMAIL_ACCOUNT_IDENTITY_MISMATCH` + a `denied` refusal audit + metric,
nothing written, the minted grant revoked iff safe, and neither email
anywhere in the response, the logs, the audit or the redirect.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any
from urllib.parse import parse_qs
from uuid import UUID, uuid4

import gmail_sync_fixtures
import httpx
import pytest
from fastapi.testclient import TestClient
from gmail_sync_fixtures import build_gmail_sync_world, cleanup_workspace, csrf_headers
from identity_fixtures import create_identity
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

import ecc.domains.personal.gmail_adapter as gmail_adapter_module
import ecc.domains.personal.gmail_oauth as gmail_oauth_module
from ecc import observability
from ecc.config import get_settings
from ecc.database import engine
from ecc.domains.personal.gmail_adapter import REQUIRED_SCOPES, GmailAdapter
from ecc.logging import JsonFormatter
from ecc.main import app

_SCOPE_STRING = " ".join(sorted(REQUIRED_SCOPES))
_REFUSED_EVENT = "connector_account.enrollment_refused"
_MISMATCH = "GMAIL_ACCOUNT_IDENTITY_MISMATCH"


# --- the deep-review probe, as a regression test (W2-1) ---------------------


def _connector_status(connector_account_id: UUID) -> str:
    with engine.connect() as connection:
        return str(
            connection.execute(
                text("SELECT status FROM connector_accounts WHERE id = :id"),
                {"id": connector_account_id},
            ).scalar_one()
        )


@pytest.mark.parametrize("identity_match", ["false", "true"])
def test_probe_rejected_consent_for_a_live_google_account_does_not_revoke_its_grant(
    identity_match: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deep review W2-1 probe, inverted. A's Gmail row for Google account X
    is live (real sync world). B completes consent for X with a scope
    unticked, so the adapter rejects the callback after the token exchange.
    Before T08 this revoked A's refresh token (the fake mints the same
    grant) in every scope/flag combination. Under the default `global`
    scope it must not."""
    env = {
        "ECC_PERSONAL_DATA_ISOLATION": "true",
        "ECC_GMAIL_REVOKE_SCOPE": "global",
        "ECC_GMAIL_REQUIRE_IDENTITY_MATCH": identity_match,
    }
    with build_gmail_sync_world(env=env, bystander=True) as world:
        world.fake_google.revoked_tokens.clear()
        monkeypatch.setattr(
            gmail_sync_fixtures, "_SCOPE_STRING", "https://www.googleapis.com/auth/userinfo.email"
        )
        client, token = world.harness.client_for(world.workspace_id, world.b.user_id)
        start = client.post("/api/v1/personal/gmail/oauth/start", headers=csrf_headers(token))
        assert start.status_code == 200, start.text
        state = httpx.URL(start.json()["authorization_url"]).params["state"]

        response = client.get(
            "/api/v1/personal/gmail/oauth/callback", params={"code": "code-a", "state": state}
        )

        assert response.status_code == 422, response.text
        assert world.fake_google.revoked_tokens == []
        assert _connector_status(world.a.connector_account_id) == "active"


# --- lightweight two-member world -------------------------------------------


@dataclass
class _FakeGoogle:
    """Token endpoint minting a DISTINCT token pair per authorization code
    (`code-<k>` -> `access-<k>`/`refresh-<k>`) and a profile endpoint
    resolving each access token to its Google account. Per-key knobs: an
    unticked scope (`partial_scope`), a failing profile lookup
    (`profile_fails`)."""

    emails_by_key: dict[str, str]
    revoked_tokens: list[str] = field(default_factory=list)
    partial_scope: set[str] = field(default_factory=set)
    profile_fails: set[str] = field(default_factory=set)

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/token":
                form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
                key = form.get("code", "").removeprefix("code-")
                scope = (
                    "https://www.googleapis.com/auth/userinfo.email"
                    if key in self.partial_scope
                    else _SCOPE_STRING
                )
                return httpx.Response(
                    200,
                    json={
                        "access_token": f"access-{key}",
                        "refresh_token": f"refresh-{key}",
                        "expires_in": 3600,
                        "scope": scope,
                    },
                )
            if request.url.path == "/gmail/v1/users/me/profile":
                key = request.headers["authorization"].removeprefix("Bearer access-")
                if key in self.profile_fails:
                    return httpx.Response(503)
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
    a: _Member  # owner; the Google account `a_google` is their own ECC email
    b: _Member  # member
    unconnected_google: str  # allowlisted, connected by nobody
    google: _FakeGoogle
    allowlist: list[str]

    @property
    def a_google(self) -> str:
        return self.a.email


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
                "VALUES (:id, 'Gmail Identity Binding Test', 'UTC', :now)"
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

    a_email, b_email = members["a"][1], members["b"][1]
    unconnected_google = f"unconnected-{suffix}@example.test"
    google = _FakeGoogle(
        emails_by_key={
            "a1": a_email,  # A connects their own account
            "b-for-a": a_email,  # B consents as A's Google account
            "b-own": b_email,
            "b-own-upper": f"  {b_email.upper()} ",
            "b-unconnected": unconnected_google,
        }
    )
    allowlist = [a_email, b_email, unconnected_google]
    monkeypatch.setenv("ECC_GMAIL_OAUTH_ALLOWLIST", ",".join(allowlist))
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("ECC_GMAIL_OAUTH_REDIRECT_URI", "https://ecc.example.test/callback")
    monkeypatch.setenv("ECC_FRONTEND_URL", "https://app.example.test")
    monkeypatch.setenv("ECC_GMAIL_REVOKE_SCOPE", "global")
    monkeypatch.setenv("ECC_GMAIL_REQUIRE_IDENTITY_MATCH", "false")
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
            unconnected_google=unconnected_google,
            google=google,
            allowlist=allowlist,
        )
    finally:
        for client in clients:
            client.close()
        cleanup_workspace(workspace_id, account_ids)
        get_settings.cache_clear()


def _set_env(monkeypatch: pytest.MonkeyPatch, name: str, value: str) -> None:
    monkeypatch.setenv(name, value)
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


def _revoke_count(site: str, result: str) -> float:
    return observability.connector_revoke_total._values.get(("gmail", site, result), 0.0)


def _refused_count(reason: str) -> float:
    return observability.connector_enrollment_refused_total._values.get(("gmail", reason), 0.0)


def _connect_a(world: _World) -> dict[str, Any]:
    response = _consent(world.a, "code-a1")
    assert response.status_code == 200, response.text
    rows = _connector_rows(world.workspace_id)
    assert len(rows) == 1
    assert rows[0]["status"] == "active"
    return rows[0]


def _rendered_app_logs(caplog: pytest.LogCaptureFixture) -> str:
    records = [r for r in caplog.records if not r.name.startswith("httpx")]
    return " ".join(JsonFormatter().format(r) for r in records)


# --- S1.10: the adapter reject hook (unflagged) -----------------------------


_REJECTIONS = ("scope_unticked", "profile_error", "allowlist")


def _arrange_rejection(world: _World, monkeypatch: pytest.MonkeyPatch, rejection: str) -> None:
    """Make B's consent as A's Google account (`code-b-for-a`) be rejected
    by the adapter after the token exchange. Only the allowlist rejection
    happens after the Google email is known."""
    if rejection == "scope_unticked":
        world.google.partial_scope.add("b-for-a")
    elif rejection == "profile_error":
        world.google.profile_fails.add("b-for-a")
    else:
        # A is still connected; the account merely left the allowlist. B's
        # own ECC email stays allowlisted so B can start the flow.
        allowlist = [e for e in world.allowlist if e != world.a_google]
        _set_env(monkeypatch, "ECC_GMAIL_OAUTH_ALLOWLIST", ",".join(allowlist))


@pytest.mark.parametrize("rejection", _REJECTIONS)
def test_rejected_callback_never_revokes_a_live_grant_under_global_scope(
    world: _World, monkeypatch: pytest.MonkeyPatch, rejection: str
) -> None:
    a_row = _connect_a(world)
    _arrange_rejection(world, monkeypatch, rejection)
    skipped_before = _revoke_count("adapter_callback", "skipped_unsafe")
    ok_before = _revoke_count("adapter_callback", "ok")

    response = _consent(world.b, "code-b-for-a")

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "GMAIL_OAUTH_FAILED"
    assert world.a_google not in response.text
    # Neither A's grant nor the minted one: the minted token may BE A's
    # grant (grant-wide revocation), and A's row is live -- or the account
    # is not even known yet.
    assert world.google.revoked_tokens == []
    assert _revoke_count("adapter_callback", "skipped_unsafe") == skipped_before + 1
    assert _revoke_count("adapter_callback", "ok") == ok_before
    assert _connector_rows(world.workspace_id) == [a_row]


@pytest.mark.parametrize("rejection", _REJECTIONS)
def test_rejected_callback_under_none_scope_revokes_only_the_minted_token(
    world: _World, monkeypatch: pytest.MonkeyPatch, rejection: str
) -> None:
    """`none` = D2 proved revocation per-token: the rejected request's own
    grant is revoked (else it would be orphaned), A's is untouched."""
    a_row = _connect_a(world)
    _set_env(monkeypatch, "ECC_GMAIL_REVOKE_SCOPE", "none")
    _arrange_rejection(world, monkeypatch, rejection)
    ok_before = _revoke_count("adapter_callback", "ok")

    response = _consent(world.b, "code-b-for-a")

    assert response.status_code == 422, response.text
    assert world.google.revoked_tokens == ["refresh-b-for-a"]
    assert _revoke_count("adapter_callback", "ok") == ok_before + 1
    assert _connector_rows(world.workspace_id) == [a_row]


def test_rejected_callback_for_an_unconnected_known_account_is_revoked_under_global(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No live row anywhere uses the account and its email is known (an
    allowlist rejection): revoking the orphaned minted grant is safe."""
    allowlist = [e for e in world.allowlist if e != world.unconnected_google]
    _set_env(monkeypatch, "ECC_GMAIL_OAUTH_ALLOWLIST", ",".join(allowlist))
    ok_before = _revoke_count("adapter_callback", "ok")

    response = _consent(world.b, "code-b-unconnected")

    assert response.status_code == 422, response.text
    assert world.google.revoked_tokens == ["refresh-b-unconnected"]
    assert _revoke_count("adapter_callback", "ok") == ok_before + 1
    assert _connector_rows(world.workspace_id) == []


def test_rejected_callback_with_unknown_account_is_not_revoked_under_global(
    world: _World,
) -> None:
    """Email unknown (profile lookup failed) -> unsafe under `global`, even
    when no row exists at all (Spec A "Revocation safety", Arch N5)."""
    world.google.profile_fails.add("b-unconnected")
    skipped_before = _revoke_count("adapter_callback", "skipped_unsafe")

    response = _consent(world.b, "code-b-unconnected")

    assert response.status_code == 422, response.text
    assert world.google.revoked_tokens == []
    assert _revoke_count("adapter_callback", "skipped_unsafe") == skipped_before + 1


def test_reject_hook_fails_closed_when_the_safety_check_cannot_run(
    world: _World, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The hook's own DB session fails: no revoke (fail closed), counted as
    `skipped_unsafe`, the exception class logged in the message text (no
    email), and the original 422 rejection is what the caller gets."""
    allowlist = [e for e in world.allowlist if e != world.unconnected_google]
    _set_env(monkeypatch, "ECC_GMAIL_OAUTH_ALLOWLIST", ",".join(allowlist))

    def _broken_session_factory() -> Any:
        raise OperationalError("SELECT 1", {}, Exception(f"db down for {world.unconnected_google}"))

    monkeypatch.setattr(gmail_oauth_module, "SessionFactory", _broken_session_factory)
    skipped_before = _revoke_count("adapter_callback", "skipped_unsafe")
    error_before = _revoke_count("adapter_callback", "error")

    with caplog.at_level(logging.DEBUG):
        response = _consent(world.b, "code-b-unconnected")

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "GMAIL_OAUTH_FAILED"
    assert world.google.revoked_tokens == []
    assert _revoke_count("adapter_callback", "skipped_unsafe") == skipped_before + 1
    assert _revoke_count("adapter_callback", "error") == error_before
    rendered = _rendered_app_logs(caplog)
    assert "gmail_revoke_on_reject_check_failed: error_class=OperationalError" in rendered
    assert world.unconnected_google not in rendered


def test_revoke_metric_failure_never_replaces_the_rejection(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The adapter's `adapter_callback` revoke metric raising must not turn
    the original rejection (422) into a 500: the revoke still happened and
    the `AdapterAuthorizationError` is what propagates."""
    allowlist = [e for e in world.allowlist if e != world.unconnected_google]
    _set_env(monkeypatch, "ECC_GMAIL_OAUTH_ALLOWLIST", ",".join(allowlist))

    def _broken_metric(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("metrics backend down")

    monkeypatch.setattr(gmail_adapter_module, "record_connector_revoke", _broken_metric)

    response = _consent(world.b, "code-b-unconnected")

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "GMAIL_OAUTH_FAILED"
    assert world.google.revoked_tokens == ["refresh-b-unconnected"]


@dataclass
class _OtherWorkspace:
    workspace_id: UUID
    member: _Member
    account_id: UUID


@pytest.fixture
def other_workspace(world: _World, monkeypatch: pytest.MonkeyPatch) -> Iterator[_OtherWorkspace]:
    """A second workspace W2 with one member C (allowlisted to start the
    flow), who can consent as A's Google account (`code-c-for-a`)."""
    workspace_id = uuid4()
    user_id = uuid4()
    now = datetime.now(UTC)
    email = f"member-c-{uuid4().hex[:8]}@example.test"
    token = f"session-{uuid4()}"
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, 'Gmail Identity Binding W2', 'UTC', :now)"
            ),
            {"id": workspace_id, "now": now},
        )
        account_id = create_identity(
            connection,
            workspace_id=workspace_id,
            user_id=user_id,
            email=email,
            now=now,
            role="owner",
        )
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
    world.google.emails_by_key["c-for-a"] = world.a_google
    world.allowlist.append(email)
    _set_env(monkeypatch, "ECC_GMAIL_OAUTH_ALLOWLIST", ",".join(world.allowlist))
    client = TestClient(app)
    client.cookies.set("ecc_session", token)
    try:
        yield _OtherWorkspace(
            workspace_id=workspace_id,
            member=_Member(user_id=user_id, email=email, client=client, token=token),
            account_id=account_id,
        )
    finally:
        client.close()
        cleanup_workspace(workspace_id, [account_id])


@pytest.mark.parametrize("scope", ["global", "none"])
def test_rejected_callback_in_another_workspace_respects_the_live_row(
    world: _World,
    other_workspace: _OtherWorkspace,
    monkeypatch: pytest.MonkeyPatch,
    scope: str,
) -> None:
    """The live-row check is global: A's live row in W1 protects A's grant
    from a REJECTED callback for the same Google account completed by C in
    W2. Under `none` only C's minted token is revoked."""
    a_row = _connect_a(world)
    _set_env(monkeypatch, "ECC_GMAIL_REVOKE_SCOPE", scope)
    world.google.partial_scope.add("c-for-a")

    response = _consent(other_workspace.member, "code-c-for-a")

    assert response.status_code == 422, response.text
    if scope == "global":
        assert world.google.revoked_tokens == []
    else:
        assert world.google.revoked_tokens == ["refresh-c-for-a"]
    assert "refresh-a1" not in world.google.revoked_tokens
    assert _connector_rows(world.workspace_id) == [a_row]
    assert _connector_rows(other_workspace.workspace_id) == []


# --- S1.1: identity binding (ECC_GMAIL_REQUIRE_IDENTITY_MATCH) --------------


def _assert_mismatch_refusal(
    world: _World, response: httpx.Response, rows_before: list[dict[str, Any]]
) -> None:
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == _MISMATCH
    for leaked in (world.a.email, world.b.email, world.unconnected_google, "@"):
        assert leaked not in response.text
    assert _connector_rows(world.workspace_id) == rows_before

    audits = _audit_rows(world.workspace_id, _REFUSED_EVENT)
    assert len(audits) == 1
    audit = audits[0]
    assert audit["authorization_result"] == "denied"
    assert audit["failure_code"] == "identity_mismatch"
    assert audit["actor_id"] == world.b.user_id
    assert audit["aggregate_type"] == "connector_account_enrollment"
    expected_payload = {"reason": "identity_mismatch", "provider": "gmail"}
    assert audit["metadata"] == expected_payload
    payloads = _outbox_payloads(world.workspace_id, _REFUSED_EVENT)
    assert payloads == [expected_payload]
    assert "@" not in json.dumps([audit["metadata"], *payloads])


def test_identity_mismatch_is_refused_without_revoking_a_live_grant(
    world: _World, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """B authorizes A's Google account: 403, audit + metric, nothing
    written; under `global` A's live row makes the minted grant unsafe to
    revoke. No email in the body, the logs, the audit or the outbox."""
    a_row = _connect_a(world)
    created_before = _audit_rows(world.workspace_id, "connector_account.created")
    _set_env(monkeypatch, "ECC_GMAIL_REQUIRE_IDENTITY_MATCH", "true")
    refused_before = _refused_count("identity_mismatch")
    skipped_before = _revoke_count("callback_failure", "skipped_unsafe")

    with caplog.at_level(logging.DEBUG):
        response = _consent(world.b, "code-b-for-a")

    _assert_mismatch_refusal(world, response, [a_row])
    assert _audit_rows(world.workspace_id, "connector_account.created") == created_before
    assert world.google.revoked_tokens == []
    assert _refused_count("identity_mismatch") == refused_before + 1
    assert _revoke_count("callback_failure", "skipped_unsafe") == skipped_before + 1
    rendered = _rendered_app_logs(caplog)
    assert "gmail_oauth_callback_refused: reason=identity_mismatch" in rendered
    for secret in (world.a.email, world.b.email, "refresh-a1", "refresh-b-for-a"):
        assert secret not in rendered


def test_identity_mismatch_under_none_scope_revokes_only_the_minted_token(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    a_row = _connect_a(world)
    _set_env(monkeypatch, "ECC_GMAIL_REQUIRE_IDENTITY_MATCH", "true")
    _set_env(monkeypatch, "ECC_GMAIL_REVOKE_SCOPE", "none")

    response = _consent(world.b, "code-b-for-a")

    _assert_mismatch_refusal(world, response, [a_row])
    assert world.google.revoked_tokens == ["refresh-b-for-a"]


def test_identity_mismatch_for_an_unconnected_account_revokes_the_minted_grant(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No live row uses the account: the orphaned minted grant is safe to
    revoke even under `global`."""
    _set_env(monkeypatch, "ECC_GMAIL_REQUIRE_IDENTITY_MATCH", "true")

    response = _consent(world.b, "code-b-unconnected")

    _assert_mismatch_refusal(world, response, [])
    assert world.google.revoked_tokens == ["refresh-b-unconnected"]


def test_identity_mismatch_redirect_carries_only_the_code(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_env(monkeypatch, "ECC_GMAIL_REQUIRE_IDENTITY_MATCH", "true")

    response = _consent(world.b, "code-b-unconnected", path="complete")

    assert response.status_code == 302
    location = response.headers["location"]
    assert httpx.URL(location).params["code"] == _MISMATCH
    assert httpx.URL(location).params["gmail"] == "error"
    for leaked in (world.b.email, world.unconnected_google, "@", "%40"):
        assert leaked not in location
    assert _connector_rows(world.workspace_id) == []


def test_missing_caller_email_is_refused_as_a_mismatch(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail closed: no caller email -> mismatch, even for B's own account."""
    _set_env(monkeypatch, "ECC_GMAIL_REQUIRE_IDENTITY_MATCH", "true")
    real_caller_email = gmail_oauth_module._caller_email
    calls: list[str] = []

    def _no_email_on_callback(session: Any, auth: Any) -> str | None:
        # `/oauth/start` reads the same helper for its allowlist check;
        # only the callback's read is made to come back empty.
        calls.append("x")
        return real_caller_email(session, auth) if len(calls) == 1 else None

    monkeypatch.setattr(gmail_oauth_module, "_caller_email", _no_email_on_callback)

    response = _consent(world.b, "code-b-own")

    _assert_mismatch_refusal(world, response, [])
    assert len(calls) == 2


def test_identity_match_connects_normally(world: _World, monkeypatch: pytest.MonkeyPatch) -> None:
    """Match is on normalized emails (case, surrounding whitespace)."""
    _set_env(monkeypatch, "ECC_GMAIL_REQUIRE_IDENTITY_MATCH", "true")
    refused_before = _refused_count("identity_mismatch")

    response = _consent(world.b, "code-b-own-upper")

    assert response.status_code == 200, response.text
    rows = _connector_rows(world.workspace_id)
    assert len(rows) == 1
    assert rows[0]["owner_id"] == world.b.user_id
    assert _audit_rows(world.workspace_id, _REFUSED_EVENT) == []
    assert _refused_count("identity_mismatch") == refused_before
    assert world.google.revoked_tokens == []


def test_flag_off_keeps_accepting_another_google_account(world: _World) -> None:
    """Default (flag off): today's behavior -- B may connect a Google
    account that is not their ECC email."""
    refused_before = _refused_count("identity_mismatch")

    response = _consent(world.b, "code-b-unconnected")

    assert response.status_code == 200, response.text
    rows = _connector_rows(world.workspace_id)
    assert len(rows) == 1
    assert rows[0]["external_account_id"] == world.unconnected_google
    assert rows[0]["owner_id"] == world.b.user_id
    assert _audit_rows(world.workspace_id, _REFUSED_EVENT) == []
    assert _refused_count("identity_mismatch") == refused_before


def test_flag_off_owner_conflict_is_unchanged(world: _World) -> None:
    """With the flag off, B consenting as A's account still reaches the
    T07 owner check (409), not the identity check."""
    a_row = _connect_a(world)

    response = _consent(world.b, "code-b-for-a")

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "CONNECTOR_OWNED_BY_ANOTHER_MEMBER"
    assert _connector_rows(world.workspace_id) == [a_row]
    assert world.google.revoked_tokens == []
