"""Phase 10 Task 6's on-demand thread reading and per-thread "forget this"
(`docs/superpowers/plans/2026-08-04-phase-10-gmail-connector.md` Task 6),
against a real Postgres database and the real `TestClient`-driven HTTP
endpoints (`ecc.domains.personal.gmail_threads`).

Covers, in the same order these tests physically appear below (round 4
review finding: an earlier version of this list was numbered in a
different order than the file itself, which made it actively misleading
to a reader scanning top-to-bottom -- kept in sync with physical order
going forward rather than insertion order):
1. `GET /threads/{thread_id}` fetches a still-unfetched message's body via
   the exact same fetch-and-store mechanism Task 5 uses
   (`GmailAdapter.fetch_and_store_body`), stores it encrypted, and returns
   the decrypted thread content.
2. `GET` on a thread with two unfetched messages, where one message's
   live fetch raises a transient `httpx` error, still returns 200 with
   the other message's content -- one message's failure does not fail
   the whole request (round 2 review finding).
3. `GET` on a thread whose connector account is disconnected skips the
   live-fetch attempt entirely rather than decrypting and using a
   likely-revoked credential (round 3 review finding) -- proven with a
   call-spy directly on `fetch_and_store_body`, not merely a raising
   transport, since the endpoint's own per-message `except Exception:
   continue` (added for item 2 above) would otherwise silently mask a
   raising transport's exception too, making "skipped" and "attempted,
   then masked" indistinguishable (round 4 review finding).
4. A second `GET` on an already-fetched thread does not re-fetch (the
   `body IS NULL` guard is respected) -- proven with a transport that
   raises if called, not merely asserting the response looks right.
5. `GET` without an active `email` domain consent is rejected (`403`), no
   Gmail call made.
6. `GET` on a thread that does not exist, or belongs to a different
   workspace, is a non-disclosing `404`.
7. `POST .../forget` nulls `snippet`/`body`/`body_fetched_at` for every
   message in the targeted thread only -- a second, untouched thread in
   the same domain keeps its own cached content, proving the scoping
   (the plan's own "removes only the targeted thread's cached content"),
   and writes exactly one `deletion_jobs` row, `scope='thread'`,
   `resource_id=<thread_id>`, `status='completed'`.
8. `POST .../forget` idempotency-key replay returns the cached response
   without inserting a second `deletion_jobs` row.
9. `POST .../forget` on a thread that does not exist is a `404`.
10. A thread `GET`-opened again after being forgotten re-fetches its
    content from Gmail -- proving "forget" nulls the cache rather than
    deleting the message row (which would otherwise make the thread
    unreadable, not merely uncached).

Task 8's own additions, appended rather than renumbered into the list
above (matching this file's own "kept in sync with physical order"
discipline for the block it belongs to):
11. `GET /threads` (list) returns the caller's own threads newest-
    `last_message_at`-first, each with its most recent message's
    `sender`/`direction`, a `message_count`, and a `body_cached` flag
    computed over every message in the thread, not only the most recent
    one.
12. `GET /threads` respects an explicit `limit` query parameter.
13. `GET /threads` is scoped to the caller's own `workspace_id`/`owner_id`
    -- a thread belonging to a different owner in the same workspace, or a
    different workspace entirely, never appears (two separate tests: the
    round-2-review-added same-workspace case, and the pre-existing
    different-workspace case, since varying both workspace_id and owner_id
    together in one test would not actually prove same-workspace isolation
    on its own).
14. `GET /threads` is rejected `403` without an active `email` domain
    consent, matching `GET /threads/{thread_id}`'s identical gate.
"""

import base64
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from hmac import new
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from identity_fixtures import create_identity
from sqlalchemy import text

from ecc.config import get_settings
from ecc.database import engine
from ecc.domains.engineering.crypto import encrypt_credential
from ecc.domains.personal import gmail_threads
from ecc.domains.personal.crypto import decrypt_field
from ecc.domains.personal.crypto import encrypt_field as _encrypt_field
from ecc.domains.personal.gmail_adapter import GmailAdapter, _pack_credential
from ecc.main import app

settings = get_settings()
pytestmark = pytest.mark.skipif(
    not settings.database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)

_OWNER_EMAIL = "gmail-threads-owner@example.test"
_SENDER = "colleague@partner-co.test"
_PLAIN_TEXT_BODY = "Here is the thread content you asked to read."


def _headers(token: str, key: str | None = None) -> dict[str, str]:
    csrf = new(settings.session_secret.encode(), token.encode(), "sha256").hexdigest()
    headers = {"X-CSRF-Token": csrf, "X-Correlation-ID": str(uuid4())}
    if key is not None:
        headers["Idempotency-Key"] = key
    return headers


def _raising_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected Gmail API call: {request.url}")

    return httpx.MockTransport(handler)


