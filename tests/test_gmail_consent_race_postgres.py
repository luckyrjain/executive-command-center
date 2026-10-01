"""Security Remediation FX5: the Gmail consent / disconnect race.

Before this fix, consent (`email` domain consent active, Gmail connector
not `disconnected`) was checked in a transaction of its own before each
Gmail fetch/detection, and the revocation cascade
(`gmail_revocation.cascade_email_revocation`) took no lock those writers
read. A disable that committed between that check and a write let the
write land after the purge, so messages, evidence, `ai_runs` and
recommendations persisted after consent was withdrawn (the detection
window spans the model call).

Now every Gmail write transaction re-checks consent and the connector
under `FOR KEY SHARE` on the owner's `gmail` `connector_accounts` rows
(after the shared membership lock, and after the idempotency lock where
there is one), and the cascade takes `FOR UPDATE` on those rows before
it purges anything. A write that commits first is purged by the cascade;
a write that starts after sees the withdrawal and writes nothing
(`EmailConsentInactiveError`), which stops the sync (403
`EMAIL_CONSENT_NOT_ACTIVE`, run closed as failed) or the detection batch.

Concurrency is real (request threads + separate connections); every wait
is bounded, and "is waiting" is observed in `pg_stat_activity`, not
assumed from timing.
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from gmail_sync_fixtures import (
    FakeGmailMessage,
    FakeGoogle,
    FakeMailbox,
    GmailSyncHarness,
    cleanup_workspace,
    csrf_headers,
    gmail_sync_harness,
)
from identity_fixtures import create_identity
from lock_race_support import holder_backend_pid
from sqlalchemy import event, text
from sqlalchemy.exc import OperationalError

import ecc.domains.ai_runtime.runtime as runtime_module
import ecc.domains.engineering.connector_accounts as connector_accounts_module
import ecc.domains.governance.recommendation_mutations as recommendation_mutations_module
import ecc.domains.personal.gmail_action_detection as gmail_action_detection_module
import ecc.domains.personal.gmail_adapter as gmail_adapter_module
import ecc.domains.personal.gmail_revocation as gmail_revocation_module
from ecc.config import get_settings
from ecc.database import SessionFactory, engine
from ecc.domains.personal.gmail_adapter import resolve_or_create_person
from ecc.domains.personal.gmail_shared import require_email_consent_locked
from ecc.platform.connector_security import (
    EmailConsentInactiveError,
    membership_mutation_lock_key,
)

pytestmark = pytest.mark.skipif(
    not get_settings().database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)

_FLAG_ON = {"ECC_PERSONAL_DATA_ISOLATION": "true"}
_WAIT_SECONDS = 15
_CONSENT_RUN_SUMMARY = "sync stopped: email consent is no longer active"


# --- world -------------------------------------------------------------------


@dataclass
class ConsentWorld:
    """One workspace: A (`owner`) and C (`member`, the mailbox owner whose
    consent is withdrawn), with the Gmail sync harness active."""

    ws: UUID
    a: UUID
    c: UUID
    account_ids: dict[str, UUID]
    harness: GmailSyncHarness
    suffix: str
    mailboxes: dict[str, FakeMailbox] = field(default_factory=dict)

    @property
    def fake_google(self) -> FakeGoogle:
        return self.harness.fake_google

    def client(self, user_id: UUID) -> tuple[Any, str]:
        return self.harness.client_for(self.ws, user_id)

    def mailbox(self, key: str) -> FakeMailbox:
        if key not in self.mailboxes:
            now = datetime.now(UTC)
            self.mailboxes[key] = FakeMailbox(
                google_email=f"fx5-{key}-{self.suffix}@example.test",
                messages=tuple(
                    FakeGmailMessage(
                        external_message_id=f"fx5-{key}-{i}-{self.suffix}",
                        external_thread_id=f"fx5-{key}-thread-{i}",
                        sender_email=f"sender-{key}-{i}-{self.suffix}@partner.test",
                        sender_name=f"Sender {i}",
                        subject=f"Subject {i}",
                        body_text="Please review the attached document.",
                        sent_at=now - timedelta(minutes=10 - i),
                    )
                    for i in range(2)
                ),
            )
        return self.mailboxes[key]

    def connect(self, user_id: UUID, key: str) -> UUID:
        client, token = self.client(user_id)
        mailbox = self.mailbox(key)
        code = self.fake_google.register(key, mailbox)
        self.harness._allow(f"fx5-member-{key}-{self.suffix}@example.test")
        self.harness._allow(mailbox.google_email)
        enable = client.post(
            "/api/v1/personal/domains",
            headers=csrf_headers(token, str(uuid4())),
            json={"domain_key": "email"},
        )
        assert enable.status_code == 201, enable.text
        start = client.post("/api/v1/personal/gmail/oauth/start", headers=csrf_headers(token))
        assert start.status_code == 200, start.text
        state = httpx.URL(start.json()["authorization_url"]).params["state"]
        callback = client.get(
            "/api/v1/personal/gmail/oauth/callback", params={"code": code, "state": state}
        )
        assert callback.status_code == 200, callback.text
        return UUID(callback.json()["id"])

    def sync(self, user_id: UUID, connector_id: UUID) -> httpx.Response:
        client, token = self.client(user_id)
        return client.post(
            f"/api/v1/engineering/connectors/{connector_id}/sync",
            headers=csrf_headers(token, str(uuid4())),
            json={"run_type": "backfill", "resource_type": "message"},
        )

    def disable_email(self, user_id: UUID) -> httpx.Response:
        client, token = self.client(user_id)
        return client.post(
            "/api/v1/personal/domains/email/disable",
            headers=csrf_headers(token, str(uuid4())),
        )

    def remove(self, *, actor: UUID, target: UUID) -> httpx.Response:
        client, token = self.client(actor)
        return client.delete(
            f"/api/v1/identity/workspaces/{self.ws}/members/{target}",
            headers=csrf_headers(token),
        )


@contextmanager
def _consent_world(env: dict[str, str] | None) -> Iterator[ConsentWorld]:
    suffix = uuid4().hex[:10]
    ws, a, c = uuid4(), uuid4(), uuid4()
    now = datetime.now(UTC)
    account_ids: dict[str, UUID] = {}
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, 'FX5 consent race', 'UTC', :now)"
            ),
            {"id": ws, "now": now},
        )
        for key, user_id, role, offset in (("a", a, "owner", 0), ("c", c, "member", 1)):
            account_ids[key] = create_identity(
                connection,
                workspace_id=ws,
                user_id=user_id,
                email=f"fx5-member-{key}-{suffix}@example.test",
                now=now + timedelta(seconds=offset),
                role=role,
            )
    try:
        with gmail_sync_harness(env=env) as harness:
            yield ConsentWorld(
                ws=ws, a=a, c=c, account_ids=account_ids, harness=harness, suffix=suffix
            )
    finally:
        cleanup_workspace(
            ws,
            list(account_ids.values()),
            extra_cleanup_tables=("member_notifications", "deletion_jobs"),
        )


@pytest.fixture
def world() -> Iterator[ConsentWorld]:
    with _consent_world(_FLAG_ON) as built:
        yield built


# --- concurrency helpers -------------------------------------------------------


@dataclass
class _Call:
    thread: threading.Thread
    result: dict[str, Any]

    @property
    def response(self) -> httpx.Response:
        assert "error" not in self.result, self.result["error"]
        return self.result["response"]

    def join(self) -> None:
        self.thread.join(timeout=_WAIT_SECONDS)
        assert not self.thread.is_alive(), "request thread did not finish"


def _in_thread(call: Callable[[], httpx.Response]) -> _Call:
    result: dict[str, Any] = {}

    def run() -> None:
        try:
            result["response"] = call()
        except BaseException as exc:  # noqa: BLE001 -- surfaced by `_Call.response`
            result["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return _Call(thread=thread, result=result)


def _lock_waiters(query_pattern: str, *, holder_pid: int) -> int:
    """Backends of this database blocked *by `holder_pid`*
    (`pg_blocking_pids`) on a row lock (`transactionid` / `tuple` wait)
    whose current statement matches `query_pattern`. Scoped to the holder so
    an unrelated waiter (another test sharing the database) cannot satisfy
    the wait."""
    with engine.connect() as probe:
        return int(
            probe.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND wait_event_type = 'Lock' "
                    "AND wait_event IN ('transactionid', 'tuple') "
                    "AND pg_blocking_pids(pid) @> ARRAY[CAST(:holder AS integer)] "
                    "AND query ~* :pattern"
                ),
                {"holder": holder_pid, "pattern": query_pattern},
            ).scalar_one()
        )


def _membership_lock_waiters(ws: UUID) -> int:
    with engine.connect() as probe:
        return int(
            probe.execute(
                text(
                    "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted "
                    "AND ((classid::bigint << 32) | objid::bigint) = hashtextextended(:k, 0)"
                ),
                {"k": membership_mutation_lock_key(ws)},
            ).scalar_one()
        )


_CASCADE_LOCK = r"FROM (personal_domains|connector_accounts).*FOR UPDATE"
_WRITER_LOCK = r"FROM (personal_domains|connector_accounts).*FOR KEY SHARE"


def _wait_until(predicate: Callable[[], bool], call: _Call | None, what: str) -> None:
    deadline = time.monotonic() + _WAIT_SECONDS
    while time.monotonic() < deadline:
        if predicate():
            return
        if call is not None:
            assert call.thread.is_alive(), f"finished before {what}: {call.result}"
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting until {what}")


# --- readers -------------------------------------------------------------------


def _scalar(sql: str, **params: Any) -> Any:
    with engine.connect() as connection:
        return connection.execute(text(sql), params).scalar_one()


def _count(table: str, ws: UUID, where: str = "TRUE", **params: Any) -> int:
    return int(
        _scalar(
            f"SELECT count(*) FROM {table} WHERE workspace_id = :ws AND ({where})",  # noqa: S608
            ws=ws,
            **params,
        )
    )


def _error_code(response: httpx.Response) -> str:
    return str(response.json()["error"]["code"])


# `gmail_sync` evidence the cascade purges: all of it except rows still
# referenced by an entity alias (a person node's source evidence, which the
# cascade deliberately keeps -- `gmail_revocation`'s "What is deliberately
# NOT deleted").
_PURGEABLE_EVIDENCE = (
    "source_type = 'gmail_sync' AND NOT EXISTS (SELECT 1 FROM entity_aliases ea "
    "WHERE ea.workspace_id = pkos_evidence.workspace_id AND ea.source_id = pkos_evidence.id)"
)


def _gmail_rows(world: ConsentWorld) -> dict[str, int]:
    """Every Gmail-content row the cascade purges, for C's mailbox."""
    return {
        "email_threads": _count("email_threads", world.ws),
        "email_messages": _count("email_messages", world.ws),
        "purgeable_evidence": _count("pkos_evidence", world.ws, _PURGEABLE_EVIDENCE),
        "recommendations": _count("recommendations", world.ws),
    }


