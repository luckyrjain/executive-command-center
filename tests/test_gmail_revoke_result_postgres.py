"""Gmail revoke outcomes are reported truthfully (Spec A fix wave FX6).

`GmailAdapter.disconnect` raises `GmailRevokeFailed` whenever Google's
grant may still be live, so `connector_security.revoke_guarded` counts
`ecc_connector_revoke_total{result="error"}` instead of `ok`:

- a transport error, a Google 5xx, or a Google 4xx other than
  `invalid_token`;
- a stored credential that cannot be unpacked;
- a missing or empty `refresh_token` (nothing is sent to Google).

Google's HTTP 400 `{"error": "invalid_token"}` (already revoked or
unknown token) and any 2xx are success: the grant is gone either way.

Covered at the adapter level, at `revoke_guarded`, and end to end through
the real-sync world (`gmail_sync_fixtures`) at three sites:

- `removal`: an admin removes member B (`DELETE .../members/{id}`);
- `cascade`: B disables the `email` domain (the purge path);
- `adapter_callback`: a rejected OAuth callback revokes its minted grant.

At every site the user-facing operation still succeeds (the revoke is
best-effort), only the metric and logs change, and no log line carries
the refresh token or the Google email.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from json import dumps
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from gmail_sync_fixtures import (
    FakeGmailMessage,
    FakeGoogle,
    FakeMailbox,
    GmailSyncWorld,
    build_gmail_sync_world,
    csrf_headers,
)
from sqlalchemy import text

import ecc.domains.personal.gmail_adapter as gmail_adapter_module
from ecc import observability
from ecc.config import get_settings
from ecc.database import engine
from ecc.domains.engineering.connectors import AdapterAuthorizationError, ConnectorAccountContext
from ecc.domains.engineering.crypto import encrypt_credential
from ecc.domains.personal.gmail_adapter import GmailAdapter, GmailRevokeFailed
from ecc.domains.personal.gmail_shared import pack_credential
from ecc.logging import JsonFormatter
from ecc.platform.connector_security import revoke_guarded

pytestmark = pytest.mark.skipif(
    not get_settings().database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)

_FLAG_ON = {"ECC_PERSONAL_DATA_ISOLATION": "true"}


# --- the outcome matrix -------------------------------------------------------


@dataclass(frozen=True)
class RevokeCase:
    """One Google `/revoke` outcome (or a credential that never reaches
    Google) and the metric result it must produce."""

    name: str
    expected: str  # "ok" | "error"
    status_code: int = 200
    body: dict[str, Any] | None = None
    transport_error: bool = False
    # Stored-credential override: None keeps the real one.
    credential: str | None = None
    sends_token: bool = True


_EXPIRES = datetime(2030, 1, 1, tzinfo=UTC)

CASES: tuple[RevokeCase, ...] = (
    RevokeCase("200", "ok"),
    RevokeCase("400_invalid_token", "ok", status_code=400, body={"error": "invalid_token"}),
    RevokeCase("400_other", "error", status_code=400, body={"error": "invalid_request"}),
    RevokeCase("503", "error", status_code=503),
    RevokeCase("transport_error", "error", transport_error=True),
    RevokeCase("bad_credential", "error", credential="not-json", sends_token=False),
    RevokeCase(
        "empty_refresh_token",
        "error",
        credential=pack_credential("access-empty", "", _EXPIRES),
        sends_token=False,
    ),
)
_REPLY_CASES = tuple(c for c in CASES if c.credential is None)


def _arrange(fake: FakeGoogle, case: RevokeCase) -> None:
    fake.revoke_status_code = case.status_code
    fake.revoke_body = case.body
    fake.revoke_transport_error = case.transport_error


def _revokes(site: str, result: str) -> float:
    return observability.connector_revoke_total._values.get(("gmail", site, result), 0.0)


def _counts(site: str) -> dict[str, float]:
    return {r: _revokes(site, r) for r in ("ok", "error", "skipped_unsafe")}


def _assert_counted(site: str, before: dict[str, float], expected: str) -> None:
    after = _counts(site)
    delta = {r: after[r] - before[r] for r in after}
    assert delta == {r: (1.0 if r == expected else 0.0) for r in after}, delta


def _rendered_logs(caplog: pytest.LogCaptureFixture) -> str:
    return " ".join(JsonFormatter().format(r) for r in caplog.records)


def _assert_logs_clean(
    caplog: pytest.LogCaptureFixture, *, secrets: tuple[str, ...], case: RevokeCase, site: str
) -> None:
    rendered = _rendered_logs(caplog)
    for secret in secrets:
        assert secret not in rendered
    if case.expected == "error":
        assert "error_class=GmailRevokeFailed" in rendered
        assert f"site={site}" in rendered
        assert "gmail_revoke_failed: reason=" in rendered
    else:
        assert "connector_revoke_failed" not in rendered
        assert "gmail_revoke_failed" not in rendered


# --- adapter level --------------------------------------------------------------


def _fake_transport(case: RevokeCase, sent: list[str]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/revoke"
        sent.append(request.content.decode())
        if case.transport_error:
            raise httpx.ConnectError("boom", request=request)
        if case.body is None:
            return httpx.Response(case.status_code)
        return httpx.Response(case.status_code, json=case.body)

    return httpx.MockTransport(handler)


def _context(credential: str) -> ConnectorAccountContext:
    return ConnectorAccountContext(
        workspace_id=uuid4(),
        connector_account_id=uuid4(),
        external_account_id="someone@example.test",
        credential=credential,
    )


_LIVE_CREDENTIAL = pack_credential("access-live", "refresh-live", _EXPIRES)


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.name)
def test_disconnect_outcome_matrix(case: RevokeCase) -> None:
    sent: list[str] = []
    adapter = GmailAdapter(transport=_fake_transport(case, sent))
    context = _context(case.credential or _LIVE_CREDENTIAL)
    if case.expected == "ok":
        assert adapter.disconnect(context) is None
    else:
        with pytest.raises(GmailRevokeFailed) as excinfo:
            adapter.disconnect(context)
        # The message is the static reason code: never the token.
        assert str(excinfo.value) == excinfo.value.reason
        assert "refresh-live" not in str(excinfo.value)
        assert excinfo.value.__cause__ is None
        assert excinfo.value.__suppress_context__ is True
    assert sent == (["token=refresh-live"] if case.sends_token else [])


@pytest.mark.parametrize(
    ("status_code", "body", "reason"),
    [
        (204, None, None),
        (400, {"error": "invalid_token", "error_description": "Token expired"}, None),
        (400, None, "provider_4xx"),
        (400, ["invalid_token"], "provider_4xx"),
        (401, {"error": "invalid_token"}, "provider_4xx"),
        (429, None, "provider_4xx"),
        (500, None, "provider_5xx"),
        (302, None, "provider_unexpected_status"),
    ],
)
def test_revoke_reply_interpretation(status_code: int, body: Any, reason: str | None) -> None:
    """Only a 2xx or a 400 `invalid_token` means the grant is gone."""
    response = (
        httpx.Response(status_code) if body is None else httpx.Response(status_code, json=body)
    )
    assert gmail_adapter_module._revoke_reply_failure(response) == reason


def test_disconnect_missing_refresh_token_key_raises() -> None:
    credential = dumps({"access_token": "a", "expires_at": "2030-01-01T00:00:00+00:00"})
    with pytest.raises(GmailRevokeFailed) as excinfo:
        GmailAdapter(transport=_fake_transport(CASES[0], [])).disconnect(_context(credential))
    assert excinfo.value.reason == "missing_refresh_token"


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.name)
def test_revoke_guarded_counts_the_real_outcome_and_never_raises(
    case: RevokeCase, caplog: pytest.LogCaptureFixture
) -> None:
    adapter = GmailAdapter(transport=_fake_transport(case, []))
    before = _counts("removal")
    with caplog.at_level(logging.WARNING):
        result = revoke_guarded(
            adapter, _context(case.credential or _LIVE_CREDENTIAL), provider="gmail", site="removal"
        )
    assert result is (case.expected == "ok")
    _assert_counted("removal", before, case.expected)
    _assert_logs_clean(
        caplog,
        secrets=("refresh-live", "access-live", "someone@example.test"),
        case=case,
        site="removal",
    )


# --- end to end: removal and cascade --------------------------------------------


@pytest.fixture
def world() -> Iterator[GmailSyncWorld]:
    with build_gmail_sync_world(env=_FLAG_ON) as built:
        built.fake_google.revoked_tokens.clear()
        yield built


def _connector_status(connector_account_id: UUID) -> str:
    with engine.connect() as connection:
        return str(
            connection.execute(
                text("SELECT status FROM connector_accounts WHERE id = :id"),
                {"id": connector_account_id},
            ).scalar_one()
        )


def _store_credential(connector_account_id: UUID, credential: str) -> None:
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE connector_accounts SET encrypted_credentials = :c WHERE id = :id"),
            {"c": encrypt_credential(credential), "id": connector_account_id},
        )


def _secrets(world: GmailSyncWorld) -> tuple[str, ...]:
    return (
        FakeGoogle.refresh_token("b"),
        FakeGoogle.access_token("b"),
        world.b.google_email,
    )


def _run_site(
    world: GmailSyncWorld,
    case: RevokeCase,
    caplog: pytest.LogCaptureFixture,
    *,
    site: str,
    act: Callable[[], httpx.Response],
) -> httpx.Response:
    _arrange(world.fake_google, case)
    if case.credential is not None:
        _store_credential(world.b.connector_account_id, case.credential)
    before = _counts(site)
    caplog.clear()
    with caplog.at_level(logging.INFO):
        response = act()
    # The user-facing operation succeeds whatever Google said.
    assert response.status_code == 200, response.text
    assert _connector_status(world.b.connector_account_id) == "disconnected"
    _assert_counted(site, before, case.expected)
    assert world.fake_google.revoked_tokens == (
        [FakeGoogle.refresh_token("b")] if case.sends_token else []
    )
    _assert_logs_clean(caplog, secrets=_secrets(world), case=case, site=site)
    # A's grant is never touched.
    assert _connector_status(world.a.connector_account_id) == "active"
    return response


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.name)
def test_member_removal_reports_the_real_revoke_outcome(
    world: GmailSyncWorld, case: RevokeCase, caplog: pytest.LogCaptureFixture
) -> None:
    def remove() -> httpx.Response:
        client, token = world.harness.client_for(world.workspace_id, world.a.user_id)
        return client.delete(
            f"/api/v1/identity/workspaces/{world.workspace_id}/members/{world.b.user_id}",
            headers=csrf_headers(token),
        )

    _run_site(world, case, caplog, site="removal", act=remove)
    with engine.connect() as connection:
        status = connection.execute(
            text(
                "SELECT status FROM workspace_memberships "
                "WHERE workspace_id = :ws AND users_id = :uid"
            ),
            {"ws": world.workspace_id, "uid": world.b.user_id},
        ).scalar_one()
    assert status == "removed"


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.name)
def test_email_domain_disable_cascade_reports_the_real_revoke_outcome(
    world: GmailSyncWorld, case: RevokeCase, caplog: pytest.LogCaptureFixture
) -> None:
    def disable() -> httpx.Response:
        return world.client("b").post(
            "/api/v1/personal/domains/email/disable",
            headers=world.headers("b", idempotency_key=str(uuid4())),
        )

    response = _run_site(world, case, caplog, site="cascade", act=disable)
    assert response.json()["enabled"] is False
    # The purge still ran.
    with engine.connect() as connection:
        threads = connection.execute(
            text("SELECT count(*) FROM email_threads WHERE connector_account_id = :id"),
            {"id": world.b.connector_account_id},
        ).scalar_one()
    assert threads == 0


# --- adapter_callback -------------------------------------------------------------


def test_rejected_callback_reports_the_real_revoke_outcome(
    world: GmailSyncWorld, caplog: pytest.LogCaptureFixture
) -> None:
    """B consents as a Google account that is not on the allowlist and that
    no live row uses: the adapter rejects the callback after the token
    exchange and revokes the minted grant (safe under `global`). The
    `adapter_callback` count follows Google's real reply; the user always
    gets the rejection (422), never a 500."""
    unlisted = f"unlisted-{uuid4().hex[:8]}@example.test"
    mailbox = FakeMailbox(
        google_email=unlisted,
        messages=(
            FakeGmailMessage(
                external_message_id=f"m-{uuid4().hex[:8]}",
                external_thread_id=f"t-{uuid4().hex[:8]}",
                sender_email="x@partner.test",
                sender_name="X",
                subject="s",
                body_text="b",
                sent_at=datetime.now(UTC) - timedelta(hours=1),
            ),
        ),
    )
    code = world.fake_google.register("unlisted", mailbox)
    client, token = world.harness.client_for(world.workspace_id, world.b.user_id)

    for case in _REPLY_CASES:
        _arrange(world.fake_google, case)
        world.fake_google.revoked_tokens.clear()
        before = _counts("adapter_callback")
        start = client.post("/api/v1/personal/gmail/oauth/start", headers=csrf_headers(token))
        assert start.status_code == 200, start.text
        state = httpx.URL(start.json()["authorization_url"]).params["state"]
        caplog.clear()
        with caplog.at_level(logging.INFO):
            response = client.get(
                "/api/v1/personal/gmail/oauth/callback", params={"code": code, "state": state}
            )
        assert response.status_code == 422, (case.name, response.text)
        assert world.fake_google.revoked_tokens == [FakeGoogle.refresh_token("unlisted")]
        _assert_counted("adapter_callback", before, case.expected)
        rendered = _rendered_logs(caplog)
        for secret in (FakeGoogle.refresh_token("unlisted"), unlisted):
            assert secret not in rendered, case.name
        assert ("gmail_revoke_failed: reason=" in rendered) is (case.expected == "error")


def test_rejected_callback_with_no_refresh_token_counts_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The minted grant came back without a `refresh_token`: nothing can be
    sent to Google, so the hooked callback counts `error`, not `ok`."""
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_SECRET", "csecret")
    get_settings.cache_clear()
    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            return httpx.Response(
                200, json={"access_token": "access-x", "expires_in": 3600, "scope": "x"}
            )
        sent.append(request.url.path)
        return httpx.Response(200)

    try:
        before = _counts("adapter_callback")
        adapter = GmailAdapter(transport=httpx.MockTransport(handler))
        with pytest.raises(AdapterAuthorizationError):
            adapter.handle_oauth_callback("code", "state", revoke_on_reject=lambda _email: True)
        assert sent == []
        _assert_counted("adapter_callback", before, "error")
    finally:
        get_settings.cache_clear()


