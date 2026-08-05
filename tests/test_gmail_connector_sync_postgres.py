"""Phase 10 Gmail Connector Task 2: backfill, incremental sync and entity
linking (`docs/superpowers/plans/2026-08-04-phase-10-gmail-connector.md`
Task 2, `backend/ecc/domains/personal/gmail_adapter.py`).

Covers, per this task's own scope:

1. `backfill` syncs messages within the requested window (`since` bounds
   the Gmail `q=after:...` search), writing `email_threads`/`email_
   messages` and resolving sender/recipients into `pkos_nodes` via
   `entity_aliases` (a person entity is created once, then reused on a
   second message from the same participant).
2. Re-invoking `backfill` re-verifies `domain_consents` is still active,
   both before starting and mid-call -- a consent revoked between two
   messages in the same call halts the call (`status="partial"`), and a
   consent that was never active at all raises outright.
3. `incremental_sync` resumes from a `historyId` cursor via Gmail's
   `history.list`, defers to a fresh `backfill` when `cursor is None`
   (the Protocol's own contract) and when Gmail reports the cursor has
   expired (`history.list` returning 404).
4. Rate-limit handling (a Gmail `429`, and a `403` carrying a
   `rateLimitExceeded`-family `reason`) degrades to `status="partial"`
   with the sync's own accumulated progress preserved, distinguishing
   that from an ordinary `403` (a real, non-transient rejection this
   adapter must not silently retry-then-swallow).
5. Idempotent re-sync: re-running `backfill` over an overlapping window
   does not duplicate `email_threads`/`email_messages` rows (`ON CONFLICT
   DO NOTHING`/`DO UPDATE`), and does not re-run entity resolution for an
   already-seen message's participants.
6. `ResourceType` widened to accept `"message"`; every other resource
   type still zero-item-succeeds for a `gmail` account, matching every
   other adapter's "not-yet-implemented resource type" contract.
7. An oversized `From`/`To` address (`_MAX_EMAIL_ADDRESS_LENGTH`) is
   dropped like any other unparseable header, not left to crash the sync
   call on `email_messages.sender`/`.recipients`' own `VARCHAR(320)`
   bound -- found by round 1 review's security/correctness lens.
8. `_MAX_MESSAGES_PER_CALL` hit mid-page (not merely mid-multi-page-walk)
   correctly reports `status="partial"`, not a false `"succeeded"` that
   would silently strand the page's own unfetched messages -- found by
   round 1 review's architecture lens; both `_sync_messages`/`_sync_
   history`'s structurally identical loops are covered.
9. `messages.list`/`history.list` pagination genuinely walks a real
   multi-page response (every test above 8 uses a single page) -- round 1
   review coverage gap, closed.
10. `_process_message`'s own guard clauses (missing `id`/`threadId`/
    `From` header/recipients, non-numeric `internalDate`) each skip that
    one message gracefully rather than raising, called directly rather
    than only exercised incidentally -- round 1 review coverage gap,
    closed.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from identity_fixtures import create_identity
from sqlalchemy import text

from ecc.config import get_settings
from ecc.database import engine
from ecc.domains.engineering.connectors import ConnectorAccountContext
from ecc.domains.engineering.crypto import encrypt_credential
from ecc.domains.personal.gmail_adapter import GmailAdapter, _pack_credential

settings = get_settings()
pytestmark = pytest.mark.skipif(
    not settings.database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)

_OWNER_EMAIL = "owner@example.test"


def _json_response(body: Any, *, status_code: int = 200) -> httpx.Response:
    return httpx.Response(status_code=status_code, json=body)


def _message_body(
    *,
    message_id: str,
    thread_id: str,
    from_addr: str,
    to_addrs: list[str],
    internal_date_ms: int,
    subject: str | None = "A subject",
    label_ids: list[str] | None = None,
    history_id: int = 1000,
) -> dict[str, Any]:
    headers = [{"name": "From", "value": from_addr}, {"name": "To", "value": ", ".join(to_addrs)}]
    if subject is not None:
        headers.append({"name": "Subject", "value": subject})
    return {
        "id": message_id,
        "threadId": thread_id,
        "labelIds": label_ids if label_ids is not None else ["INBOX"],
        "internalDate": str(internal_date_ms),
        "historyId": history_id,
        "payload": {"headers": headers},
    }


@pytest.fixture
def seeded_gmail_account() -> Iterator[tuple[ConnectorAccountContext, UUID]]:
    """A `workspaces`/`users`/`connector_accounts`(`provider='gmail'`)/
    `personal_domains`/`domain_consents` row set -- required for any
    `backfill`/`incremental_sync` call, both of which genuinely write to
    `email_threads`/`email_messages`/`pkos_nodes`/`pkos_evidence`/
    `entity_aliases` (none of this is mocked) and read `connector_
    accounts.owner_id`/`domain_consents` directly. Migration `0063`'s
    `_NEW_OWNER_FROM_CREATED_BY` trigger backfills `connector_accounts.
    owner_id` from `created_by` when not explicitly set below, matching
    `gmail_oauth.py`'s own real INSERT (which sets both to the same
    value) -- not relied on as a coincidence, confirmed by `test_owner_
    id_is_populated_from_created_by` below.
    """
    workspace_id = uuid4()
    owner_id = uuid4()
    account_id = uuid4()
    domain_id = uuid4()
    now = datetime.now(UTC)
    credential = _pack_credential("access-1", "refresh-1", now + timedelta(hours=1))
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, 'Gmail Sync Unit Test', 'UTC', :now)"
            ),
            {"id": workspace_id, "now": now},
        )
        create_identity(
            connection, workspace_id=workspace_id, user_id=owner_id, email=_OWNER_EMAIL, now=now
        )
        connection.execute(
            text(
                """
                INSERT INTO connector_accounts (
                    id, workspace_id, provider, external_account_id, display_name,
                    granted_scopes, encrypted_credentials, status, version,
                    created_by, updated_by, created_at, updated_at
                ) VALUES (
                    :id, :workspace_id, 'gmail', :external_account_id, 'Gmail sync test',
                    ARRAY['https://www.googleapis.com/auth/gmail.metadata'], :encrypted,
                    'active', 1, :actor_id, :actor_id, :now, :now
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
            {"id": domain_id, "workspace_id": workspace_id, "owner_id": owner_id, "now": now},
        )
        connection.execute(
            text(
                "INSERT INTO domain_consents (id, workspace_id, owner_id, domain_key, "
                "granted_at, created_at) "
                "VALUES (:id, :workspace_id, :owner_id, 'email', :now, :now)"
            ),
            {"id": uuid4(), "workspace_id": workspace_id, "owner_id": owner_id, "now": now},
        )
    try:
        yield (
            ConnectorAccountContext(
                workspace_id=workspace_id,
                connector_account_id=account_id,
                external_account_id=_OWNER_EMAIL,
                credential=credential,
            ),
            owner_id,
        )
    finally:
        with engine.begin() as connection:
            for table in (
                "entity_aliases",
                "pkos_evidence",
                "pkos_nodes",
                "email_messages",
                "email_threads",
                "domain_consents",
                "personal_domains",
                "connector_accounts",
                "users",
            ):
                connection.execute(
                    text(f"DELETE FROM {table} WHERE workspace_id = :workspace_id"),  # noqa: S608
                    {"workspace_id": workspace_id},
                )
            connection.execute(
                text("DELETE FROM workspaces WHERE id = :workspace_id"),
                {"workspace_id": workspace_id},
            )
            connection.execute(
                text("DELETE FROM accounts WHERE email = :email"), {"email": _OWNER_EMAIL}
            )