_PURGED = {
    "email_threads": 0,
    "email_messages": 0,
    "purgeable_evidence": 0,
    "recommendations": 0,
}

_SNAPSHOT_TABLES = (
    "email_threads",
    "email_messages",
    "pkos_evidence",
    "pkos_nodes",
    "entity_aliases",
    "recommendations",
    "ai_runs",
    "ai_run_steps",
)


def _snapshot(world: ConsentWorld) -> dict[str, int]:
    """Row counts of every table a Gmail sync/detection writes."""
    return {table: _count(table, world.ws) for table in _SNAPSHOT_TABLES}


def _detection_runs(world: ConsentWorld) -> dict[str, int]:
    return {
        "ai_runs": _count("ai_runs", world.ws, "task_type = 'email.detect_action'"),
        "ai_run_steps": _count("ai_run_steps", world.ws),
    }


def _connector_status(connector: UUID) -> str:
    return str(_scalar("SELECT status FROM connector_accounts WHERE id = :id", id=connector))


def _message_fetches(world: ConsentWorld, fmt: str) -> list[str]:
    prefix = "/gmail/v1/users/me/messages/"
    return [
        r.url.path.removeprefix(prefix)
        for r in world.fake_google.requests
        if r.url.path.startswith(prefix) and r.url.params.get("format") == fmt
    ]


def _run_summaries(connector: UUID) -> str:
    return str(
        _scalar(
            "SELECT json_agg(json_build_array(status, error_summary))::text FROM sync_runs "
            "WHERE connector_account_id = :id",
            id=connector,
        )
    )


def _disable_once(world: ConsentWorld, result: dict[str, Any]) -> None:
    """Runs C's real `email` disable on its own thread, once, and waits for
    it to commit -- called from inside C's in-flight sync/detection.
    `result["after"]` is the row snapshot right after that commit: nothing
    may be written after it."""
    if "response" not in result:
        disable = _in_thread(lambda: world.disable_email(world.c))
        disable.join()
        result["response"] = disable.response
        result["after"] = _snapshot(world)


# --- sync writes -----------------------------------------------------------------