def _gmail_transport(*external_message_ids: str) -> httpx.MockTransport:
    encoded_body = base64.urlsafe_b64encode(_PLAIN_TEXT_BODY.encode()).decode().rstrip("=")
    allowed = set(external_message_ids)

    def handler(request: httpx.Request) -> httpx.Response:
        external_id = request.url.path.rsplit("/", 1)[-1]
        assert external_id in allowed, f"unexpected message fetch: {external_id}"
        return httpx.Response(
            200,
            json={
                "id": external_id,
                "payload": {"mimeType": "text/plain", "body": {"data": encoded_body}},
            },
        )

    return httpx.MockTransport(handler)


def _gmail_transport_with_one_failure(
    ok_external_message_id: str, failing_external_message_id: str
) -> httpx.MockTransport:
    encoded_body = base64.urlsafe_b64encode(_PLAIN_TEXT_BODY.encode()).decode().rstrip("=")

    def handler(request: httpx.Request) -> httpx.Response:
        external_id = request.url.path.rsplit("/", 1)[-1]
        if external_id == failing_external_message_id:
            raise httpx.ConnectError("simulated transient Gmail failure")
        assert external_id == ok_external_message_id, f"unexpected message fetch: {external_id}"
        return httpx.Response(
            200,
            json={
                "id": external_id,
                "payload": {"mimeType": "text/plain", "body": {"data": encoded_body}},
            },
        )

    return httpx.MockTransport(handler)


def _insert_thread(
    connection,
    *,
    workspace_id: UUID,
    owner_id: UUID,
    account_id: UUID,
    subject: str,
    now: datetime,
    last_message_at: datetime | None = None,
) -> UUID:
    thread_id = uuid4()
    connection.execute(
        text(
            """
            INSERT INTO email_threads (
                id, workspace_id, owner_id, domain_key, connector_account_id,
                external_thread_id, subject, last_message_at, created_at, updated_at
            ) VALUES (
                :id, :workspace_id, :owner_id, 'email', :connector_account_id,
                :external_thread_id, :subject, :last_message_at, :now, :now
            )
            """
        ),
        {
            "id": thread_id,
            "workspace_id": workspace_id,
            "owner_id": owner_id,
            "connector_account_id": account_id,
            "external_thread_id": f"thread-{thread_id}",
            "subject": subject,
            "last_message_at": last_message_at or now,
            "now": now,
        },
    )
    return thread_id


def _insert_message(
    connection,
    *,
    workspace_id: UUID,
    owner_id: UUID,
    thread_id: UUID,
    external_message_id: str,
    now: datetime,
    fetched: bool,
    sent_at: datetime | None = None,
    sender: str = _SENDER,
    direction: str = "inbound",
) -> UUID:
    message_id = uuid4()
    body = _encrypt_field(_PLAIN_TEXT_BODY) if fetched else None
    connection.execute(
        text(
            """
            INSERT INTO email_messages (
                id, workspace_id, owner_id, thread_id, external_message_id,
                sender, recipients, sent_at, direction, snippet, body,
                body_fetched_at, created_at, updated_at
            ) VALUES (
                :id, :workspace_id, :owner_id, :thread_id, :external_message_id,
                :sender, :recipients, :sent_at, :direction, :snippet, :body,
                :body_fetched_at, :now, :now
            )
            """
        ),
        {
            "id": message_id,
            "workspace_id": workspace_id,
            "owner_id": owner_id,
            "thread_id": thread_id,
            "external_message_id": external_message_id,
            "sender": sender,
            "recipients": [_OWNER_EMAIL],
            "sent_at": sent_at or now,
            "direction": direction,
            "now": now,
            "snippet": "preview" if fetched else None,
            "body": body,
            "body_fetched_at": now if fetched else None,
        },
    )
    return message_id


def _cleanup_workspace(workspace_id: UUID, *, email: str = _OWNER_EMAIL) -> None:
    with engine.begin() as connection:
        for table in (
            "deletion_jobs",
            "idempotency_records",
            "audit_events",
            "event_outbox",
            "email_messages",
            "email_threads",
            "domain_consents",
            "personal_domains",
            "connector_accounts",
            "sessions",
            "users",
        ):
            connection.execute(
                text(f"DELETE FROM {table} WHERE workspace_id = :workspace_id"),  # noqa: S608
                {"workspace_id": workspace_id},
            )
        connection.execute(
            text("DELETE FROM workspaces WHERE id = :workspace_id"), {"workspace_id": workspace_id}
        )
        # `accounts` is not workspace-scoped (Phase 8 Decision 1 -- one
        # `accounts` row can anchor `users` rows in more than one workspace),
        # so it needs its own delete by email, matching the established
        # `_teardown_workspace` precedent in
        # `test_gmail_action_detection_sync_postgres.py`.
        connection.execute(text("DELETE FROM accounts WHERE email = :email"), {"email": email})