def _revoke_consent(workspace_id: UUID, owner_id: UUID) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE domain_consents SET revoked_at = :now "
                "WHERE workspace_id = :workspace_id AND owner_id = :owner_id "
                "AND domain_key = 'email'"
            ),
            {"workspace_id": workspace_id, "owner_id": owner_id, "now": datetime.now(UTC)},
        )


def _threads_and_messages(workspace_id: UUID) -> list[dict[str, Any]]:
    with engine.begin() as connection:
        rows = (
            connection.execute(
                text(
                    "SELECT m.external_message_id, m.sender, m.recipients, m.direction, "
                    "t.external_thread_id, t.subject "
                    "FROM email_messages m JOIN email_threads t ON t.id = m.thread_id "
                    "WHERE m.workspace_id = :workspace_id ORDER BY m.sent_at"
                ),
                {"workspace_id": workspace_id},
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


def _resolved_person(workspace_id: UUID, email: str) -> dict[str, Any] | None:
    with engine.begin() as connection:
        row = (
            connection.execute(
                text(
                    "SELECT n.id, n.canonical_name FROM entity_aliases a "
                    "JOIN pkos_nodes n ON n.workspace_id = a.workspace_id AND n.id = a.entity_id "
                    "WHERE a.workspace_id = :workspace_id AND a.alias_type = 'email' "
                    "AND a.normalized_value = :normalized_value"
                ),
                {"workspace_id": workspace_id, "normalized_value": email.strip().casefold()},
            )
            .mappings()
            .one_or_none()
        )
        return dict(row) if row is not None else None


# --- ResourceType widening / non-message no-op ------------------------------


def test_backfill_still_no_ops_for_a_non_message_resource_type(
    seeded_gmail_account: tuple[ConnectorAccountContext, UUID],
) -> None:
    context, _owner_id = seeded_gmail_account
    adapter = GmailAdapter()
    outcome = adapter.backfill(context, "thread")
    assert outcome == adapter.backfill(context, "thread")
    assert outcome.status == "succeeded"
    assert outcome.items_processed == 0
    assert outcome.next_cursor is None


def test_incremental_sync_still_no_ops_for_a_non_message_resource_type(
    seeded_gmail_account: tuple[ConnectorAccountContext, UUID],
) -> None:
    context, _owner_id = seeded_gmail_account
    adapter = GmailAdapter()
    outcome = adapter.incremental_sync(context, "thread", "some-cursor")
    assert outcome.status == "succeeded"
    assert outcome.items_processed == 0
    assert outcome.next_cursor == "some-cursor"


# --- backfill: happy path, window, entity resolution ------------------------


def test_backfill_writes_messages_and_resolves_participants(
    seeded_gmail_account: tuple[ConnectorAccountContext, UUID],
) -> None:
    context, owner_id = seeded_gmail_account
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    bodies = {
        "msg-1": _message_body(
            message_id="msg-1",
            thread_id="thread-1",
            from_addr="Alice Sender <alice@example.test>",
            to_addrs=["owner@example.test"],
            internal_date_ms=now_ms,
            history_id=101,
        ),
        "msg-2": _message_body(
            message_id="msg-2",
            thread_id="thread-1",
            from_addr="Alice Sender <alice@example.test>",
            to_addrs=["owner@example.test", "Bob Recipient <bob@example.test>"],
            internal_date_ms=now_ms + 1000,
            history_id=102,
        ),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/gmail/v1/users/me/messages":
            return _json_response({"messages": [{"id": "msg-1"}, {"id": "msg-2"}]})
        if request.url.path.startswith("/gmail/v1/users/me/messages/"):
            message_id = request.url.path.rsplit("/", 1)[-1]
            return _json_response(bodies[message_id])
        raise AssertionError(f"unexpected request to {request.url}")

    adapter = GmailAdapter(transport=httpx.MockTransport(handler))
    outcome = adapter.backfill(context, "message", since=datetime.now(UTC) - timedelta(days=1))

    assert outcome.status == "succeeded"
    assert outcome.items_processed == 2
    assert outcome.next_cursor == "102"

    rows = _threads_and_messages(context.workspace_id)
    assert len(rows) == 2
    assert {row["external_message_id"] for row in rows} == {"msg-1", "msg-2"}
    assert all(row["external_thread_id"] == "thread-1" for row in rows)
    assert rows[0]["sender"] == "alice@example.test"
    assert set(rows[1]["recipients"]) == {"owner@example.test", "bob@example.test"}

    alice = _resolved_person(context.workspace_id, "alice@example.test")
    bob = _resolved_person(context.workspace_id, "bob@example.test")
    owner = _resolved_person(context.workspace_id, "owner@example.test")
    assert alice is not None and alice["canonical_name"] == "Alice Sender"
    assert bob is not None and bob["canonical_name"] == "Bob Recipient"
    assert owner is not None
    # The second message's `alice@example.test` reuses the entity resolved
    # by the first, rather than creating a duplicate `pkos_nodes` row.
    with engine.begin() as connection:
        count = connection.execute(
            text(
                "SELECT count(*) FROM entity_aliases WHERE workspace_id = :workspace_id "
                "AND normalized_value = 'alice@example.test'"
            ),
            {"workspace_id": context.workspace_id},
        ).scalar_one()
    assert count == 1


def test_backfill_uses_since_as_the_gmail_query_lower_bound(
    seeded_gmail_account: tuple[ConnectorAccountContext, UUID],
) -> None:
    context, _owner_id = seeded_gmail_account
    since = datetime(2026, 1, 1, tzinfo=UTC)
    captured_query: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/gmail/v1/users/me/messages":
            captured_query["q"] = request.url.params.get("q", "")
            return _json_response({"messages": []})
        if request.url.path == "/gmail/v1/users/me/profile":
            return _json_response({"historyId": 1})
        raise AssertionError(f"unexpected request to {request.url}")

    adapter = GmailAdapter(transport=httpx.MockTransport(handler))
    outcome = adapter.backfill(context, "message", since=since)
    assert outcome.status == "succeeded"
    assert captured_query["q"] == f"after:{int(since.timestamp())}"


def test_backfill_defaults_to_the_30_day_window_when_since_is_none(
    seeded_gmail_account: tuple[ConnectorAccountContext, UUID],
) -> None:
    context, _owner_id = seeded_gmail_account
    captured_query: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/gmail/v1/users/me/messages":
            captured_query["q"] = request.url.params.get("q", "")
            return _json_response({"messages": []})
        if request.url.path == "/gmail/v1/users/me/profile":
            return _json_response({"historyId": 1})
        raise AssertionError(f"unexpected request to {request.url}")

    adapter = GmailAdapter(transport=httpx.MockTransport(handler))
    before = int((datetime.now(UTC) - timedelta(days=30)).timestamp())
    outcome = adapter.backfill(context, "message", since=None)
    after = int((datetime.now(UTC) - timedelta(days=30)).timestamp())
    assert outcome.status == "succeeded"
    observed = int(captured_query["q"].removeprefix("after:"))
    # Small window: the "30 days ago" reference point is computed twice
    # (once by this test, once inside `backfill`), a few seconds apart.
    assert before <= observed <= after


def test_backfill_empty_window_seeds_next_cursor_from_profile(
    seeded_gmail_account: tuple[ConnectorAccountContext, UUID],
) -> None:
    """Zero messages in the requested window means no `historyId` was ever
    observed from a message resource -- `backfill` falls back to `users.
    getProfile` (the only other Gmail endpoint reporting the mailbox's
    current `historyId`) so `incremental_sync` still has a real cursor to
    resume from, rather than permanently deferring back to `backfill`.
    """
    context, _owner_id = seeded_gmail_account

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/gmail/v1/users/me/messages":
            return _json_response({"messages": []})
        if request.url.path == "/gmail/v1/users/me/profile":
            return _json_response({"emailAddress": _OWNER_EMAIL, "historyId": 555})
        raise AssertionError(f"unexpected request to {request.url}")

    adapter = GmailAdapter(transport=httpx.MockTransport(handler))
    outcome = adapter.backfill(context, "message", since=datetime.now(UTC) - timedelta(days=1))
    assert outcome.status == "succeeded"
    assert outcome.items_processed == 0
    assert outcome.next_cursor == "555"


# --- consent re-verification -------------------------------------------------


def test_backfill_raises_when_consent_was_never_active(
    seeded_gmail_account: tuple[ConnectorAccountContext, UUID],
) -> None:
    context, owner_id = seeded_gmail_account
    _revoke_consent(context.workspace_id, owner_id)

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"no Gmail call should happen: {request.url}")

    adapter = GmailAdapter(transport=httpx.MockTransport(handler))
    with pytest.raises(RuntimeError, match="consent is not active"):
        adapter.backfill(context, "message", since=datetime.now(UTC) - timedelta(days=1))


def test_backfill_halts_when_consent_is_revoked_mid_window(
    seeded_gmail_account: tuple[ConnectorAccountContext, UUID],
) -> None:
    """Plan Task 2: "a revoked-mid-window consent halts the call." Revokes
    consent as a side effect of fetching the *first* message's metadata --
    the second message must never be fetched or written.
    """
    context, owner_id = seeded_gmail_account
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    first_body = _message_body(
        message_id="msg-1",
        thread_id="thread-1",
        from_addr="alice@example.test",
        to_addrs=["owner@example.test"],
        internal_date_ms=now_ms,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/gmail/v1/users/me/messages":
            return _json_response({"messages": [{"id": "msg-1"}, {"id": "msg-2"}]})
        if request.url.path == "/gmail/v1/users/me/messages/msg-1":
            return _json_response(first_body)
        if request.url.path == "/gmail/v1/users/me/messages/msg-2":
            raise AssertionError("msg-2 must not be fetched after consent is revoked")
        raise AssertionError(f"unexpected request to {request.url}")

    adapter = GmailAdapter(transport=httpx.MockTransport(handler))
    original_get = adapter._request_with_rate_limit_retry

    def instrumented(method: str, path: str, **kwargs: Any) -> httpx.Response | None:
        response = original_get(method, path, **kwargs)
        if path == "/gmail/v1/users/me/messages/msg-1":
            _revoke_consent(context.workspace_id, owner_id)
        return response

    adapter._request_with_rate_limit_retry = instrumented  # type: ignore[method-assign]

    outcome = adapter.backfill(context, "message", since=datetime.now(UTC) - timedelta(days=1))
    assert outcome.status == "partial"
    assert outcome.error_summary is not None and "revoked" in outcome.error_summary
    assert outcome.items_processed == 1

    rows = _threads_and_messages(context.workspace_id)
    assert len(rows) == 1
    assert rows[0]["external_message_id"] == "msg-1"


# --- rate limiting ------------------------------------------------------------


def test_backfill_degrades_to_partial_when_rate_limited_beyond_the_bound(
    seeded_gmail_account: tuple[ConnectorAccountContext, UUID],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/gmail/v1/users/me/messages":
            return httpx.Response(
                429, json={"error": {"errors": [{"reason": "rateLimitExceeded"}]}}
            )
        raise AssertionError(f"unexpected request to {request.url}")

    context, _owner_id = seeded_gmail_account
    adapter = GmailAdapter(transport=httpx.MockTransport(handler), sleep=lambda _seconds: None)
    outcome = adapter.backfill(context, "message", since=datetime.now(UTC) - timedelta(days=1))
    assert outcome.status == "partial"
    assert outcome.error_summary is not None and "rate limit" in outcome.error_summary
    assert outcome.items_processed == 0


def test_backfill_succeeds_after_one_rate_limited_retry(
    seeded_gmail_account: tuple[ConnectorAccountContext, UUID],
) -> None:
    context, _owner_id = seeded_gmail_account
    calls = {"messages_list": 0}
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    body = _message_body(
        message_id="msg-1",
        thread_id="thread-1",
        from_addr="alice@example.test",
        to_addrs=["owner@example.test"],
        internal_date_ms=now_ms,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/gmail/v1/users/me/messages":
            calls["messages_list"] += 1
            if calls["messages_list"] == 1:
                return httpx.Response(
                    429, headers={"Retry-After": "0"}, json={"error": {"errors": []}}
                )
            return _json_response({"messages": [{"id": "msg-1"}]})
        if request.url.path == "/gmail/v1/users/me/messages/msg-1":
            return _json_response(body)
        raise AssertionError(f"unexpected request to {request.url}")

    adapter = GmailAdapter(transport=httpx.MockTransport(handler), sleep=lambda _seconds: None)
    outcome = adapter.backfill(context, "message", since=datetime.now(UTC) - timedelta(days=1))
    assert outcome.status == "succeeded"
    assert outcome.items_processed == 1


def test_a_genuine_403_is_not_treated_as_rate_limiting(
    seeded_gmail_account: tuple[ConnectorAccountContext, UUID],
) -> None:
    """A `403` whose `reason` isn't one of the rate-limit family (here,
    `insufficientPermissions`) must raise, not silently degrade to
    `partial` -- retrying can never fix it, and swallowing it would mask a
    real, non-transient failure as if it will resolve itself later.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/gmail/v1/users/me/messages":
            return httpx.Response(
                403, json={"error": {"errors": [{"reason": "insufficientPermissions"}]}}
            )
        raise AssertionError(f"unexpected request to {request.url}")

    context, _owner_id = seeded_gmail_account
    adapter = GmailAdapter(transport=httpx.MockTransport(handler))
    with pytest.raises(RuntimeError, match="status 403"):
        adapter.backfill(context, "message", since=datetime.now(UTC) - timedelta(days=1))


# --- idempotent re-sync --------------------------------------------------------


def test_backfill_rerun_over_an_overlapping_window_does_not_duplicate_rows(
    seeded_gmail_account: tuple[ConnectorAccountContext, UUID],
) -> None:
    context, _owner_id = seeded_gmail_account
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    body = _message_body(
        message_id="msg-1",
        thread_id="thread-1",
        from_addr="alice@example.test",
        to_addrs=["owner@example.test"],
        internal_date_ms=now_ms,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/gmail/v1/users/me/messages":
            return _json_response({"messages": [{"id": "msg-1"}]})
        if request.url.path == "/gmail/v1/users/me/messages/msg-1":
            return _json_response(body)
        raise AssertionError(f"unexpected request to {request.url}")

    adapter = GmailAdapter(transport=httpx.MockTransport(handler))
    since = datetime.now(UTC) - timedelta(days=1)
    first = adapter.backfill(context, "message", since=since)
    second = adapter.backfill(context, "message", since=since)
    assert first.status == "succeeded"
    assert second.status == "succeeded"
    assert second.items_processed == 1

    rows = _threads_and_messages(context.workspace_id)
    assert len(rows) == 1
    with engine.begin() as connection:
        alias_count = connection.execute(
            text(
                "SELECT count(*) FROM entity_aliases WHERE workspace_id = :workspace_id "
                "AND normalized_value = 'alice@example.test'"
            ),
            {"workspace_id": context.workspace_id},
        ).scalar_one()
    assert alias_count == 1


# --- incremental_sync ----------------------------------------------------------


def test_incremental_sync_with_none_cursor_defers_to_backfill(
    seeded_gmail_account: tuple[ConnectorAccountContext, UUID],
) -> None:
    context, _owner_id = seeded_gmail_account
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/gmail/v1/users/me/messages":
            return _json_response({"messages": []})
        if request.url.path == "/gmail/v1/users/me/profile":
            return _json_response({"historyId": 42})
        raise AssertionError(f"unexpected request to {request.url}")

    adapter = GmailAdapter(transport=httpx.MockTransport(handler))
    outcome = adapter.incremental_sync(context, "message", None)
    assert outcome.status == "succeeded"
    assert "/gmail/v1/users/me/messages" in calls
    assert "/gmail/v1/users/me/history" not in calls


def test_incremental_sync_resumes_from_history_cursor(
    seeded_gmail_account: tuple[ConnectorAccountContext, UUID],
) -> None:
    context, _owner_id = seeded_gmail_account
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    body = _message_body(
        message_id="msg-9",
        thread_id="thread-9",
        from_addr="carol@example.test",
        to_addrs=["owner@example.test"],
        internal_date_ms=now_ms,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/gmail/v1/users/me/history":
            assert request.url.params.get("startHistoryId") == "100"
            return _json_response(
                {
                    "history": [{"messagesAdded": [{"message": {"id": "msg-9"}}]}],
                    "historyId": 150,
                }
            )
        if request.url.path == "/gmail/v1/users/me/messages/msg-9":
            return _json_response(body)
        raise AssertionError(f"unexpected request to {request.url}")

    adapter = GmailAdapter(transport=httpx.MockTransport(handler))
    outcome = adapter.incremental_sync(context, "message", "100")
    assert outcome.status == "succeeded"
    assert outcome.items_processed == 1
    assert outcome.next_cursor == "150"

    rows = _threads_and_messages(context.workspace_id)
    assert len(rows) == 1
    assert rows[0]["external_message_id"] == "msg-9"


def test_incremental_sync_falls_back_to_backfill_when_history_id_expired(
    seeded_gmail_account: tuple[ConnectorAccountContext, UUID],
) -> None:
    """Gmail retains `history` for a rolling window, not indefinitely -- a
    `startHistoryId` older than that window 404s, and the only correct
    recovery is a fresh `backfill` (disclosed design decision, see
    `_sync_history`'s own docstring)."""
    context, _owner_id = seeded_gmail_account
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/gmail/v1/users/me/history":
            return httpx.Response(404, json={"error": {"errors": [{"reason": "notFound"}]}})
        if request.url.path == "/gmail/v1/users/me/messages":
            return _json_response({"messages": []})
        if request.url.path == "/gmail/v1/users/me/profile":
            return _json_response({"historyId": 1})
        raise AssertionError(f"unexpected request to {request.url}")

    adapter = GmailAdapter(transport=httpx.MockTransport(handler))
    outcome = adapter.incremental_sync(context, "message", "999999")
    assert outcome.status == "succeeded"
    assert "/gmail/v1/users/me/messages" in calls


def test_incremental_sync_raises_when_consent_was_never_active(
    seeded_gmail_account: tuple[ConnectorAccountContext, UUID],
) -> None:
    context, owner_id = seeded_gmail_account
    _revoke_consent(context.workspace_id, owner_id)

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"no Gmail call should happen: {request.url}")

    adapter = GmailAdapter(transport=httpx.MockTransport(handler))
    with pytest.raises(RuntimeError, match="consent is not active"):
        adapter.incremental_sync(context, "message", "100")


# --- owner_id trigger precondition -------------------------------------------


def test_owner_id_is_populated_from_created_by(
    seeded_gmail_account: tuple[ConnectorAccountContext, UUID],
) -> None:
    """`seeded_gmail_account`'s own `connector_accounts` INSERT above does
    not explicitly set `owner_id` -- this asserts migration `0063`'s
    trigger actually backfills it from `created_by` in this sandbox's real
    Postgres, rather than every other test in this file silently depending
    on that being true without ever checking it.
    """
    context, owner_id = seeded_gmail_account
    with engine.begin() as connection:
        row = connection.execute(
            text("SELECT owner_id FROM connector_accounts WHERE id = :id"),
            {"id": context.connector_account_id},
        ).one()
    assert row[0] == owner_id


# --- malformed message shape --------------------------------------------------


def test_nul_byte_in_from_address_is_skipped_not_a_crash(
    seeded_gmail_account: tuple[ConnectorAccountContext, UUID],
) -> None:
    """A JSON-escaped `\\u0000` in Gmail's own response body decodes to a
    real NUL character in the parsed `From` address -- Postgres `text`/
    `varchar` columns can never store `0x00` (a distinct constraint from
    `_MAX_EMAIL_ADDRESS_LENGTH`'s column-width check above), so this
    previously reached the `email_messages` INSERT uncaught (`psycopg.
    DataError`), crashing the whole sync call the same way an over-length
    address did -- found by the same review pass. Must degrade to a
    skipped message, not a crash.
    """
    context, _owner_id = seeded_gmail_account
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    body = _message_body(
        message_id="msg-nul",
        thread_id="thread-nul",
        from_addr="weird\x00addr@example.test",
        to_addrs=[_OWNER_EMAIL],
        internal_date_ms=now_ms,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/gmail/v1/users/me/messages":
            return _json_response({"messages": [{"id": "msg-nul"}]})
        if request.url.path.startswith("/gmail/v1/users/me/messages/"):
            return _json_response(body)
        if request.url.path == "/gmail/v1/users/me/profile":
            return _json_response({"historyId": 1})
        raise AssertionError(f"unexpected request to {request.url}")

    adapter = GmailAdapter(transport=httpx.MockTransport(handler))
    outcome = adapter.backfill(context, "message", since=datetime.now(UTC) - timedelta(days=1))
    assert outcome.status == "succeeded"
    assert outcome.items_processed == 1
    assert _threads_and_messages(context.workspace_id) == []


def test_nul_byte_in_from_display_name_is_skipped_not_a_crash(
    seeded_gmail_account: tuple[ConnectorAccountContext, UUID],
) -> None:
    """Same NUL-byte hazard as the address case above, but in the display-
    name half of the header -- `parseaddr` keeps it separate from the
    address, and it flows to `pkos_nodes.canonical_name` (also `text`)
    via `_resolve_or_create_person`, not `email_messages.sender`. A valid
    address alongside a NUL-carrying display name must still be treated
    as unparseable as a whole (matching `_parse_address`'s own "can't
    make sense of it" contract), not silently truncate/strip just the
    display name and resolve a person from the rest.
    """
    context, _owner_id = seeded_gmail_account
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    body = _message_body(
        message_id="msg-nul-name",
        thread_id="thread-nul-name",
        from_addr="Weird\x00Name <alice@example.test>",
        to_addrs=[_OWNER_EMAIL],
        internal_date_ms=now_ms,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/gmail/v1/users/me/messages":
            return _json_response({"messages": [{"id": "msg-nul-name"}]})
        if request.url.path.startswith("/gmail/v1/users/me/messages/"):
            return _json_response(body)
        if request.url.path == "/gmail/v1/users/me/profile":
            return _json_response({"historyId": 1})
        raise AssertionError(f"unexpected request to {request.url}")

    adapter = GmailAdapter(transport=httpx.MockTransport(handler))
    outcome = adapter.backfill(context, "message", since=datetime.now(UTC) - timedelta(days=1))
    assert outcome.status == "succeeded"
    assert outcome.items_processed == 1
    assert _threads_and_messages(context.workspace_id) == []
    assert _resolved_person(context.workspace_id, "alice@example.test") is None


def test_nul_byte_in_subject_is_dropped_but_message_still_syncs(
    seeded_gmail_account: tuple[ConnectorAccountContext, UUID],
) -> None:
    """`email_threads.subject` is nullable, so a NUL-carrying `Subject`
    header only drops that one field rather than the whole message --
    unlike a NUL in the sender/recipient address, nothing else about the
    message depends on it.
    """
    context, _owner_id = seeded_gmail_account
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    body = _message_body(
        message_id="msg-nul-subject",
        thread_id="thread-nul-subject",
        from_addr="alice@example.test",
        to_addrs=[_OWNER_EMAIL],
        internal_date_ms=now_ms,
        subject="Weird\x00Subject",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/gmail/v1/users/me/messages":
            return _json_response({"messages": [{"id": "msg-nul-subject"}]})
        if request.url.path.startswith("/gmail/v1/users/me/messages/"):
            return _json_response(body)
        raise AssertionError(f"unexpected request to {request.url}")

    adapter = GmailAdapter(transport=httpx.MockTransport(handler))
    outcome = adapter.backfill(context, "message", since=datetime.now(UTC) - timedelta(days=1))
    assert outcome.status == "succeeded"
    assert outcome.items_processed == 1

    rows = _threads_and_messages(context.workspace_id)
    assert len(rows) == 1
    assert rows[0]["subject"] is None


def test_oversized_from_address_is_skipped_not_a_crash(
    seeded_gmail_account: tuple[ConnectorAccountContext, UUID],
) -> None:
    """`email_messages.sender`/`.recipients` are `VARCHAR(320)` (migration
    `0069`); `email.utils.parseaddr` has no length limit of its own, so a
    `From` header whose address portion exceeds that column width
    previously reached Postgres uncaught (`StringDataRightTruncation`),
    aborting the entire sync call instead of skipping just this one
    malformed message -- found by review. A real Gmail sender fully
    controls their own `From` header value; this must degrade the same
    way any other malformed message already does (skipped, contributing
    to `items_processed` but writing nothing -- see `_process_message`'s
    own docstring), not crash the call.
    """
    context, _owner_id = seeded_gmail_account
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    long_addr = "x" * 400 + "@example.test"
    body = _message_body(
        message_id="msg-oversized",
        thread_id="thread-oversized",
        from_addr=f"Attacker <{long_addr}>",
        to_addrs=[_OWNER_EMAIL],
        internal_date_ms=now_ms,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/gmail/v1/users/me/messages":
            return _json_response({"messages": [{"id": "msg-oversized"}]})
        if request.url.path.startswith("/gmail/v1/users/me/messages/"):
            return _json_response(body)
        if request.url.path == "/gmail/v1/users/me/profile":
            return _json_response({"historyId": 1})
        raise AssertionError(f"unexpected request to {request.url}")

    adapter = GmailAdapter(transport=httpx.MockTransport(handler))
    outcome = adapter.backfill(context, "message", since=datetime.now(UTC) - timedelta(days=1))
    assert outcome.status == "succeeded"
    assert outcome.items_processed == 1
    assert _threads_and_messages(context.workspace_id) == []


def test_oversized_recipient_address_is_dropped_but_message_still_syncs(
    seeded_gmail_account: tuple[ConnectorAccountContext, UUID],
) -> None:
    """Same bound as the `From` case above, but on one `To` address among
    several -- only the oversized recipient is dropped (matching how any
    other individually-unparseable recipient is already handled), the
    message itself still syncs with its remaining, well-formed recipients.
    """
    context, _owner_id = seeded_gmail_account
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    long_addr = "y" * 400 + "@example.test"
    body = _message_body(
        message_id="msg-partial-recipients",
        thread_id="thread-partial-recipients",
        from_addr="alice@example.test",
        to_addrs=[_OWNER_EMAIL, f"Oversized <{long_addr}>"],
        internal_date_ms=now_ms,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/gmail/v1/users/me/messages":
            return _json_response({"messages": [{"id": "msg-partial-recipients"}]})
        if request.url.path.startswith("/gmail/v1/users/me/messages/"):
            return _json_response(body)
        raise AssertionError(f"unexpected request to {request.url}")

    adapter = GmailAdapter(transport=httpx.MockTransport(handler))
    outcome = adapter.backfill(context, "message", since=datetime.now(UTC) - timedelta(days=1))
    assert outcome.status == "succeeded"
    assert outcome.items_processed == 1

    rows = _threads_and_messages(context.workspace_id)
    assert len(rows) == 1
    assert rows[0]["recipients"] == [_OWNER_EMAIL]


# --- per-call message budget hit mid-page -------------------------------------


def test_backfill_reports_partial_not_succeeded_when_budget_exhausted_mid_page(
    seeded_gmail_account: tuple[ConnectorAccountContext, UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 1 review: `_MAX_MESSAGES_PER_CALL` bounds total `messages.get`
    calls across every page fetched in one call -- but the per-page `for`
    loop that enforces it previously fell straight through to the
    `nextPageToken` check once the budget ran out, rather than returning
    early. When the bound-hitting page also happened to be Gmail's own
    *last* page (no `nextPageToken`), that meant this method reported
    `status="succeeded"` -- and handed `incremental_sync` a `next_cursor`
    implying nothing older remained -- despite some of that very page's
    own `messages` never having been fetched or written at all: silent
    data loss reported as success. This monkeypatches `_MAX_MESSAGES_PER_
    CALL` down to 3 against a single five-message page (no `nextPageToken`
    at all) to exercise exactly that shape without needing 200+ messages
    in a test fixture. Mutation-confirmed: reverting `gmail_adapter.py`'s
    `budget_exhausted` fix reproduces `status="succeeded"` with only 3 of
    5 messages ever written -- silently incomplete, not merely capped.
    """
    import ecc.domains.personal.gmail_adapter as gmail_adapter_module

    monkeypatch.setattr(gmail_adapter_module, "_MAX_MESSAGES_PER_CALL", 3)

    context, _owner_id = seeded_gmail_account
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    message_ids = [f"msg-{i}" for i in range(5)]
    bodies = {
        message_id: _message_body(
            message_id=message_id,
            thread_id=f"thread-{message_id}",
            from_addr="alice@example.test",
            to_addrs=[_OWNER_EMAIL],
            internal_date_ms=now_ms + i,
        )
        for i, message_id in enumerate(message_ids)
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/gmail/v1/users/me/messages":
            # A single page, no `nextPageToken` -- this is Gmail's own
            # last (and only) page for this query, with more refs than
            # the (monkeypatched) budget allows.
            return _json_response({"messages": [{"id": m} for m in message_ids]})
        if request.url.path.startswith("/gmail/v1/users/me/messages/"):
            message_id = request.url.path.rsplit("/", 1)[-1]
            return _json_response(bodies[message_id])
        raise AssertionError(f"unexpected request to {request.url}")

    adapter = GmailAdapter(transport=httpx.MockTransport(handler))
    outcome = adapter.backfill(context, "message", since=datetime.now(UTC) - timedelta(days=1))

    assert outcome.status == "partial"
    assert outcome.items_processed == 3
    assert outcome.next_cursor is None
    assert outcome.error_summary is not None and "per-call bound" in outcome.error_summary

    rows = _threads_and_messages(context.workspace_id)
    assert len(rows) == 3


def test_incremental_sync_reports_partial_not_succeeded_when_budget_exhausted_mid_page(
    seeded_gmail_account: tuple[ConnectorAccountContext, UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same bug, same fix, in `_sync_history`'s identical loop shape --
    see `test_backfill_reports_partial_not_succeeded_when_budget_exhausted_
    mid_page`'s own docstring for the full mechanism.
    """
    import ecc.domains.personal.gmail_adapter as gmail_adapter_module

    monkeypatch.setattr(gmail_adapter_module, "_MAX_MESSAGES_PER_CALL", 2)

    context, _owner_id = seeded_gmail_account
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    message_ids = [f"hist-{i}" for i in range(4)]
    bodies = {
        message_id: _message_body(
            message_id=message_id,
            thread_id=f"thread-{message_id}",
            from_addr="carol@example.test",
            to_addrs=[_OWNER_EMAIL],
            internal_date_ms=now_ms + i,
        )
        for i, message_id in enumerate(message_ids)
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/gmail/v1/users/me/history":
            return _json_response(
                {
                    "history": [{"messagesAdded": [{"message": {"id": m}}]} for m in message_ids],
                    "historyId": 999,
                }
            )
        if request.url.path.startswith("/gmail/v1/users/me/messages/"):
            message_id = request.url.path.rsplit("/", 1)[-1]
            return _json_response(bodies[message_id])
        raise AssertionError(f"unexpected request to {request.url}")

    adapter = GmailAdapter(transport=httpx.MockTransport(handler))
    outcome = adapter.incremental_sync(context, "message", "100")

    assert outcome.status == "partial"
    assert outcome.items_processed == 2
    assert outcome.next_cursor == "100"
    assert outcome.error_summary is not None and "per-call bound" in outcome.error_summary

    rows = _threads_and_messages(context.workspace_id)
    assert len(rows) == 2


# --- pagination across multiple messages.list/history.list pages -------------


def test_backfill_paginates_across_multiple_messages_list_pages(
    seeded_gmail_account: tuple[ConnectorAccountContext, UUID],
) -> None:
    """Every other `backfill` test in this file exercises a single
    `messages.list` page (`nextPageToken` absent) -- this is the only test
    proving the `while`/`pageToken` loop that walks a real multi-page
    `messages.list` response actually advances page-to-page rather than
    only ever having been exercised with one page.
    """
    context, _owner_id = seeded_gmail_account
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    body_1 = _message_body(
        message_id="page1-msg",
        thread_id="thread-page1",
        from_addr="alice@example.test",
        to_addrs=[_OWNER_EMAIL],
        internal_date_ms=now_ms,
        history_id=201,
    )
    body_2 = _message_body(
        message_id="page2-msg",
        thread_id="thread-page2",
        from_addr="bob@example.test",
        to_addrs=[_OWNER_EMAIL],
        internal_date_ms=now_ms + 1000,
        history_id=202,
    )
    seen_page_tokens: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/gmail/v1/users/me/messages":
            page_token = request.url.params.get("pageToken")
            seen_page_tokens.append(page_token)
            if page_token is None:
                return _json_response(
                    {"messages": [{"id": "page1-msg"}], "nextPageToken": "page-2"}
                )
            assert page_token == "page-2"
            return _json_response({"messages": [{"id": "page2-msg"}]})
        if request.url.path == "/gmail/v1/users/me/messages/page1-msg":
            return _json_response(body_1)
        if request.url.path == "/gmail/v1/users/me/messages/page2-msg":
            return _json_response(body_2)
        raise AssertionError(f"unexpected request to {request.url}")

    adapter = GmailAdapter(transport=httpx.MockTransport(handler))
    outcome = adapter.backfill(context, "message", since=datetime.now(UTC) - timedelta(days=1))

    assert outcome.status == "succeeded"
    assert outcome.items_processed == 2
    assert outcome.next_cursor == "202"
    assert seen_page_tokens == [None, "page-2"]

    rows = _threads_and_messages(context.workspace_id)
    assert {row["external_message_id"] for row in rows} == {"page1-msg", "page2-msg"}


def test_incremental_sync_paginates_across_multiple_history_list_pages(
    seeded_gmail_account: tuple[ConnectorAccountContext, UUID],
) -> None:
    """Same coverage gap as the `backfill` test above, for `_sync_history`'s
    own `history.list` pagination loop.
    """
    context, _owner_id = seeded_gmail_account
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    body = _message_body(
        message_id="hist-page2-msg",
        thread_id="thread-hist-page2",
        from_addr="dave@example.test",
        to_addrs=[_OWNER_EMAIL],
        internal_date_ms=now_ms,
    )
    seen_page_tokens: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/gmail/v1/users/me/history":
            page_token = request.url.params.get("pageToken")
            seen_page_tokens.append(page_token)
            if page_token is None:
                return _json_response(
                    {"history": [], "historyId": 300, "nextPageToken": "hist-page-2"}
                )
            assert page_token == "hist-page-2"
            return _json_response(
                {
                    "history": [{"messagesAdded": [{"message": {"id": "hist-page2-msg"}}]}],
                    "historyId": 310,
                }
            )
        if request.url.path == "/gmail/v1/users/me/messages/hist-page2-msg":
            return _json_response(body)
        raise AssertionError(f"unexpected request to {request.url}")

    adapter = GmailAdapter(transport=httpx.MockTransport(handler))
    outcome = adapter.incremental_sync(context, "message", "100")

    assert outcome.status == "succeeded"
    assert outcome.items_processed == 1
    assert outcome.next_cursor == "310"
    assert seen_page_tokens == [None, "hist-page-2"]

    rows = _threads_and_messages(context.workspace_id)
    assert rows[0]["external_message_id"] == "hist-page2-msg"


# --- _process_message guard clauses: skip gracefully, never crash ------------
#
# `_process_message`'s own docstring: "A malformed/incomplete response
# shape is skipped ... rather than raising." Every branch below was
# previously only exercised indirectly (or not at all) through the
# happy-path tests above -- these call `_process_message` directly (no
# HTTP mocking needed, it takes an already-parsed response body) to prove
# each guard clause actually returns `None`/skips rather than raising,
# per this file's own review-round-1 coverage sweep.


def test_process_message_skips_when_thread_id_missing(
    seeded_gmail_account: tuple[ConnectorAccountContext, UUID],
) -> None:
    context, owner_id = seeded_gmail_account
    adapter = GmailAdapter()
    body = _message_body(
        message_id="msg-no-thread",
        thread_id="irrelevant",
        from_addr="alice@example.test",
        to_addrs=[_OWNER_EMAIL],
        internal_date_ms=int(datetime.now(UTC).timestamp() * 1000),
    )
    del body["threadId"]

    result = adapter._process_message(
        body,
        workspace_id=context.workspace_id,
        owner_id=owner_id,
        connector_account_id=context.connector_account_id,
        now=datetime.now(UTC),
    )
    assert result is None
    assert _threads_and_messages(context.workspace_id) == []


def test_process_message_skips_when_from_header_missing(
    seeded_gmail_account: tuple[ConnectorAccountContext, UUID],
) -> None:
    context, owner_id = seeded_gmail_account
    adapter = GmailAdapter()
    body = _message_body(
        message_id="msg-no-from",
        thread_id="thread-no-from",
        from_addr="irrelevant@example.test",
        to_addrs=[_OWNER_EMAIL],
        internal_date_ms=int(datetime.now(UTC).timestamp() * 1000),
    )
    body["payload"]["headers"] = [h for h in body["payload"]["headers"] if h["name"] != "From"]

    result = adapter._process_message(
        body,
        workspace_id=context.workspace_id,
        owner_id=owner_id,
        connector_account_id=context.connector_account_id,
        now=datetime.now(UTC),
    )
    assert result is None
    assert _threads_and_messages(context.workspace_id) == []


def test_process_message_skips_when_no_recipients(
    seeded_gmail_account: tuple[ConnectorAccountContext, UUID],
) -> None:
    context, owner_id = seeded_gmail_account
    adapter = GmailAdapter()
    body = _message_body(
        message_id="msg-no-to",
        thread_id="thread-no-to",
        from_addr="alice@example.test",
        to_addrs=["irrelevant@example.test"],
        internal_date_ms=int(datetime.now(UTC).timestamp() * 1000),
    )
    body["payload"]["headers"] = [h for h in body["payload"]["headers"] if h["name"] != "To"]

    result = adapter._process_message(
        body,
        workspace_id=context.workspace_id,
        owner_id=owner_id,
        connector_account_id=context.connector_account_id,
        now=datetime.now(UTC),
    )
    assert result is None
    assert _threads_and_messages(context.workspace_id) == []


def test_process_message_skips_when_internal_date_is_non_numeric(
    seeded_gmail_account: tuple[ConnectorAccountContext, UUID],
) -> None:
    context, owner_id = seeded_gmail_account
    adapter = GmailAdapter()
    body = _message_body(
        message_id="msg-bad-date",
        thread_id="thread-bad-date",
        from_addr="alice@example.test",
        to_addrs=[_OWNER_EMAIL],
        internal_date_ms=0,
    )
    body["internalDate"] = "not-a-number"

    result = adapter._process_message(
        body,
        workspace_id=context.workspace_id,
        owner_id=owner_id,
        connector_account_id=context.connector_account_id,
        now=datetime.now(UTC),
    )
    assert result is None
    assert _threads_and_messages(context.workspace_id) == []


def test_process_message_skips_when_message_id_missing(
    seeded_gmail_account: tuple[ConnectorAccountContext, UUID],
) -> None:
    context, owner_id = seeded_gmail_account
    adapter = GmailAdapter()
    body = _message_body(
        message_id="irrelevant",
        thread_id="thread-no-id",
        from_addr="alice@example.test",
        to_addrs=[_OWNER_EMAIL],
        internal_date_ms=int(datetime.now(UTC).timestamp() * 1000),
    )
    del body["id"]

    result = adapter._process_message(
        body,
        workspace_id=context.workspace_id,
        owner_id=owner_id,
        connector_account_id=context.connector_account_id,
        now=datetime.now(UTC),
    )
    assert result is None
    assert _threads_and_messages(context.workspace_id) == []