def test_message_write_after_cascade_commit_writes_nothing_and_stops_sync(
    world: ConsentWorld,
) -> None:
    """C's sync fetched message 1 (consent's unlocked pre-check had passed)
    when C's disable committed. Message 1's write transaction re-checks
    consent under the connector row lock and refuses: no message 1 row, and
    the purge of message 0 (committed earlier) stands -- no thread, message
    or evidence survives. The sync stops: 403 EMAIL_CONSENT_NOT_ACTIVE, run
    closed as failed, no cursor, no `synced` audit, no detection."""
    connector = world.connect(world.c, "c")
    gmail = connector_accounts_module.connector_registry.get("gmail")
    assert isinstance(gmail, gmail_adapter_module.GmailAdapter)
    second = world.mailbox("c").messages[1].external_message_id
    disabled: dict[str, Any] = {}
    original = gmail._request_with_rate_limit_retry

    def fetch_then_disable(method: str, path: str, **kwargs: Any) -> Any:
        fetched = original(method, path, **kwargs)
        if path.endswith(f"/messages/{second}"):
            _disable_once(world, disabled)
        return fetched

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(gmail, "_request_with_rate_limit_retry", fetch_then_disable)
        response = world.sync(world.c, connector)

    assert disabled["response"].status_code == 200, disabled["response"].text
    assert response.status_code == 403, response.text
    assert _error_code(response) == "EMAIL_CONSENT_NOT_ACTIVE"
    assert _gmail_rows(world) == _PURGED
    assert _snapshot(world) == disabled["after"]
    assert _connector_status(connector) == "disconnected"
    assert _run_summaries(connector) == f'[["failed", "{_CONSENT_RUN_SUMMARY}"]]'
    assert _count("sync_cursors", world.ws) == 0
    assert _count("audit_events", world.ws, "event_type = 'connector_account.synced'") == 0
    assert _detection_runs(world) == {"ai_runs": 0, "ai_run_steps": 0}
    assert _message_fetches(world, "full") == []


def test_withdrawal_seen_by_the_unlocked_pre_check_is_the_same_403_skip(
    world: ConsentWorld,
) -> None:
    """One contract (round 1, B2): consent revoked after the message list
    was fetched and before the first message -- caught by the cheap
    unlocked per-message pre-check, not a locked write -- ends exactly like
    a locked refusal: 403 EMAIL_CONSENT_NOT_ACTIVE, run failed, no cursor,
    no `synced`/`sync_failed` audit, nothing fetched or written; never a
    201 `partial`."""
    connector = world.connect(world.c, "c")
    gmail = connector_accounts_module.connector_registry.get("gmail")
    assert isinstance(gmail, gmail_adapter_module.GmailAdapter)
    original = gmail._request_with_rate_limit_retry

    def list_then_revoke(method: str, path: str, **kwargs: Any) -> Any:
        fetched = original(method, path, **kwargs)
        if path.endswith("/users/me/messages"):
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "UPDATE domain_consents SET revoked_at = now() WHERE workspace_id = :ws "
                        "AND domain_key = 'email'"
                    ),
                    {"ws": world.ws},
                )
        return fetched

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(gmail, "_request_with_rate_limit_retry", list_then_revoke)
        response = world.sync(world.c, connector)

    assert response.status_code == 403, response.text
    assert _error_code(response) == "EMAIL_CONSENT_NOT_ACTIVE"
    assert _run_summaries(connector) == f'[["failed", "{_CONSENT_RUN_SUMMARY}"]]'
    assert _count("sync_cursors", world.ws) == 0
    assert (
        _count(
            "audit_events",
            world.ws,
            "event_type IN ('connector_account.synced', 'connector_account.sync_failed')",
        )
        == 0
    )
    assert _message_fetches(world, "metadata") == []
    assert _snapshot(world) == dict.fromkeys(_SNAPSHOT_TABLES, 0)


def _revoke_consent_row(world: ConsentWorld) -> None:
    """Withdraws C's `email` consent without the purge cascade."""
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE domain_consents SET revoked_at = now() WHERE workspace_id = :ws "
                "AND domain_key = 'email' AND revoked_at IS NULL"
            ),
            {"ws": world.ws},
        )


def _sync_audits(world: ConsentWorld) -> int:
    return _count(
        "audit_events",
        world.ws,
        "event_type IN ('connector_account.synced', 'connector_account.sync_failed')",
    )


def test_incremental_withdrawal_seen_by_the_history_pre_check_is_the_same_403_skip(
    world: ConsentWorld,
) -> None:
    """Incremental twin of the pre-check test: after a completed backfill,
    an incremental sync gets one new message from `history.list`; consent
    is revoked right after that response, so `_sync_history`'s per-message
    pre-check catches it. 403 EMAIL_CONSENT_NOT_ACTIVE, that run failed,
    the cursor unchanged, no sync audit from the incremental run, the new
    message never fetched and nothing new written."""
    connector = world.connect(world.c, "c")
    backfill = world.sync(world.c, connector)
    assert backfill.status_code == 201, backfill.text
    assert backfill.json()["status"] == "succeeded", backfill.json()
    cursor_before = _scalar(
        "SELECT json_agg(json_build_array(resource_type, cursor_value, "
        "backfill_resume_cursor, updated_at) ORDER BY resource_type)::text "
        "FROM sync_cursors WHERE connector_account_id = :id",
        id=connector,
    )
    assert _count("sync_cursors", world.ws, "cursor_value IS NOT NULL") == 1, (
        "the backfill must leave an incremental cursor"
    )
    audits_before = _sync_audits(world)
    rows_before = _snapshot(world)
    gmail = connector_accounts_module.connector_registry.get("gmail")
    assert isinstance(gmail, gmail_adapter_module.GmailAdapter)
    original = gmail._request_with_rate_limit_retry
    new_message = f"fx5-c-new-{world.suffix}"

    def history_then_revoke(method: str, path: str, **kwargs: Any) -> Any:
        if path.endswith("/users/me/history"):
            original(method, path, **kwargs)
            _revoke_consent_row(world)
            return httpx.Response(
                200,
                json={
                    "history": [
                        {"id": "5000", "messagesAdded": [{"message": {"id": new_message}}]}
                    ],
                    "historyId": "5000",
                },
            )
        return original(method, path, **kwargs)

    client, token = world.client(world.c)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(gmail, "_request_with_rate_limit_retry", history_then_revoke)
        response = client.post(
            f"/api/v1/engineering/connectors/{connector}/sync",
            headers=csrf_headers(token, str(uuid4())),
            json={"run_type": "incremental", "resource_type": "message"},
        )

    assert response.status_code == 403, response.text
    assert _error_code(response) == "EMAIL_CONSENT_NOT_ACTIVE"
    runs = _scalar(
        "SELECT json_agg(json_build_array(run_type, status, error_summary) "
        "ORDER BY started_at)::text FROM sync_runs WHERE connector_account_id = :id",
        id=connector,
    )
    assert runs == (
        f'[["backfill", "succeeded", null], ["incremental", "failed", "{_CONSENT_RUN_SUMMARY}"]]'
    )
    assert (
        _scalar(
            "SELECT json_agg(json_build_array(resource_type, cursor_value, "
            "backfill_resume_cursor, updated_at) ORDER BY resource_type)::text "
            "FROM sync_cursors WHERE connector_account_id = :id",
            id=connector,
        )
        == cursor_before
    )
    assert _sync_audits(world) == audits_before
    assert new_message not in _message_fetches(world, "metadata")
    assert _snapshot(world) == rows_before