@pytest.fixture
def gmail_threads_context() -> Iterator[dict]:
    workspace_id = uuid4()
    owner_id = uuid4()
    account_id = uuid4()
    token = f"session-{uuid4()}"
    now = datetime.now(UTC)
    credential = _pack_credential("access-1", "refresh-1", now + timedelta(hours=1))

    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, 'Gmail Threads Test', 'UTC', :now)"
            ),
            {"id": workspace_id, "now": now},
        )
        create_identity(
            connection, workspace_id=workspace_id, user_id=owner_id, email=_OWNER_EMAIL, now=now
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
                "user_id": owner_id,
                "token_hash": sha256(token.encode()).hexdigest(),
                "expires_at": now + timedelta(hours=1),
                "now": now,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO connector_accounts (
                    id, workspace_id, provider, external_account_id, display_name,
                    granted_scopes, encrypted_credentials, status, version,
                    created_by, updated_by, created_at, updated_at, owner_id, visibility
                ) VALUES (
                    :id, :workspace_id, 'gmail', :external_account_id, 'Threads test account',
                    ARRAY['https://www.googleapis.com/auth/gmail.readonly'], :encrypted,
                    'active', 1, :actor_id, :actor_id, :now, :now, :actor_id, 'workspace'
                )
                """
            ),
            {
                "id": account_id,
                "workspace_id": workspace_id,
                "external_account_id": _OWNER_EMAIL,
                "encrypted": encrypt_credential(credential),
                "actor_id": owner_id,
                "now": now,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO personal_domains (
                    id, workspace_id, owner_id, domain_key, classification, enabled,
                    enabled_at, created_by, updated_by, created_at, updated_at, version
                ) VALUES (
                    :id, :workspace_id, :owner_id, 'email', 'high_stakes', true,
                    :now, :owner_id, :owner_id, :now, :now, 1
                )
                """
            ),
            {"id": uuid4(), "workspace_id": workspace_id, "owner_id": owner_id, "now": now},
        )
        connection.execute(
            text(
                "INSERT INTO domain_consents (id, workspace_id, owner_id, domain_key, "
                "granted_at, created_at) "
                "VALUES (:id, :workspace_id, :owner_id, 'email', :now, :now)"
            ),
            {"id": uuid4(), "workspace_id": workspace_id, "owner_id": owner_id, "now": now},
        )

    client = TestClient(app)
    client.cookies.set("ecc_session", token)
    try:
        yield {
            "client": client,
            "token": token,
            "workspace_id": workspace_id,
            "owner_id": owner_id,
            "account_id": account_id,
            "now": now,
        }
    finally:
        client.close()
        _cleanup_workspace(workspace_id)


def _message_row(workspace_id: UUID, message_id: UUID) -> dict:
    with engine.begin() as connection:
        row = (
            connection.execute(
                text(
                    "SELECT snippet, body, body_fetched_at FROM email_messages "
                    "WHERE workspace_id = :workspace_id AND id = :id"
                ),
                {"workspace_id": workspace_id, "id": message_id},
            )
            .mappings()
            .one()
        )
    return dict(row)