# --- a non-httpx exception from the revoke POST ---------------------------------


def _raise_on_revoke(adapter: GmailAdapter, monkeypatch: pytest.MonkeyPatch) -> None:
    """Make only the `/revoke` POST raise a non-httpx exception (e.g. a
    closed client); every other OAuth request goes through unchanged."""
    real_post = adapter._oauth_client.post

    def post(url: str, *args: Any, **kwargs: Any) -> httpx.Response:
        if url == "/revoke":
            raise RuntimeError("client has been closed refresh-live")
        return real_post(url, *args, **kwargs)

    monkeypatch.setattr(adapter._oauth_client, "post", post)


def test_disconnect_non_http_exception_raises_revoke_failed(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    adapter = GmailAdapter(transport=_fake_transport(CASES[0], []))
    _raise_on_revoke(adapter, monkeypatch)
    with caplog.at_level(logging.WARNING), pytest.raises(GmailRevokeFailed) as excinfo:
        adapter.disconnect(_context(_LIVE_CREDENTIAL))
    assert excinfo.value.reason == "transport_error"
    rendered = _rendered_logs(caplog)
    assert "gmail_revoke_post_failed: error_class=RuntimeError" in rendered
    assert "refresh-live" not in rendered
    assert "client has been closed" not in rendered


def test_rejected_callback_non_http_revoke_exception_keeps_the_rejection(
    world: GmailSyncWorld,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The revoke POST raising a non-httpx exception inside the callback's
    reject guard must not replace the original rejection (still 422, not
    500) and must still count `adapter_callback` as `error`."""
    import ecc.domains.personal.gmail_oauth as gmail_oauth_module  # noqa: PLC0415

    unlisted = f"unlisted-{uuid4().hex[:8]}@example.test"
    code = world.fake_google.register(
        "unlisted-rt", FakeMailbox(google_email=unlisted, messages=())
    )
    _raise_on_revoke(gmail_oauth_module._adapter, monkeypatch)
    client, token = world.harness.client_for(world.workspace_id, world.b.user_id)
    start = client.post("/api/v1/personal/gmail/oauth/start", headers=csrf_headers(token))
    assert start.status_code == 200, start.text
    state = httpx.URL(start.json()["authorization_url"]).params["state"]
    before = _counts("adapter_callback")
    caplog.clear()
    with caplog.at_level(logging.INFO):
        response = client.get(
            "/api/v1/personal/gmail/oauth/callback", params={"code": code, "state": state}
        )
    assert response.status_code == 422, response.text
    _assert_counted("adapter_callback", before, "error")
    rendered = _rendered_logs(caplog)
    assert "gmail_revoke_post_failed: error_class=RuntimeError" in rendered
    for secret in (FakeGoogle.refresh_token("unlisted-rt"), unlisted, "client has been closed"):
        assert secret not in rendered