def test_never_active_consent_sync_endpoint_is_the_403_skip(world: ConsentWorld) -> None:
    """Visible contract change: a sync whose owner's `email` consent is
    not active when the adapter starts (was: 201 with a `failed` run and
    a `sync_failed` audit) is now the 403 EMAIL_CONSENT_NOT_ACTIVE skip --
    run failed, no cursor, no sync audit, no Gmail call, nothing written."""
    connector = world.connect(world.c, "c")
    _revoke_consent_row(world)
    requests_before = len(world.fake_google.requests)

    response = world.sync(world.c, connector)

    assert response.status_code == 403, response.text
    assert _error_code(response) == "EMAIL_CONSENT_NOT_ACTIVE"
    assert _run_summaries(connector) == f'[["failed", "{_CONSENT_RUN_SUMMARY}"]]'
    assert _count("sync_cursors", world.ws) == 0
    assert _sync_audits(world) == 0
    assert [
        r.url.path for r in world.fake_google.requests[requests_before:] if "/gmail/" in r.url.path
    ] == []
    assert _snapshot(world) == dict.fromkeys(_SNAPSHOT_TABLES, 0)
    assert _connector_status(connector) == "active"


def test_guard_locks_only_the_known_connector_row(world: ConsentWorld) -> None:
    """Single-row lock (round 1, B1): C owns two Gmail connectors, A and B.
    Another connection holds connector B `FOR UPDATE` (as sync phase 1 does
    across a token refresh). The guard for connector A must not wait on B
    (`lock_timeout` 200ms); the owner-wide form (no connector id) does."""
    connector_a = world.connect(world.c, "c")
    connector_b = uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TEMP TABLE fx5_copy ON COMMIT DROP AS "
                "SELECT * FROM connector_accounts WHERE id = :a"
            ),
            {"a": connector_a},
        )
        connection.execute(
            text("UPDATE fx5_copy SET id = :b, external_account_id = :ext"),
            {"b": connector_b, "ext": f"fx5-second-{world.suffix}"},
        )
        connection.execute(text("INSERT INTO connector_accounts SELECT * FROM fx5_copy"))

    def guard(connector_account_id: UUID | None) -> None:
        with SessionFactory() as session, session.begin():
            session.execute(text("SET LOCAL lock_timeout = '200ms'"))
            require_email_consent_locked(
                session,
                workspace_id=world.ws,
                owner_id=world.c,
                connector_account_id=connector_account_id,
            )

    with engine.connect() as holder, holder.begin():
        holder.execute(
            text("SELECT id FROM connector_accounts WHERE id = :b FOR UPDATE"),
            {"b": connector_b},
        )
        guard(connector_a)  # completes without waiting on B
        with pytest.raises(OperationalError, match="lock timeout"):
            guard(None)
    guard(connector_a)
    guard(None)


def test_participant_write_after_cascade_commit_writes_nothing(world: ConsentWorld) -> None:
    """The disable commits after message 0's row was written and before its
    participants are resolved: the resolution transaction refuses (no
    person node, alias or evidence), the error is not swallowed as a
    per-participant failure, and the sync stops before fetching message 1."""
    connector = world.connect(world.c, "c")
    disabled: dict[str, Any] = {}
    original = gmail_adapter_module.resolve_or_create_person

    def disable_then_resolve(**kwargs: Any) -> UUID:
        _disable_once(world, disabled)
        return original(**kwargs)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(gmail_adapter_module, "resolve_or_create_person", disable_then_resolve)
        response = world.sync(world.c, connector)

    assert disabled["response"].status_code == 200, disabled["response"].text
    assert response.status_code == 403, response.text
    assert _error_code(response) == "EMAIL_CONSENT_NOT_ACTIVE"
    assert _gmail_rows(world) == _PURGED
    assert _snapshot(world) == disabled["after"]
    assert _count("pkos_nodes", world.ws) == 0
    assert _count("entity_aliases", world.ws) == 0
    assert _message_fetches(world, "metadata") == [
        world.mailbox("c").messages[0].external_message_id
    ]


def test_cascade_waits_for_an_in_flight_message_write_then_purges_it(
    world: ConsentWorld,
) -> None:
    """C's sync is paused INSIDE message 0's write transaction (consent
    re-checked, connector row key-share locked, message inserted, not yet
    committed). C's disable blocks on the connector row lock -- it has
    purged nothing yet -- until that write commits; then it purges the
    message the write just committed. Nothing Gmail-derived survives."""
    connector = world.connect(world.c, "c")
    entered, release = threading.Event(), threading.Event()
    original = gmail_adapter_module._insert_message_if_new
    writer_pid: list[int] = []  # the paused write transaction's backend

    def paused_insert(*args: Any, **kwargs: Any) -> Any:
        inserted = original(*args, **kwargs)
        if not entered.is_set():
            writer_pid.append(holder_backend_pid(args[0].connection()))
            entered.set()
            release.wait(timeout=_WAIT_SECONDS)
        return inserted

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(gmail_adapter_module, "_insert_message_if_new", paused_insert)
        sync = _in_thread(lambda: world.sync(world.c, connector))
        try:
            assert entered.wait(timeout=_WAIT_SECONDS), "sync never reached its message write"
            disable = _in_thread(lambda: world.disable_email(world.c))
            _wait_until(
                lambda: _lock_waiters(_CASCADE_LOCK, holder_pid=writer_pid[0]) > 0,
                disable,
                "the cascade waits on the connector row lock",
            )
            assert "response" not in disable.result
            # Waiting on the row lock, the cascade has purged nothing: the
            # writer's own (uncommitted) row is invisible, consent is still
            # recorded as active to everyone else.
            assert (
                _scalar(
                    "SELECT count(*) FROM domain_consents WHERE workspace_id = :ws "
                    "AND domain_key = 'email' AND revoked_at IS NULL",
                    ws=world.ws,
                )
                == 1
            )
        finally:
            release.set()
        disable.join()
        sync.join()

    assert disable.response.status_code == 200, disable.response.text
    assert _gmail_rows(world) == _PURGED
    assert _connector_status(connector) == "disconnected"
    # Whatever the sync did after its first write, it never out-wrote the
    # cascade: consent is withdrawn, and no Gmail row exists.
    assert sync.response.status_code in (201, 403), sync.response.text