def test_get_thread_fetches_unfetched_message_and_returns_decrypted_content(
    gmail_threads_context: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = gmail_threads_context
    with engine.begin() as connection:
        thread_id = _insert_thread(
            connection,
            workspace_id=ctx["workspace_id"],
            owner_id=ctx["owner_id"],
            account_id=ctx["account_id"],
            subject="Please read this",
            now=ctx["now"],
        )
        external_message_id = f"msg-{uuid4()}"
        message_id = _insert_message(
            connection,
            workspace_id=ctx["workspace_id"],
            owner_id=ctx["owner_id"],
            thread_id=thread_id,
            external_message_id=external_message_id,
            now=ctx["now"],
            fetched=False,
        )

    monkeypatch.setattr(
        gmail_threads, "_adapter", GmailAdapter(transport=_gmail_transport(external_message_id))
    )

    resp = ctx["client"].get(f"/api/v1/personal/gmail/threads/{thread_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["subject"] == "Please read this"
    assert len(body["messages"]) == 1
    assert body["messages"][0]["body"] == _PLAIN_TEXT_BODY

    row = _message_row(ctx["workspace_id"], message_id)
    assert row["body_fetched_at"] is not None
    assert decrypt_field(row["body"]) == _PLAIN_TEXT_BODY


def test_get_thread_one_message_fetch_failure_does_not_fail_the_request(
    gmail_threads_context: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = gmail_threads_context
    with engine.begin() as connection:
        thread_id = _insert_thread(
            connection,
            workspace_id=ctx["workspace_id"],
            owner_id=ctx["owner_id"],
            account_id=ctx["account_id"],
            subject="Partial failure",
            now=ctx["now"],
        )
        ok_external_message_id = f"msg-{uuid4()}"
        ok_message_id = _insert_message(
            connection,
            workspace_id=ctx["workspace_id"],
            owner_id=ctx["owner_id"],
            thread_id=thread_id,
            external_message_id=ok_external_message_id,
            now=ctx["now"],
            fetched=False,
        )
        failing_external_message_id = f"msg-{uuid4()}"
        failing_message_id = _insert_message(
            connection,
            workspace_id=ctx["workspace_id"],
            owner_id=ctx["owner_id"],
            thread_id=thread_id,
            external_message_id=failing_external_message_id,
            now=ctx["now"],
            fetched=False,
        )

    monkeypatch.setattr(
        gmail_threads,
        "_adapter",
        GmailAdapter(
            transport=_gmail_transport_with_one_failure(
                ok_external_message_id, failing_external_message_id
            )
        ),
    )

    resp = ctx["client"].get(f"/api/v1/personal/gmail/threads/{thread_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["messages"]) == 1
    assert body["messages"][0]["body"] == _PLAIN_TEXT_BODY

    ok_row = _message_row(ctx["workspace_id"], ok_message_id)
    assert ok_row["body_fetched_at"] is not None

    failing_row = _message_row(ctx["workspace_id"], failing_message_id)
    assert failing_row["body_fetched_at"] is None
    assert failing_row["body"] is None


def test_get_thread_with_disconnected_connector_skips_live_fetch(
    gmail_threads_context: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = gmail_threads_context
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE connector_accounts SET status = 'disconnected' WHERE id = :id"),
            {"id": ctx["account_id"]},
        )
        thread_id = _insert_thread(
            connection,
            workspace_id=ctx["workspace_id"],
            owner_id=ctx["owner_id"],
            account_id=ctx["account_id"],
            subject="Disconnected connector",
            now=ctx["now"],
        )
        _insert_message(
            connection,
            workspace_id=ctx["workspace_id"],
            owner_id=ctx["owner_id"],
            thread_id=thread_id,
            external_message_id=f"msg-{uuid4()}",
            now=ctx["now"],
            fetched=False,
        )

    # A `_raising_transport()` alone would not actually prove the live
    # fetch was skipped: `get_thread_endpoint`'s own per-message `except
    # Exception: continue` (added for the round-2 partial-failure fix)
    # would silently swallow the `AssertionError` a raising transport
    # produces, exactly as it swallows a genuine transient failure --
    # making "skipped" and "attempted, then masked" indistinguishable by
    # response shape alone (round 4 review finding). A call-spy on
    # `fetch_and_store_body` itself, upstream of that `try`/`except`, is
    # the only assertion that is actually causally tied to the
    # disconnected-status check rather than to "was some exception raised
    # somewhere in the loop."
    calls: list[UUID] = []
    monkeypatch.setattr(
        gmail_threads._adapter,
        "fetch_and_store_body",
        lambda **kwargs: calls.append(kwargs["message_id"]),
    )

    resp = ctx["client"].get(f"/api/v1/personal/gmail/threads/{thread_id}")
    assert resp.status_code == 200
    assert resp.json()["messages"] == []
    assert calls == []


def test_get_thread_already_fetched_does_not_call_gmail_again(
    gmail_threads_context: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = gmail_threads_context
    with engine.begin() as connection:
        thread_id = _insert_thread(
            connection,
            workspace_id=ctx["workspace_id"],
            owner_id=ctx["owner_id"],
            account_id=ctx["account_id"],
            subject="Already read",
            now=ctx["now"],
        )
        _insert_message(
            connection,
            workspace_id=ctx["workspace_id"],
            owner_id=ctx["owner_id"],
            thread_id=thread_id,
            external_message_id=f"msg-{uuid4()}",
            now=ctx["now"],
            fetched=True,
        )

    monkeypatch.setattr(gmail_threads, "_adapter", GmailAdapter(transport=_raising_transport()))

    resp = ctx["client"].get(f"/api/v1/personal/gmail/threads/{thread_id}")
    assert resp.status_code == 200
    assert resp.json()["messages"][0]["body"] == _PLAIN_TEXT_BODY


def test_get_thread_without_active_consent_is_rejected(
    gmail_threads_context: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = gmail_threads_context
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE domain_consents SET revoked_at = :now "
                "WHERE workspace_id = :workspace_id AND owner_id = :owner_id "
                "AND domain_key = 'email'"
            ),
            {"now": ctx["now"], "workspace_id": ctx["workspace_id"], "owner_id": ctx["owner_id"]},
        )
        thread_id = _insert_thread(
            connection,
            workspace_id=ctx["workspace_id"],
            owner_id=ctx["owner_id"],
            account_id=ctx["account_id"],
            subject="No consent",
            now=ctx["now"],
        )
        _insert_message(
            connection,
            workspace_id=ctx["workspace_id"],
            owner_id=ctx["owner_id"],
            thread_id=thread_id,
            external_message_id=f"msg-{uuid4()}",
            now=ctx["now"],
            fetched=False,
        )

    monkeypatch.setattr(gmail_threads, "_adapter", GmailAdapter(transport=_raising_transport()))

    resp = ctx["client"].get(f"/api/v1/personal/gmail/threads/{thread_id}")
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "EMAIL_CONSENT_NOT_ACTIVE"


def test_get_thread_not_found_is_non_disclosing_404(gmail_threads_context: dict) -> None:
    ctx = gmail_threads_context
    resp = ctx["client"].get(f"/api/v1/personal/gmail/threads/{uuid4()}")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "THREAD_NOT_FOUND"


def test_get_thread_belonging_to_a_different_workspace_is_404(gmail_threads_context: dict) -> None:
    ctx = gmail_threads_context
    other_workspace_id = uuid4()
    other_owner_id = uuid4()
    now = ctx["now"]
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, 'Other Workspace', 'UTC', :now)"
            ),
            {"id": other_workspace_id, "now": now},
        )
        create_identity(
            connection,
            workspace_id=other_workspace_id,
            user_id=other_owner_id,
            email="other-owner@example.test",
            now=now,
        )
        other_account_id = uuid4()
        connection.execute(
            text(
                """
                INSERT INTO connector_accounts (
                    id, workspace_id, provider, external_account_id, display_name,
                    granted_scopes, encrypted_credentials, status, version,
                    created_by, updated_by, created_at, updated_at, owner_id, visibility
                ) VALUES (
                    :id, :workspace_id, 'gmail', 'other-owner@example.test', 'Other account',
                    ARRAY['https://www.googleapis.com/auth/gmail.readonly'], :encrypted,
                    'active', 1, :actor_id, :actor_id, :now, :now, :actor_id, 'workspace'
                )
                """
            ),
            {
                "id": other_account_id,
                "workspace_id": other_workspace_id,
                "encrypted": encrypt_credential(
                    _pack_credential("access-2", "refresh-2", now + timedelta(hours=1))
                ),
                "actor_id": other_owner_id,
                "now": now,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO personal_domains (
                    id, workspace_id, owner_id, domain_key, classification, enabled,
                    enabled_at, created_by, updated_by, created_at, updated_at, version
                ) VALUES (
                    :id, :workspace_id, :owner_id, 'email', 'high_stakes', true,
                    :now, :owner_id, :owner_id, :now, :now, 1
                )
                """
            ),
            {
                "id": uuid4(),
                "workspace_id": other_workspace_id,
                "owner_id": other_owner_id,
                "now": now,
            },
        )
        other_thread_id = _insert_thread(
            connection,
            workspace_id=other_workspace_id,
            owner_id=other_owner_id,
            account_id=other_account_id,
            subject="Not yours",
            now=now,
        )

    try:
        resp = ctx["client"].get(f"/api/v1/personal/gmail/threads/{other_thread_id}")
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "THREAD_NOT_FOUND"
    finally:
        _cleanup_workspace(other_workspace_id, email="other-owner@example.test")


def test_forget_thread_removes_only_targeted_threads_content(gmail_threads_context: dict) -> None:
    ctx = gmail_threads_context
    with engine.begin() as connection:
        forgotten_thread_id = _insert_thread(
            connection,
            workspace_id=ctx["workspace_id"],
            owner_id=ctx["owner_id"],
            account_id=ctx["account_id"],
            subject="Forget me",
            now=ctx["now"],
        )
        forgotten_message_id = _insert_message(
            connection,
            workspace_id=ctx["workspace_id"],
            owner_id=ctx["owner_id"],
            thread_id=forgotten_thread_id,
            external_message_id=f"msg-{uuid4()}",
            now=ctx["now"],
            fetched=True,
        )
        surviving_thread_id = _insert_thread(
            connection,
            workspace_id=ctx["workspace_id"],
            owner_id=ctx["owner_id"],
            account_id=ctx["account_id"],
            subject="Keep me",
            now=ctx["now"],
        )
        surviving_message_id = _insert_message(
            connection,
            workspace_id=ctx["workspace_id"],
            owner_id=ctx["owner_id"],
            thread_id=surviving_thread_id,
            external_message_id=f"msg-{uuid4()}",
            now=ctx["now"],
            fetched=True,
        )

    resp = ctx["client"].post(
        f"/api/v1/personal/gmail/threads/{forgotten_thread_id}/forget",
        headers=_headers(ctx["token"], str(uuid4())),
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "completed"
    assert resp.json()["thread_id"] == str(forgotten_thread_id)

    forgotten_row = _message_row(ctx["workspace_id"], forgotten_message_id)
    assert forgotten_row["body"] is None
    assert forgotten_row["snippet"] is None
    assert forgotten_row["body_fetched_at"] is None

    surviving_row = _message_row(ctx["workspace_id"], surviving_message_id)
    assert surviving_row["body"] is not None
    assert decrypt_field(surviving_row["body"]) == _PLAIN_TEXT_BODY
    assert surviving_row["body_fetched_at"] is not None

    with engine.begin() as connection:
        jobs = (
            connection.execute(
                text(
                    "SELECT status, scope, resource_id, domain_key FROM deletion_jobs "
                    "WHERE workspace_id = :workspace_id"
                ),
                {"workspace_id": ctx["workspace_id"]},
            )
            .mappings()
            .all()
        )
    assert len(jobs) == 1
    assert jobs[0]["status"] == "completed"
    assert jobs[0]["scope"] == "thread"
    assert jobs[0]["resource_id"] == forgotten_thread_id
    assert jobs[0]["domain_key"] == "email"


def test_forget_thread_idempotency_replay_does_not_insert_a_second_job(
    gmail_threads_context: dict,
) -> None:
    ctx = gmail_threads_context
    with engine.begin() as connection:
        thread_id = _insert_thread(
            connection,
            workspace_id=ctx["workspace_id"],
            owner_id=ctx["owner_id"],
            account_id=ctx["account_id"],
            subject="Replay me",
            now=ctx["now"],
        )
        _insert_message(
            connection,
            workspace_id=ctx["workspace_id"],
            owner_id=ctx["owner_id"],
            thread_id=thread_id,
            external_message_id=f"msg-{uuid4()}",
            now=ctx["now"],
            fetched=True,
        )

    key = str(uuid4())
    first = ctx["client"].post(
        f"/api/v1/personal/gmail/threads/{thread_id}/forget", headers=_headers(ctx["token"], key)
    )
    second = ctx["client"].post(
        f"/api/v1/personal/gmail/threads/{thread_id}/forget", headers=_headers(ctx["token"], key)
    )
    assert first.status_code == 200
    assert second.status_code == 200
    # Compare only the cached business fields, not the full body: `response_
    # contract_middleware` stamps a fresh `request_id`/`correlation_id` onto
    # every response -- including a replayed one -- so those two keys
    # legitimately differ between calls even though the cached payload
    # underneath is identical (matching the established pattern in
    # `test_collaboration_delegations_postgres.py`'s own idempotency-replay
    # test, which compares specific fields rather than the whole body).
    for field in ("id", "thread_id", "status", "requested_at", "completed_at"):
        assert first.json()[field] == second.json()[field]

    with engine.begin() as connection:
        count = connection.execute(
            text(
                "SELECT count(*) FROM deletion_jobs WHERE workspace_id = :workspace_id "
                "AND resource_id = :thread_id"
            ),
            {"workspace_id": ctx["workspace_id"], "thread_id": thread_id},
        ).scalar_one()
    assert count == 1


def test_forget_thread_not_found_is_404(gmail_threads_context: dict) -> None:
    ctx = gmail_threads_context
    resp = ctx["client"].post(
        f"/api/v1/personal/gmail/threads/{uuid4()}/forget",
        headers=_headers(ctx["token"], str(uuid4())),
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "THREAD_NOT_FOUND"


def test_reopening_a_forgotten_thread_refetches_its_content(
    gmail_threads_context: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = gmail_threads_context
    with engine.begin() as connection:
        thread_id = _insert_thread(
            connection,
            workspace_id=ctx["workspace_id"],
            owner_id=ctx["owner_id"],
            account_id=ctx["account_id"],
            subject="Forget then reopen",
            now=ctx["now"],
        )
        external_message_id = f"msg-{uuid4()}"
        message_id = _insert_message(
            connection,
            workspace_id=ctx["workspace_id"],
            owner_id=ctx["owner_id"],
            thread_id=thread_id,
            external_message_id=external_message_id,
            now=ctx["now"],
            fetched=True,
        )

    ctx["client"].post(
        f"/api/v1/personal/gmail/threads/{thread_id}/forget",
        headers=_headers(ctx["token"], str(uuid4())),
    )
    assert _message_row(ctx["workspace_id"], message_id)["body"] is None

    monkeypatch.setattr(
        gmail_threads, "_adapter", GmailAdapter(transport=_gmail_transport(external_message_id))
    )
    resp = ctx["client"].get(f"/api/v1/personal/gmail/threads/{thread_id}")
    assert resp.status_code == 200
    assert resp.json()["messages"][0]["body"] == _PLAIN_TEXT_BODY
    assert _message_row(ctx["workspace_id"], message_id)["body"] is not None


def test_list_threads_returns_newest_first_with_summary_fields(gmail_threads_context: dict) -> None:
    ctx = gmail_threads_context
    older_at = ctx["now"] - timedelta(hours=2)
    newer_at = ctx["now"]
    with engine.begin() as connection:
        older_thread_id = _insert_thread(
            connection,
            workspace_id=ctx["workspace_id"],
            owner_id=ctx["owner_id"],
            account_id=ctx["account_id"],
            subject="Older thread",
            now=ctx["now"],
            last_message_at=older_at,
        )
        _insert_message(
            connection,
            workspace_id=ctx["workspace_id"],
            owner_id=ctx["owner_id"],
            thread_id=older_thread_id,
            external_message_id=f"msg-{uuid4()}",
            now=ctx["now"],
            fetched=False,
            sent_at=older_at,
        )
        newer_thread_id = _insert_thread(
            connection,
            workspace_id=ctx["workspace_id"],
            owner_id=ctx["owner_id"],
            account_id=ctx["account_id"],
            subject="Newer thread",
            now=ctx["now"],
            last_message_at=newer_at,
        )
        _insert_message(
            connection,
            workspace_id=ctx["workspace_id"],
            owner_id=ctx["owner_id"],
            thread_id=newer_thread_id,
            external_message_id=f"msg-{uuid4()}",
            now=ctx["now"],
            fetched=False,
            sent_at=older_at,
            sender=_SENDER,
            direction="inbound",
        )
        _insert_message(
            connection,
            workspace_id=ctx["workspace_id"],
            owner_id=ctx["owner_id"],
            thread_id=newer_thread_id,
            external_message_id=f"msg-{uuid4()}",
            now=ctx["now"],
            fetched=True,
            sent_at=newer_at,
            sender=_OWNER_EMAIL,
            direction="outbound",
        )

    resp = ctx["client"].get("/api/v1/personal/gmail/threads")
    assert resp.status_code == 200
    threads = resp.json()["threads"]
    assert [t["id"] for t in threads] == [str(newer_thread_id), str(older_thread_id)]

    newer = threads[0]
    assert newer["subject"] == "Newer thread"
    assert newer["last_sender"] == _OWNER_EMAIL
    assert newer["last_direction"] == "outbound"
    assert newer["message_count"] == 2
    assert newer["body_cached"] is True

    older = threads[1]
    assert older["subject"] == "Older thread"
    assert older["last_sender"] == _SENDER
    assert older["last_direction"] == "inbound"
    assert older["message_count"] == 1
    assert older["body_cached"] is False


def test_list_threads_respects_limit(gmail_threads_context: dict) -> None:
    ctx = gmail_threads_context
    with engine.begin() as connection:
        for offset in range(3):
            thread_id = _insert_thread(
                connection,
                workspace_id=ctx["workspace_id"],
                owner_id=ctx["owner_id"],
                account_id=ctx["account_id"],
                subject=f"Thread {offset}",
                now=ctx["now"],
                last_message_at=ctx["now"] - timedelta(hours=offset),
            )
            _insert_message(
                connection,
                workspace_id=ctx["workspace_id"],
                owner_id=ctx["owner_id"],
                thread_id=thread_id,
                external_message_id=f"msg-{uuid4()}",
                now=ctx["now"],
                fetched=False,
            )

    resp = ctx["client"].get("/api/v1/personal/gmail/threads", params={"limit": 2})
    assert resp.status_code == 200
    threads = resp.json()["threads"]
    assert len(threads) == 2
    assert threads[0]["subject"] == "Thread 0"
    assert threads[1]["subject"] == "Thread 1"


def test_list_threads_excludes_a_different_owners_thread_in_the_same_workspace(
    gmail_threads_context: dict,
) -> None:
    # Loop 2 round 2 review (PR #130): the existing "only returns caller's
    # own threads" test below only varies workspace_id *and* owner_id
    # together, so it never actually proved same-workspace, different-
    # owner isolation -- the exact gap this module's docstring already
    # (over)claimed was covered under item 13. A peer owner's own thread,
    # in the identical workspace, must still be invisible.
    ctx = gmail_threads_context
    peer_owner_id = uuid4()
    peer_email = "gmail-threads-peer@example.test"
    now = ctx["now"]
    with engine.begin() as connection:
        own_thread_id = _insert_thread(
            connection,
            workspace_id=ctx["workspace_id"],
            owner_id=ctx["owner_id"],
            account_id=ctx["account_id"],
            subject="Mine",
            now=now,
        )
        _insert_message(
            connection,
            workspace_id=ctx["workspace_id"],
            owner_id=ctx["owner_id"],
            thread_id=own_thread_id,
            external_message_id=f"msg-{uuid4()}",
            now=now,
            fetched=False,
        )
        create_identity(
            connection,
            workspace_id=ctx["workspace_id"],
            user_id=peer_owner_id,
            email=peer_email,
            now=now,
        )
        peer_account_id = uuid4()
        connection.execute(
            text(
                """
                INSERT INTO connector_accounts (
                    id, workspace_id, provider, external_account_id, display_name,
                    granted_scopes, encrypted_credentials, status, version,
                    created_by, updated_by, created_at, updated_at, owner_id, visibility
                ) VALUES (
                    :id, :workspace_id, 'gmail', :peer_email, 'Peer account',
                    ARRAY['https://www.googleapis.com/auth/gmail.readonly'], :encrypted,
                    'active', 1, :actor_id, :actor_id, :now, :now, :actor_id, 'workspace'
                )
                """
            ),
            {
                "id": peer_account_id,
                "workspace_id": ctx["workspace_id"],
                "peer_email": peer_email,
                "encrypted": encrypt_credential(
                    _pack_credential("access-peer", "refresh-peer", now + timedelta(hours=1))
                ),
                "actor_id": peer_owner_id,
                "now": now,
            },
        )
        peer_thread_id = _insert_thread(
            connection,
            workspace_id=ctx["workspace_id"],
            owner_id=peer_owner_id,
            account_id=peer_account_id,
            subject="Peer's, not mine",
            now=now,
        )
        _insert_message(
            connection,
            workspace_id=ctx["workspace_id"],
            owner_id=peer_owner_id,
            thread_id=peer_thread_id,
            external_message_id=f"msg-{uuid4()}",
            now=now,
            fetched=False,
        )

    try:
        resp = ctx["client"].get("/api/v1/personal/gmail/threads")
        assert resp.status_code == 200
        thread_ids = [t["id"] for t in resp.json()["threads"]]
        assert thread_ids == [str(own_thread_id)]
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM accounts WHERE email = :email"), {"email": peer_email}
            )


def test_list_threads_only_returns_callers_own_threads(gmail_threads_context: dict) -> None:
    ctx = gmail_threads_context
    other_workspace_id = uuid4()
    other_owner_id = uuid4()
    now = ctx["now"]
    with engine.begin() as connection:
        own_thread_id = _insert_thread(
            connection,
            workspace_id=ctx["workspace_id"],
            owner_id=ctx["owner_id"],
            account_id=ctx["account_id"],
            subject="Mine",
            now=now,
        )
        _insert_message(
            connection,
            workspace_id=ctx["workspace_id"],
            owner_id=ctx["owner_id"],
            thread_id=own_thread_id,
            external_message_id=f"msg-{uuid4()}",
            now=now,
            fetched=False,
        )

        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, 'Other Workspace', 'UTC', :now)"
            ),
            {"id": other_workspace_id, "now": now},
        )
        create_identity(
            connection,
            workspace_id=other_workspace_id,
            user_id=other_owner_id,
            email="other-owner@example.test",
            now=now,
        )
        other_account_id = uuid4()
        connection.execute(
            text(
                """
                INSERT INTO connector_accounts (
                    id, workspace_id, provider, external_account_id, display_name,
                    granted_scopes, encrypted_credentials, status, version,
                    created_by, updated_by, created_at, updated_at, owner_id, visibility
                ) VALUES (
                    :id, :workspace_id, 'gmail', 'other-owner@example.test', 'Other account',
                    ARRAY['https://www.googleapis.com/auth/gmail.readonly'], :encrypted,
                    'active', 1, :actor_id, :actor_id, :now, :now, :actor_id, 'workspace'
                )
                """
            ),
            {
                "id": other_account_id,
                "workspace_id": other_workspace_id,
                "encrypted": encrypt_credential(
                    _pack_credential("access-2", "refresh-2", now + timedelta(hours=1))
                ),
                "actor_id": other_owner_id,
                "now": now,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO personal_domains (
                    id, workspace_id, owner_id, domain_key, classification, enabled,
                    enabled_at, created_by, updated_by, created_at, updated_at, version
                ) VALUES (
                    :id, :workspace_id, :owner_id, 'email', 'high_stakes', true,
                    :now, :owner_id, :owner_id, :now, :now, 1
                )
                """
            ),
            {
                "id": uuid4(),
                "workspace_id": other_workspace_id,
                "owner_id": other_owner_id,
                "now": now,
            },
        )
        other_thread_id = _insert_thread(
            connection,
            workspace_id=other_workspace_id,
            owner_id=other_owner_id,
            account_id=other_account_id,
            subject="Not yours",
            now=now,
        )
        _insert_message(
            connection,
            workspace_id=other_workspace_id,
            owner_id=other_owner_id,
            thread_id=other_thread_id,
            external_message_id=f"msg-{uuid4()}",
            now=now,
            fetched=False,
        )

    try:
        resp = ctx["client"].get("/api/v1/personal/gmail/threads")
        assert resp.status_code == 200
        thread_ids = [t["id"] for t in resp.json()["threads"]]
        assert thread_ids == [str(own_thread_id)]
    finally:
        _cleanup_workspace(other_workspace_id, email="other-owner@example.test")


def test_list_threads_without_active_consent_is_rejected(gmail_threads_context: dict) -> None:
    ctx = gmail_threads_context
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE domain_consents SET revoked_at = :now "
                "WHERE workspace_id = :workspace_id AND owner_id = :owner_id "
                "AND domain_key = 'email'"
            ),
            {"now": ctx["now"], "workspace_id": ctx["workspace_id"], "owner_id": ctx["owner_id"]},
        )

    resp = ctx["client"].get("/api/v1/personal/gmail/threads")
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "EMAIL_CONSENT_NOT_ACTIVE"