def test_writer_blocked_by_an_in_flight_cascade_sees_the_withdrawal(world: ConsentWorld) -> None:
    """The reverse order: C's disable holds the connector row lock (paused
    right after taking it, before purging) while C's sync reaches a write.
    The write blocks on its key-share lock; once the cascade commits it
    re-reads the connector as `disconnected`, consent as revoked, and
    writes nothing."""
    connector = world.connect(world.c, "c")
    locked, release = threading.Event(), threading.Event()
    original_lock = gmail_revocation_module._lock_owner_gmail_connectors
    cascade_pid: list[int] = []  # the paused cascade's backend
    disabled: dict[str, _Call] = {}
    original_resolve = gmail_adapter_module.resolve_or_create_person

    def paused_lock(*args: Any, **kwargs: Any) -> Any:
        rows = original_lock(*args, **kwargs)
        cascade_pid.append(holder_backend_pid(args[0].connection()))
        locked.set()
        release.wait(timeout=_WAIT_SECONDS)
        return rows

    def release_when_writer_waits() -> None:
        _wait_until(
            lambda: _lock_waiters(_WRITER_LOCK, holder_pid=cascade_pid[0]) > 0,
            None,
            "the writer waits",
        )
        release.set()

    def start_disable_then_resolve(**kwargs: Any) -> UUID:
        if "call" not in disabled:
            disabled["call"] = _in_thread(lambda: world.disable_email(world.c))
            assert locked.wait(timeout=_WAIT_SECONDS), "cascade never took the row lock"
            threading.Thread(target=release_when_writer_waits, daemon=True).start()
        return original_resolve(**kwargs)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(gmail_revocation_module, "_lock_owner_gmail_connectors", paused_lock)
        mp.setattr(gmail_adapter_module, "resolve_or_create_person", start_disable_then_resolve)
        try:
            response = world.sync(world.c, connector)
        finally:
            release.set()
        disabled["call"].join()

    assert disabled["call"].response.status_code == 200, disabled["call"].response.text
    assert response.status_code == 403, response.text
    assert _error_code(response) == "EMAIL_CONSENT_NOT_ACTIVE"
    assert _gmail_rows(world) == _PURGED
    assert _count("pkos_nodes", world.ws) == 0


def test_resolve_or_create_person_refuses_withdrawn_consent_or_disconnected_connector(
    world: ConsentWorld,
) -> None:
    """The locked re-check itself, one condition at a time: consent revoked
    (connector still live), then connector disconnected (consent active)."""
    connector = world.connect(world.c, "c")

    def resolve() -> None:
        resolve_or_create_person(
            workspace_id=world.ws,
            owner_id=world.c,
            email=f"someone-{world.suffix}@partner.test",
            display_name="Someone",
            source_ref="gmail:fx5",
            now=datetime.now(UTC),
            connector_account_id=connector,
        )

    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE domain_consents SET revoked_at = now() WHERE workspace_id = :ws "
                "AND owner_id = :u AND domain_key = 'email'"
            ),
            {"ws": world.ws, "u": world.c},
        )
    with pytest.raises(EmailConsentInactiveError):
        resolve()

    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE domain_consents SET revoked_at = NULL WHERE workspace_id = :ws "
                "AND owner_id = :u AND domain_key = 'email'"
            ),
            {"ws": world.ws, "u": world.c},
        )
        connection.execute(
            text("UPDATE connector_accounts SET status = 'disconnected' WHERE id = :id"),
            {"id": connector},
        )
    with pytest.raises(EmailConsentInactiveError):
        resolve()

    assert _count("pkos_nodes", world.ws) == 0
    assert _count("entity_aliases", world.ws) == 0
    assert _count("pkos_evidence", world.ws) == 0


# --- action detection ------------------------------------------------------------


class _TriggeringOllama:
    """Wraps the harness's fake Ollama adapter: the model call first runs
    `trigger`, then answers normally."""

    def __init__(self, inner: Any, trigger: Callable[[], None]) -> None:
        self._inner = inner
        self._trigger = trigger

    def generate(self, *args: Any, **kwargs: Any) -> Any:
        self._trigger()
        return self._inner.generate(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def test_detection_writes_nothing_after_cascade_commits_during_model_call(
    world: ConsentWorld,
) -> None:
    """C's post-sync detection is inside the model call when C's disable
    commits. The run persist re-checks consent under the row lock and
    refuses: no `ai_runs`/`ai_run_steps`, no recommendation, and the
    detection evidence written before the model call was purged. The batch
    stops (one body fetch); the already-recorded sync still returns 201."""
    connector = world.connect(world.c, "c")
    disabled: dict[str, Any] = {}
    wrapped = _TriggeringOllama(
        runtime_module.OllamaAdapter(), lambda: _disable_once(world, disabled)
    )

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(runtime_module, "OllamaAdapter", lambda *_a, **_k: wrapped)
        response = world.sync(world.c, connector)

    assert disabled["response"].status_code == 200, disabled["response"].text
    assert response.status_code == 201, response.text
    assert _detection_runs(world) == {"ai_runs": 0, "ai_run_steps": 0}
    assert _gmail_rows(world) == _PURGED
    assert _snapshot(world) == disabled["after"]
    assert _count("pkos_evidence", world.ws, "source_ref LIKE 'gmail:detect_action:%'") == 0
    assert len(_message_fetches(world, "full")) == 1


def test_detection_evidence_not_written_after_cascade_commit(world: ConsentWorld) -> None:
    """The disable commits after detection resolved the sender and before
    the detect-action evidence insert: that insert's locked re-check
    refuses (no `gmail:detect_action:*` evidence), and the batch stops --
    no run, no recommendation, no second body fetch."""
    connector = world.connect(world.c, "c")
    disabled: dict[str, Any] = {}
    original = gmail_action_detection_module.resolve_or_create_person

    def resolve_then_disable(**kwargs: Any) -> UUID:
        node_id = original(**kwargs)
        _disable_once(world, disabled)
        return node_id

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(gmail_action_detection_module, "resolve_or_create_person", resolve_then_disable)
        response = world.sync(world.c, connector)

    assert disabled["response"].status_code == 200, disabled["response"].text
    assert response.status_code == 201, response.text
    assert _count("pkos_evidence", world.ws, "source_ref LIKE 'gmail:detect_action:%'") == 0
    assert _detection_runs(world) == {"ai_runs": 0, "ai_run_steps": 0}
    assert _gmail_rows(world) == _PURGED
    assert _snapshot(world) == disabled["after"]
    assert len(_message_fetches(world, "full")) == 1


def test_detection_recommendation_not_written_after_cascade_commit(world: ConsentWorld) -> None:
    """The run persisted while consent was active; the disable commits
    inside `create_recommendation` before its insert (via `request_hash`,
    which runs between the role check and the locks). The locked re-check
    after the idempotency lock refuses: no recommendation."""
    connector = world.connect(world.c, "c")
    disabled: dict[str, Any] = {}
    original = recommendation_mutations_module.request_hash

    def disable_then_hash(*args: Any, **kwargs: Any) -> Any:
        _disable_once(world, disabled)
        return original(*args, **kwargs)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(recommendation_mutations_module, "request_hash", disable_then_hash)
        response = world.sync(world.c, connector)

    assert disabled["response"].status_code == 200, disabled["response"].text
    assert response.status_code == 201, response.text
    assert _count("recommendations", world.ws) == 0
    assert _gmail_rows(world) == _PURGED
    assert _snapshot(world) == disabled["after"]
    assert len(_message_fetches(world, "full")) == 1


def test_body_not_stored_after_cascade_commit() -> None:
    """`fetch_and_store_body` (also the on-demand thread-open path): the
    disable commits after the Gmail GET and before the body UPDATE ->
    returns None, nothing stored (the message row itself is purged)."""
    env = {**_FLAG_ON, "ECC_EMAIL_ACTION_DETECTION_ENABLED": "false"}
    with _consent_world(env) as world:
        connector = world.connect(world.c, "c")
        sync = world.sync(world.c, connector)
        assert sync.status_code == 201, sync.text
        # Consent withdrawn without the purge (a plain revoke of the consent
        # row), so the message row the UPDATE targets still exists.
        message_id, external_id = (
            _scalar(
                "SELECT json_build_array(id, external_message_id)::text FROM email_messages "
                "WHERE workspace_id = :ws AND body IS NULL ORDER BY sent_at LIMIT 1",
                ws=world.ws,
            )
            .strip("[]")
            .replace('"', "")
            .split(", ")
        )
        gmail = connector_accounts_module.connector_registry.get("gmail")
        assert isinstance(gmail, gmail_adapter_module.GmailAdapter)
        original = gmail._request_with_rate_limit_retry

        def get_then_revoke(*args: Any, **kwargs: Any) -> Any:
            fetched = original(*args, **kwargs)
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "UPDATE domain_consents SET revoked_at = now() WHERE workspace_id = :ws "
                        "AND domain_key = 'email'"
                    ),
                    {"ws": world.ws},
                )
            return fetched

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(gmail, "_request_with_rate_limit_retry", get_then_revoke)
            stored = gmail.fetch_and_store_body(
                workspace_id=world.ws,
                message_id=UUID(message_id),
                external_message_id=external_id,
                headers={"Authorization": f"Bearer {FakeGoogle.access_token('c')}"},
            )

        assert stored is None
        assert _scalar("SELECT body IS NULL FROM email_messages WHERE id = :id", id=message_id)


# --- sync phase 3 --------------------------------------------------------------


def test_phase3_refuses_outcome_writes_after_cascade_commits_post_adapter(
    world: ConsentWorld,
) -> None:
    """The adapter returned a complete outcome; C's disable commits before
    phase 3 records it. Phase 3's own locked consent re-check (this
    connector's row) refuses: 403 EMAIL_CONSENT_NOT_ACTIVE, run failed, no
    cursor, no sync audit, no detection; the connector stays disconnected
    (not flipped back to `active`)."""
    connector = world.connect(world.c, "c")
    gmail = connector_accounts_module.connector_registry.get("gmail")
    assert isinstance(gmail, gmail_adapter_module.GmailAdapter)
    disabled: dict[str, Any] = {}
    original = gmail.backfill

    def backfill_then_disable(*args: Any, **kwargs: Any) -> Any:
        outcome = original(*args, **kwargs)
        _disable_once(world, disabled)
        return outcome

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(gmail, "backfill", backfill_then_disable)
        response = world.sync(world.c, connector)

    assert disabled["response"].status_code == 200, disabled["response"].text
    assert response.status_code == 403, response.text
    assert _error_code(response) == "EMAIL_CONSENT_NOT_ACTIVE"
    assert _run_summaries(connector) == f'[["failed", "{_CONSENT_RUN_SUMMARY}"]]'
    assert _count("sync_cursors", world.ws) == 0
    assert _sync_audits(world) == 0
    assert _connector_status(connector) == "disconnected"
    assert _detection_runs(world) == {"ai_runs": 0, "ai_run_steps": 0}
    assert _gmail_rows(world) == _PURGED
    assert _snapshot(world) == disabled["after"]


# --- recommendation confirm ------------------------------------------------------

_REDACTED = gmail_revocation_module._REDACTED_RATIONALE


def _pending_email_recommendation(world: ConsentWorld) -> tuple[UUID, int]:
    """Syncs C's mailbox (detection creates an `email_action_detected`
    recommendation) and publishes it to `pending_confirmation`. Returns
    `(id, version)`."""
    connector = world.connect(world.c, "c")
    sync = world.sync(world.c, connector)
    assert sync.status_code == 201, sync.text
    rec_id, version = (
        _scalar(
            "SELECT json_build_array(id, version)::text FROM recommendations "
            "WHERE workspace_id = :ws AND recommendation_type = 'email_action_detected' "
            "ORDER BY created_at LIMIT 1",
            ws=world.ws,
        )
        .strip("[]")
        .replace('"', "")
        .split(", ")
    )
    client, token = world.client(world.c)
    published = client.post(
        f"/api/v1/recommendations/{rec_id}/publish",
        headers=csrf_headers(token, str(uuid4())),
        json={"expected_version": int(version)},
    )
    assert published.status_code == 200, published.text
    assert published.json()["status"] == "pending_confirmation"
    return UUID(rec_id), int(published.json()["version"])


def _confirm(world: ConsentWorld, rec_id: UUID, version: int) -> httpx.Response:
    client, token = world.client(world.c)
    return client.post(
        f"/api/v1/recommendations/{rec_id}/confirm",
        headers=csrf_headers(token, str(uuid4())),
        json={"expected_version": version},
    )


def _rec_state(rec_id: UUID) -> dict[str, Any] | None:
    with engine.connect() as connection:
        row = (
            connection.execute(
                text(
                    "SELECT status, rationale, proposed_action::text AS proposed_action, "
                    "evidence_ids::text AS evidence_ids FROM recommendations WHERE id = :id"
                ),
                {"id": rec_id},
            )
            .mappings()
            .one_or_none()
        )
    return dict(row) if row is not None else None


def _assert_no_unredacted_executed(world: ConsentWorld) -> None:
    assert (
        _count(
            "recommendations",
            world.ws,
            "recommendation_type = 'email_action_detected' AND "
            "(status != 'executed' OR rationale IS DISTINCT FROM :r "
            "OR proposed_action != '{}'::jsonb OR cardinality(evidence_ids) != 0)",
            r=_REDACTED,
        )
        == 0
    )


def test_confirm_in_flight_serializes_with_the_cascade_and_ends_redacted(
    world: ConsentWorld,
) -> None:
    """C confirms an email-derived recommendation and is paused inside the
    confirm transaction (consent re-checked under the domain/connector key-
    share locks, recommendation locked, target task inserted). C's disable
    blocks on the `personal_domains` row lock -- before touching any
    recommendation -- until the confirm commits `executed`; then its cascade
    redacts that row. Never an unredacted `executed` recommendation."""
    rec_id, version = _pending_email_recommendation(world)
    entered, release = threading.Event(), threading.Event()
    original = recommendation_mutations_module.execute_target
    confirm_pid: list[int] = []  # the paused confirm transaction's backend

    def paused_execute(*args: Any, **kwargs: Any) -> Any:
        result = original(*args, **kwargs)
        confirm_pid.append(holder_backend_pid(args[0].connection()))
        entered.set()
        release.wait(timeout=_WAIT_SECONDS)
        return result

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(recommendation_mutations_module, "execute_target", paused_execute)
        confirm = _in_thread(lambda: _confirm(world, rec_id, version))
        try:
            assert entered.wait(timeout=_WAIT_SECONDS), "confirm never reached execute_target"
            disable = _in_thread(lambda: world.disable_email(world.c))
            _wait_until(
                lambda: _lock_waiters(
                    r"FROM personal_domains.*FOR UPDATE", holder_pid=confirm_pid[0]
                )
                > 0,
                disable,
                "the disable waits on the email domain row",
            )
            assert "response" not in disable.result
        finally:
            release.set()
        confirm.join()
        disable.join()

    assert confirm.response.status_code == 200, confirm.response.text
    assert confirm.response.json()["status"] == "executed"
    assert disable.response.status_code == 200, disable.response.text
    assert _rec_state(rec_id) == {
        "status": "executed",
        "rationale": _REDACTED,
        "proposed_action": "{}",
        "evidence_ids": "{}",
    }
    _assert_no_unredacted_executed(world)


def test_confirm_refused_once_consent_is_withdrawn(world: ConsentWorld) -> None:
    """Consent withdrawn (consent row revoked, no cascade yet): confirming
    the pending email recommendation is refused -- 403
    EMAIL_CONSENT_NOT_ACTIVE, recommendation unchanged, no task inserted."""
    rec_id, version = _pending_email_recommendation(world)
    tasks_before = _count("tasks", world.ws)
    _revoke_consent_row(world)

    response = _confirm(world, rec_id, version)

    assert response.status_code == 403, response.text
    assert _error_code(response) == "EMAIL_CONSENT_NOT_ACTIVE"
    state = _rec_state(rec_id)
    assert state is not None and state["status"] == "pending_confirmation"
    assert _count("tasks", world.ws) == tasks_before


def test_cascade_redacts_a_row_confirmed_while_its_delete_waited(world: ConsentWorld) -> None:
    """Cascade ordering (DELETE, then redact UPDATE), independent of the
    confirm guard: another transaction holds the pending recommendation
    `FOR UPDATE` and turns it `executed`. The cascade's DELETE waits on it;
    after that commits, the DELETE's re-check skips the now-executed row and
    the later UPDATE (fresh snapshot) redacts it."""
    rec_id, _version = _pending_email_recommendation(world)
    with engine.connect() as holder:
        with holder.begin():
            holder_pid = holder_backend_pid(holder)
            holder.execute(
                text("SELECT id FROM recommendations WHERE id = :id FOR UPDATE"), {"id": rec_id}
            )
            holder.execute(
                text("UPDATE recommendations SET status = 'executed' WHERE id = :id"),
                {"id": rec_id},
            )
            disable = _in_thread(lambda: world.disable_email(world.c))
            _wait_until(
                lambda: _lock_waiters(r"DELETE FROM recommendations", holder_pid=holder_pid) > 0,
                disable,
                "the cascade's recommendation DELETE waits on the row",
            )
        disable.join()

    assert disable.response.status_code == 200, disable.response.text
    assert _rec_state(rec_id) == {
        "status": "executed",
        "rationale": _REDACTED,
        "proposed_action": "{}",
        "evidence_ids": "{}",
    }
    _assert_no_unredacted_executed(world)


# --- lock ordering / no deadlock --------------------------------------------------


def test_writer_cascade_and_removal_together_do_not_deadlock(world: ConsentWorld) -> None:
    """C's sync is paused inside a write transaction holding the shared
    membership lock AND the connector key-share lock. C's disable (wants
    the connector row, FOR UPDATE) and A's removal of C (wants the
    membership lock exclusively, then the same row) both queue behind it.
    Releasing the writer lets both finish -- no deadlock error, both 200 --
    and nothing Gmail-derived survives."""
    connector = world.connect(world.c, "c")
    entered, release = threading.Event(), threading.Event()
    original = gmail_adapter_module._insert_message_if_new
    writer_pid: list[int] = []  # the paused write transaction's backend

    def paused_insert(*args: Any, **kwargs: Any) -> Any:
        inserted = original(*args, **kwargs)
        if not entered.is_set():
            writer_pid.append(holder_backend_pid(args[0].connection()))
            entered.set()
            release.wait(timeout=_WAIT_SECONDS)
        return inserted

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(gmail_adapter_module, "_insert_message_if_new", paused_insert)
        sync = _in_thread(lambda: world.sync(world.c, connector))
        try:
            assert entered.wait(timeout=_WAIT_SECONDS), "sync never reached its message write"
            disable = _in_thread(lambda: world.disable_email(world.c))
            _wait_until(
                lambda: _lock_waiters(_CASCADE_LOCK, holder_pid=writer_pid[0]) > 0,
                disable,
                "cascade waits",
            )
            removal = _in_thread(lambda: world.remove(actor=world.a, target=world.c))
            _wait_until(lambda: _membership_lock_waiters(world.ws) > 0, removal, "removal waits")
        finally:
            release.set()
        disable.join()
        removal.join()
        sync.join()

    assert disable.response.status_code == 200, disable.response.text
    assert removal.response.status_code == 200, removal.response.text
    assert sync.response.status_code in (201, 403), sync.response.text
    assert _gmail_rows(world) == _PURGED
    assert _connector_status(connector) == "disconnected"


_ROW_LOCK_OR_WRITE = re.compile(
    r"\bFOR (UPDATE|NO KEY UPDATE|SHARE|KEY SHARE)\b|^\s*(INSERT|UPDATE|DELETE)\b", re.IGNORECASE
)


@dataclass
class _Txn:
    statements: list[tuple[str, dict[str, Any]]] = field(default_factory=list)


@contextmanager
def _capture_transactions() -> Iterator[list[_Txn]]:
    done: list[_Txn] = []
    open_txns: dict[int, _Txn] = {}
    guard = threading.Lock()

    def on_begin(conn: Any) -> None:
        with guard:
            open_txns[id(conn)] = _Txn()

    def on_end(conn: Any) -> None:
        with guard:
            txn = open_txns.pop(id(conn), None)
            if txn is not None:
                done.append(txn)

    def on_execute(conn: Any, _cursor: Any, statement: str, params: Any, *_: Any) -> None:
        with guard:
            txn = open_txns.get(id(conn))
            if txn is not None:
                txn.statements.append((statement, params if isinstance(params, dict) else {}))

    listeners = (("begin", on_begin), ("commit", on_end), ("rollback", on_end))
    for name, fn in listeners:
        event.listen(engine, name, fn)
    event.listen(engine, "before_cursor_execute", on_execute)
    try:
        yield done
    finally:
        event.remove(engine, "before_cursor_execute", on_execute)
        for name, fn in listeners:
            event.remove(engine, name, fn)


def _kind(statement: str, params: dict[str, Any], ws: UUID) -> str | None:
    if "pg_advisory_xact_lock" in statement:
        if params.get("lock_key") == membership_mutation_lock_key(ws):
            return "membership_shared" if "_shared" in statement else "membership_exclusive"
        return "other_advisory"
    if re.search(r"FROM personal_domains.*FOR KEY SHARE", statement, re.DOTALL):
        return "consent_guard_domain"
    if re.search(r"FROM connector_accounts.*FOR KEY SHARE", statement, re.DOTALL):
        return "consent_guard"
    if re.search(r"FROM connector_accounts.*FOR UPDATE", statement, re.DOTALL):
        return "connector_for_update"
    if _ROW_LOCK_OR_WRITE.search(statement):
        return "row"
    return None


def test_lock_order_and_normal_flow(world: ConsentWorld) -> None:
    """Over real traffic (connect, a full sync with detection, then the
    disable): every transaction that takes the consent guard took the
    shared membership lock first, any other advisory (idempotency) lock
    before the guard, and no row lock or write before it; every Gmail
    write site took the guard; and the cascade locked the connector rows
    FOR UPDATE before its first purge. With consent active, sync and
    detection are unaffected (messages, evidence, run, recommendation)."""
    connector = world.connect(world.c, "c")
    with _capture_transactions() as txns:
        sync = world.sync(world.c, connector)
        assert sync.status_code == 201, sync.text
        assert sync.json()["status"] == "succeeded", sync.json()
        rows_after_sync = _gmail_rows(world)
        sync_evidence = _count("pkos_evidence", world.ws, "source_type = 'gmail_sync'")
        runs_after_sync = _detection_runs(world)
        detect_evidence = _count(
            "pkos_evidence", world.ws, "source_ref LIKE 'gmail:detect_action:%'"
        )
        disable = world.disable_email(world.c)
        assert disable.status_code == 200, disable.text

    # Normal flow unaffected.
    assert rows_after_sync["email_messages"] == 2
    assert sync_evidence >= 3
    assert rows_after_sync["recommendations"] >= 1
    assert runs_after_sync["ai_runs"] >= 1
    assert detect_evidence >= 1

    # Every Gmail write path passes its own connector: the guard's connector
    # statement never runs owner-wide (`only_id` NULL) in sync/detection.
    guard_binds = [
        params.get("only_id")
        for txn in txns
        for statement, params in txn.statements
        if _kind(statement, params, world.ws) == "consent_guard"
    ]
    assert guard_binds, "no consent guard ran"
    assert all(bind is not None for bind in guard_binds), guard_binds
    assert set(guard_binds) == {connector}, guard_binds

    guarded_writes: list[str] = []
    cascade_seen = False
    for txn in txns:
        kinds = [
            (kind, statement)
            for statement, params in txn.statements
            if (kind := _kind(statement, params, world.ws)) is not None
        ]
        names = [k for k, _ in kinds]
        if "consent_guard" in names:
            guard_at = names.index("consent_guard")
            assert names[0] == "membership_shared", names
            # The domain row lock immediately precedes the connector row lock.
            assert names[guard_at - 1] == "consent_guard_domain", names
            assert all(
                k in ("membership_shared", "other_advisory") for k in names[: guard_at - 1]
            ), names
            assert "other_advisory" not in names[guard_at:], names
            guarded_writes.extend(s for k, s in kinds[guard_at + 1 :] if k == "row")
        if any("DELETE FROM email_messages" in s for _, s in kinds):
            cascade_seen = True
            first_purge = next(
                i
                for i, (_, s) in enumerate(kinds)
                if s.lstrip().upper().startswith(("DELETE", "UPDATE RECOMMENDATIONS"))
                and "personal_domains" not in s
                and "domain_consents" not in s
                and "idempotency" not in s
            )
            assert "connector_for_update" in names[:first_purge], names

    assert cascade_seen, "the disable cascade never ran"
    written = " ".join(guarded_writes)
    for fragment in (
        "INSERT INTO email_threads",
        "INSERT INTO email_messages",
        "INSERT INTO pkos_nodes",
        "UPDATE email_messages SET body",
        "INSERT INTO pkos_evidence",
        "INSERT INTO ai_runs",
        "INSERT INTO recommendations",
    ):
        assert fragment in written, f"no consent-guarded transaction wrote {fragment}"
