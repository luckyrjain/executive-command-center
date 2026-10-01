"""Security Remediation Spec A S1.11 (T09): removal race protection.

A member being removed must not, concurrently, keep writing rows owned by
them. Member removal takes the membership-mutation advisory lock
EXCLUSIVELY (`identity/membership_removal.py`); the write paths below take
its SHARED side as the first lock of their transaction and re-check the
member's active membership under it:

- the Gmail OAuth callback's write transaction (-> 403 `MEMBERSHIP_INACTIVE`,
  refusal audit, minted grant revoked only when `revoke_is_safe`);
- connector sync phase 1 (-> `SyncSkipped` / 403 `MEMBERSHIP_INACTIVE`,
  nothing written) and phase 3;
- the Gmail sync's phase-2 writes (messages, person nodes, evidence,
  aliases -- plan note N15(1)), which stop the sync;
- ownership transfer to a member (plan note N15(2)) -> 404
  `RECIPIENT_NOT_FOUND`, no ownership change.

Concurrency is real (two connections / request threads) and deterministic:
removal is paused *inside* its locked section (or a writer is paused inside
its own), and the other side is observed waiting on the advisory lock in
`pg_locks` before the pause is released. Every wait is bounded.
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
from sqlalchemy import event, text

import ecc.domains.ai_runtime.runtime as runtime_module
import ecc.domains.engineering.connector_accounts as connector_accounts_module
import ecc.domains.governance.recommendation_mutations as recommendation_mutations_module
import ecc.domains.identity.membership_removal as membership_removal_module
import ecc.domains.personal.gmail_action_detection as gmail_action_detection_module
import ecc.domains.personal.gmail_adapter as gmail_adapter_module
import ecc.domains.personal.gmail_oauth as gmail_oauth_module
from ecc import observability
from ecc.config import get_settings
from ecc.database import engine
from ecc.domains.personal.gmail_adapter import resolve_or_create_person
from ecc.platform.connector_security import MembershipInactiveError, membership_mutation_lock_key

pytestmark = pytest.mark.skipif(
    not get_settings().database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)

_FLAG_ON = {"ECC_PERSONAL_DATA_ISOLATION": "true"}
_EXTRA_CLEANUP_TABLES = ("ownership_transfers", "member_notifications", "incidents")
_WAIT_SECONDS = 15
_MEMBERSHIP_INACTIVE_RUN_SUMMARY = "sync stopped: workspace membership is no longer active"


# --- world -------------------------------------------------------------------


@dataclass
class RaceWorld:
    """One workspace: A (`owner`, the original user) and C (`member`, the
    one being removed), with the Gmail sync harness active."""

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
                google_email=f"t09-{key}-{self.suffix}@example.test",
                messages=tuple(
                    FakeGmailMessage(
                        external_message_id=f"t09-{key}-{i}-{self.suffix}",
                        external_thread_id=f"t09-{key}-thread-{i}",
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

    def start_oauth(self, user_id: UUID, key: str, mailbox: FakeMailbox) -> tuple[str, str]:
        """Enables `email` consent and starts OAuth; returns `(code, state)`."""
        client, token = self.client(user_id)
        code = self.fake_google.register(key, mailbox)
        # `/oauth/start` checks the caller's own email, the callback the
        # authorized mailbox's: both must be allowlisted.
        self.harness._allow(self.member_email(user_id))
        self.harness._allow(mailbox.google_email)
        enable = client.post(
            "/api/v1/personal/domains",
            headers=csrf_headers(token, str(uuid4())),
            json={"domain_key": "email"},
        )
        assert enable.status_code == 201, enable.text
        start = client.post("/api/v1/personal/gmail/oauth/start", headers=csrf_headers(token))
        assert start.status_code == 200, start.text
        return code, httpx.URL(start.json()["authorization_url"]).params["state"]

    def callback(self, user_id: UUID, code: str, state: str) -> httpx.Response:
        client, _token = self.client(user_id)
        return client.get(
            "/api/v1/personal/gmail/oauth/callback", params={"code": code, "state": state}
        )

    def connect(self, user_id: UUID, key: str) -> UUID:
        code, state = self.start_oauth(user_id, key, self.mailbox(key))
        response = self.callback(user_id, code, state)
        assert response.status_code == 200, response.text
        return UUID(response.json()["id"])

    def sync(self, user_id: UUID, connector_id: UUID) -> httpx.Response:
        client, token = self.client(user_id)
        return client.post(
            f"/api/v1/engineering/connectors/{connector_id}/sync",
            headers=csrf_headers(token, str(uuid4())),
            json={"run_type": "backfill", "resource_type": "message"},
        )

    def remove(self, *, actor: UUID, target: UUID) -> httpx.Response:
        client, token = self.client(actor)
        return client.delete(
            f"/api/v1/identity/workspaces/{self.ws}/members/{target}",
            headers=csrf_headers(token),
        )

    def member_email(self, user_id: UUID) -> str:
        key = "a" if user_id == self.a else "c"
        return f"t09-member-{key}-{self.suffix}@example.test"

    def account_id(self, key: str) -> UUID:
        return self.account_ids[key]

    def create_incident(self, user_id: UUID) -> UUID:
        client, token = self.client(user_id)
        response = client.post(
            "/api/v1/engineering/incidents",
            json={"title": "T09", "severity": "high", "detected_at": datetime.now(UTC).isoformat()},
            headers=csrf_headers(token, str(uuid4())),
        )
        assert response.status_code == 201, response.text
        return UUID(response.json()["id"])

    def transfer(self, actor: UUID, incident_id: UUID, to_account_id: UUID) -> httpx.Response:
        client, token = self.client(actor)
        return client.post(
            "/api/v1/ownership/transfers",
            json={
                "resource_type": "incidents",
                "resource_id": str(incident_id),
                "to_account_id": str(to_account_id),
            },
            headers=csrf_headers(token, str(uuid4())),
        )


@contextmanager
def _race_world(env: dict[str, str] | None) -> Iterator[RaceWorld]:
    suffix = uuid4().hex[:10]
    ws, a, c = uuid4(), uuid4(), uuid4()
    now = datetime.now(UTC)
    account_ids: dict[str, UUID] = {}
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, 'T09 removal races', 'UTC', :now)"
            ),
            {"id": ws, "now": now},
        )
        for key, user_id, role, offset in (("a", a, "owner", 0), ("c", c, "member", 1)):
            account_ids[key] = create_identity(
                connection,
                workspace_id=ws,
                user_id=user_id,
                email=f"t09-member-{key}-{suffix}@example.test",
                now=now + timedelta(seconds=offset),
                role=role,
            )
    try:
        with gmail_sync_harness(env=env) as harness:
            yield RaceWorld(
                ws=ws, a=a, c=c, account_ids=account_ids, harness=harness, suffix=suffix
            )
    finally:
        cleanup_workspace(
            ws, list(account_ids.values()), extra_cleanup_tables=_EXTRA_CLEANUP_TABLES
        )


@pytest.fixture
def world() -> Iterator[RaceWorld]:
    with _race_world(_FLAG_ON) as built:
        yield built


@pytest.fixture
def world_off() -> Iterator[RaceWorld]:
    with _race_world(None) as built:
        yield built


# --- concurrency helpers -------------------------------------------------------


@dataclass
class _Call:
    """A request running on its own thread; `response` once finished."""

    thread: threading.Thread
    result: dict[str, httpx.Response]

    @property
    def response(self) -> httpx.Response:
        return self.result["response"]

    def join(self) -> None:
        self.thread.join(timeout=_WAIT_SECONDS)
        assert not self.thread.is_alive(), "request thread did not finish"


def _in_thread(call: Callable[[], httpx.Response]) -> _Call:
    result: dict[str, httpx.Response] = {}

    def run() -> None:
        result["response"] = call()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return _Call(thread=thread, result=result)


def _membership_lock_waiters(ws: UUID) -> int:
    """Backends waiting (not granted) on `membership_mutation_lock_key(ws)`."""
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


def _wait_for_membership_lock_waiter(ws: UUID, call: _Call) -> None:
    deadline = time.monotonic() + _WAIT_SECONDS
    while time.monotonic() < deadline:
        if _membership_lock_waiters(ws) > 0:
            return
        assert call.thread.is_alive(), f"finished without waiting: {call.result}"
        time.sleep(0.02)
    raise AssertionError("never waited on the membership advisory lock")


@dataclass
class _PausedRemoval:
    call: _Call
    release: threading.Event


@contextmanager
def _removal_paused_in_locked_section(
    world: RaceWorld, *, actor: UUID, target: UUID
) -> Iterator[_PausedRemoval]:
    """Runs the REAL removal endpoint on its own thread and pauses it inside
    its transaction, after it took the exclusive membership lock and marked
    nothing yet committed (its `cancel_runs_for_removed_member` step). Set
    `release` to let it finish and commit; always released and joined on
    exit."""
    entered, release = threading.Event(), threading.Event()
    original = membership_removal_module.cancel_runs_for_removed_member

    def paused(*args: Any, **kwargs: Any) -> Any:
        entered.set()
        release.wait(timeout=_WAIT_SECONDS)
        return original(*args, **kwargs)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(membership_removal_module, "cancel_runs_for_removed_member", paused)
        call = _in_thread(lambda: world.remove(actor=actor, target=target))
        try:
            assert entered.wait(timeout=_WAIT_SECONDS), "removal never reached its locked section"
            yield _PausedRemoval(call=call, release=release)
        finally:
            release.set()
            call.thread.join(timeout=_WAIT_SECONDS)
    assert not call.thread.is_alive()


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


def _membership_status(ws: UUID, users_id: UUID) -> str:
    return str(
        _scalar(
            "SELECT status FROM workspace_memberships WHERE workspace_id = :ws AND users_id = :u",
            ws=ws,
            u=users_id,
        )
    )


def _error_code(response: httpx.Response) -> str:
    return str(response.json()["error"]["code"])


def _refused_count() -> float:
    return observability.connector_enrollment_refused_total._values.get(
        ("gmail", "membership_inactive"), 0.0
    )


def _revoke_count(result: str) -> float:
    return observability.connector_revoke_total._values.get(
        ("gmail", "callback_failure", result), 0.0
    )


def _refusal_audits(ws: UUID) -> list[dict[str, Any]]:
    with engine.connect() as connection:
        return [
            dict(row)
            for row in connection.execute(
                text(
                    "SELECT aggregate_type, authorization_result, failure_code, metadata, "
                    "actor_id FROM audit_events WHERE workspace_id = :ws "
                    "AND event_type = 'connector_account.enrollment_refused'"
                ),
                {"ws": ws},
            ).mappings()
        ]


def _knowledge_rows_owned_by(ws: UUID, users_id: UUID) -> dict[str, int]:
    return {
        table: _count(table, ws, "owner_id = :u", u=users_id)
        for table in ("pkos_nodes", "pkos_evidence", "entity_aliases")
    }


# --- Gmail OAuth callback ----------------------------------------------------------


@pytest.mark.parametrize("flag_on", [True, False])
def test_callback_waits_for_removal_then_refuses_membership_inactive(flag_on: bool) -> None:
    """Removal of C holds the exclusive lock (paused mid-transaction); C's
    OAuth callback -- whose entry role check already passed -- blocks on the
    shared lock before writing. Once removal commits, the callback re-checks
    C's membership and refuses: 403 MEMBERSHIP_INACTIVE, no connector row,
    a `denied` refusal audit, and C's freshly minted grant revoked (no live
    row anywhere uses that Google account, so the revoke is safe)."""
    with _race_world(_FLAG_ON if flag_on else None) as world:
        code, state = world.start_oauth(world.c, "c", world.mailbox("c"))
        refused_before, ok_before = _refused_count(), _revoke_count("ok")

        with _removal_paused_in_locked_section(world, actor=world.a, target=world.c) as removal:
            callback = _in_thread(lambda: world.callback(world.c, code, state))
            _wait_for_membership_lock_waiter(world.ws, callback)
            assert "response" not in callback.result
            removal.release.set()
            callback.join()
            removal.call.join()

        assert removal.call.response.status_code == 200, removal.call.response.text
        assert callback.response.status_code == 403, callback.response.text
        assert _error_code(callback.response) == "MEMBERSHIP_INACTIVE"
        assert _membership_status(world.ws, world.c) == "removed"
        assert _count("connector_accounts", world.ws) == 0
        audits = _refusal_audits(world.ws)
        assert len(audits) == 1
        assert audits[0]["aggregate_type"] == "connector_account_enrollment"
        assert audits[0]["authorization_result"] == "denied"
        assert audits[0]["failure_code"] == "membership_inactive"
        assert audits[0]["metadata"] == {"reason": "membership_inactive", "provider": "gmail"}
        assert audits[0]["actor_id"] == world.c
        assert _refused_count() == refused_before + 1
        assert world.fake_google.revoked_tokens == [FakeGoogle.refresh_token("c")]
        assert _revoke_count("ok") == ok_before + 1


def test_callback_after_removal_never_revokes_another_members_live_grant(
    world: RaceWorld,
) -> None:
    """C re-authorizes the Google account A already connected (so C's minted
    token IS A's grant). C is removed -- by the real removal endpoint --
    while the OAuth token exchange is in flight. The callback refuses with
    403 MEMBERSHIP_INACTIVE (the membership re-check runs before any
    conflict handling), leaves A's row untouched and does not revoke the
    grant: A's row is live, so `revoke_is_safe` refuses."""
    a_connector = world.connect(world.a, "a")
    a_row_before = _scalar(
        "SELECT row_to_json(c)::text FROM connector_accounts c WHERE id = :id", id=a_connector
    )
    code, state = world.start_oauth(world.c, "c", world.mailbox("a"))
    removal_result: dict[str, httpx.Response] = {}
    adapter = gmail_oauth_module._adapter
    original = adapter.handle_oauth_callback

    def remove_during_exchange(*args: Any, **kwargs: Any) -> Any:
        authorization = original(*args, **kwargs)
        removal = _in_thread(lambda: world.remove(actor=world.a, target=world.c))
        removal.join()
        removal_result["response"] = removal.response
        return authorization

    skipped_before = _revoke_count("skipped_unsafe")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(adapter, "handle_oauth_callback", remove_during_exchange)
        callback = world.callback(world.c, code, state)

    assert removal_result["response"].status_code == 200, removal_result["response"].text
    assert callback.status_code == 403, callback.text
    assert _error_code(callback) == "MEMBERSHIP_INACTIVE"
    assert world.fake_google.revoked_tokens == []
    assert _revoke_count("skipped_unsafe") == skipped_before + 1
    assert (
        _scalar(
            "SELECT row_to_json(c)::text FROM connector_accounts c WHERE id = :id", id=a_connector
        )
        == a_row_before
    )
    assert _count("connector_accounts", world.ws) == 1


def test_callback_demoted_during_exchange_refuses_insufficient_role(world: RaceWorld) -> None:
    """ADR-0014: the callback's only role gate (`require_role_action`) runs
    before the OAuth round trip. C is demoted to viewer while the token
    exchange is in flight; the persist transaction re-checks the role under
    the membership lock and refuses: 403 INSUFFICIENT_ROLE, no connector
    row, a `denied` refusal audit, and C's minted grant revoked."""
    code, state = world.start_oauth(world.c, "c", world.mailbox("c"))
    adapter = gmail_oauth_module._adapter
    original = adapter.handle_oauth_callback

    def demote_during_exchange(*args: Any, **kwargs: Any) -> Any:
        authorization = original(*args, **kwargs)
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE workspace_memberships SET role = 'viewer' "
                    "WHERE workspace_id = :ws AND users_id = :u"
                ),
                {"ws": world.ws, "u": world.c},
            )
        return authorization

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(adapter, "handle_oauth_callback", demote_during_exchange)
        callback = world.callback(world.c, code, state)

    assert callback.status_code == 403, callback.text
    assert _error_code(callback) == "INSUFFICIENT_ROLE"
    assert _count("connector_accounts", world.ws) == 0
    audits = _refusal_audits(world.ws)
    assert len(audits) == 1
    assert audits[0]["failure_code"] == "insufficient_role"
    assert audits[0]["metadata"] == {"reason": "insufficient_role", "provider": "gmail"}
    assert world.fake_google.revoked_tokens == [FakeGoogle.refresh_token("c")]


def test_callback_membership_check_failure_still_revokes_the_minted_grant(
    world: RaceWorld,
) -> None:
    """A failure of the lock/re-check statement itself (not a refusal) is an
    ordinary persist failure: nothing written, the minted grant queued for
    the always-drain revoke like every other failure in the callback."""
    code, state = world.start_oauth(world.c, "c", world.mailbox("c"))

    def broken(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("lock statement failed")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(gmail_oauth_module, "require_active_members_locked", broken)
        with pytest.raises(RuntimeError):
            world.callback(world.c, code, state)

    assert _count("connector_accounts", world.ws) == 0
    assert world.fake_google.revoked_tokens == [FakeGoogle.refresh_token("c")]


# --- connector sync ------------------------------------------------------------------


def test_sync_phase1_waits_for_removal_then_is_skipped(world: RaceWorld) -> None:
    """C's sync blocks on the shared lock while C's removal (which also
    disconnects C's Gmail connector under the flag) holds it exclusively;
    once removal commits, phase 1 re-checks C's membership and skips: 403
    MEMBERSHIP_INACTIVE, no sync run, no cursor, no mail or knowledge rows."""
    connector = world.connect(world.c, "c")
    runs_before = _count("sync_runs", world.ws)

    with _removal_paused_in_locked_section(world, actor=world.a, target=world.c) as removal:
        sync = _in_thread(lambda: world.sync(world.c, connector))
        _wait_for_membership_lock_waiter(world.ws, sync)
        assert "response" not in sync.result
        removal.release.set()
        sync.join()
        removal.call.join()

    assert removal.call.response.status_code == 200, removal.call.response.text
    assert sync.response.status_code == 403, sync.response.text
    assert _error_code(sync.response) == "MEMBERSHIP_INACTIVE"
    assert _count("sync_runs", world.ws) == runs_before
    assert _count("sync_cursors", world.ws) == 0
    assert _count("email_messages", world.ws) == 0
    assert _knowledge_rows_owned_by(world.ws, world.c) == {
        "pkos_nodes": 0,
        "pkos_evidence": 0,
        "entity_aliases": 0,
    }


def test_sync_of_a_removed_owners_personal_connector_is_skipped(world_off: RaceWorld) -> None:
    """Flag off, a personal connector is workspace-visible, so another member
    (A, active) may sync it. Its owner C is no longer active: phase 1 checks
    the personal connector's OWNER too, and skips with nothing written."""
    world = world_off
    connector = world.connect(world.c, "c")
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE workspace_memberships SET status = 'removed', removed_at = now() "
                "WHERE workspace_id = :ws AND users_id = :u"
            ),
            {"ws": world.ws, "u": world.c},
        )
    version_before = _scalar("SELECT version FROM connector_accounts WHERE id = :id", id=connector)

    response = world.sync(world.a, connector)

    assert response.status_code == 403, response.text
    assert _error_code(response) == "MEMBERSHIP_INACTIVE"
    assert _count("sync_runs", world.ws) == 0
    assert _count("email_messages", world.ws) == 0
    assert (
        _scalar("SELECT version FROM connector_accounts WHERE id = :id", id=connector)
        == version_before
    )


def test_phase2_writes_stop_once_removal_commits(world: RaceWorld) -> None:
    """N15(1): C's sync is in phase 2 -- the first message row is written and
    participant resolution is about to start -- when C's removal (the real
    endpoint) commits. The next write re-checks C's membership under the
    shared lock and stops the sync: no person node, evidence or alias owned
    by C; phase 3 closes the reserved run as failed and returns 403."""
    connector = world.connect(world.c, "c")
    removal_result: dict[str, httpx.Response] = {}
    original = gmail_adapter_module.resolve_or_create_person

    def remove_then_resolve(**kwargs: Any) -> UUID:
        if "response" not in removal_result:
            removal = _in_thread(lambda: world.remove(actor=world.a, target=world.c))
            removal.join()
            removal_result["response"] = removal.response
        return original(**kwargs)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(gmail_adapter_module, "resolve_or_create_person", remove_then_resolve)
        response = world.sync(world.c, connector)

    assert removal_result["response"].status_code == 200, removal_result["response"].text
    assert response.status_code == 403, response.text
    assert _error_code(response) == "MEMBERSHIP_INACTIVE"
    assert _knowledge_rows_owned_by(world.ws, world.c) == {
        "pkos_nodes": 0,
        "pkos_evidence": 0,
        "entity_aliases": 0,
    }
    assert _count("pkos_nodes", world.ws) == 0
    # Only the first message (committed before the removal) exists.
    assert _count("email_messages", world.ws) == 1
    # The participant loop re-raised: the sync stopped at message 0 and
    # never fetched message 1 (a `continue` there would fetch it and only
    # stop at that message's own locked write).
    assert _message_fetches(world, "metadata") == [world.mailbox("c").messages[0]]
    runs = _scalar(
        "SELECT json_agg(json_build_array(status, error_summary))::text FROM sync_runs "
        "WHERE connector_account_id = :id",
        id=connector,
    )
    assert runs == f'[["failed", "{_MEMBERSHIP_INACTIVE_RUN_SUMMARY}"]]'
    assert _count("sync_cursors", world.ws) == 0
    assert (
        _count(
            "audit_events",
            world.ws,
            "event_type IN ('connector_account.synced', 'connector_account.sync_failed')",
        )
        == 0
    )


def test_resolve_or_create_person_refuses_a_removed_owner(world: RaceWorld) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE workspace_memberships SET status = 'removed', removed_at = now() "
                "WHERE workspace_id = :ws AND users_id = :u"
            ),
            {"ws": world.ws, "u": world.c},
        )
    with pytest.raises(MembershipInactiveError):
        resolve_or_create_person(
            workspace_id=world.ws,
            owner_id=world.c,
            email=f"someone-{world.suffix}@partner.test",
            display_name="Someone",
            source_ref="gmail:t09",
            now=datetime.now(UTC),
            # No connector in this world; the membership re-check refuses first.
            connector_account_id=None,
        )
    assert _count("pkos_nodes", world.ws) == 0
    assert _count("entity_aliases", world.ws) == 0


# --- Gmail action detection -------------------------------------------------------


def _remove_c_once(world: RaceWorld, result: dict[str, httpx.Response]) -> None:
    """Runs the real removal of C (by A) on its own thread, once, and waits
    for it -- called from inside C's in-flight request."""
    if "response" not in result:
        removal = _in_thread(lambda: world.remove(actor=world.a, target=world.c))
        removal.join()
        result["response"] = removal.response


class _RemovingOllama:
    """Wraps the harness's fake Ollama adapter: the model call first lets
    C's removal commit, then answers normally."""

    def __init__(self, inner: Any, trigger: Callable[[], None]) -> None:
        self._inner = inner
        self._trigger = trigger

    def generate(self, *args: Any, **kwargs: Any) -> Any:
        self._trigger()
        return self._inner.generate(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def test_detection_run_and_recommendation_not_written_after_removal_during_model_call(
    world: RaceWorld,
) -> None:
    """A-2: C's post-sync action detection is inside the model call when C's
    removal commits. The run persist re-checks C under the shared lock and
    stops: no `ai_runs`/`ai_run_steps` and no recommendation owned by C;
    detection is best-effort, so the (already recorded) sync still returns
    201."""
    connector = world.connect(world.c, "c")
    removal_result: dict[str, httpx.Response] = {}
    fake_ollama = runtime_module.OllamaAdapter()
    wrapped = _RemovingOllama(fake_ollama, lambda: _remove_c_once(world, removal_result))

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(runtime_module, "OllamaAdapter", lambda *_a, **_k: wrapped)
        response = world.sync(world.c, connector)

    assert removal_result["response"].status_code == 200, removal_result["response"].text
    assert response.status_code == 201, response.text
    assert _membership_status(world.ws, world.c) == "removed"
    assert _count("ai_runs", world.ws, "owner_id = :u", u=world.c) == 0
    assert _count("ai_run_steps", world.ws, "owner_id = :u", u=world.c) == 0
    assert _count("recommendations", world.ws, "owner_id = :u", u=world.c) == 0


def test_detection_recommendation_not_written_after_removal_before_insert(
    world: RaceWorld,
) -> None:
    """A-2: the run persisted while C was active (a retained personal row);
    C's removal commits inside `create_recommendation` BEFORE its locked
    membership and role check -- via `request_hash`, which runs first. That
    check sees the committed removal and refuses, and no recommendation is
    written."""
    connector = world.connect(world.c, "c")
    removal_result: dict[str, httpx.Response] = {}
    original = recommendation_mutations_module.request_hash

    def remove_then_hash(*args: Any, **kwargs: Any) -> Any:
        _remove_c_once(world, removal_result)
        return original(*args, **kwargs)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(recommendation_mutations_module, "request_hash", remove_then_hash)
        response = world.sync(world.c, connector)

    assert removal_result["response"].status_code == 200, removal_result["response"].text
    assert response.status_code == 201, response.text
    assert _count("ai_runs", world.ws, "owner_id = :u", u=world.c) == 1
    assert _count("recommendations", world.ws) == 0


def _message_fetches(world: RaceWorld, fmt: str) -> list[FakeGmailMessage]:
    """C's mailbox messages fetched from the fake Gmail API with
    `format=<fmt>`, in request order."""
    by_id = {m.external_message_id: m for m in world.mailbox("c").messages}
    prefix = "/gmail/v1/users/me/messages/"
    return [
        by_id[r.url.path.removeprefix(prefix)]
        for r in world.fake_google.requests
        if r.url.path.startswith(prefix)
        and r.url.params.get("format") == fmt
        and r.url.path.removeprefix(prefix) in by_id
    ]


def test_detection_evidence_not_written_after_removal_before_evidence_insert(
    world: RaceWorld,
) -> None:
    """B-1 (round 3): C's removal commits after detection resolved the
    sender (`resolve_or_create_person` returned) and before the
    detect-action evidence insert. Only that insert's own locked re-check
    can refuse: no `gmail:detect_action:*` evidence is written, and the
    batch stops (no further body fetch, run or recommendation)."""
    connector = world.connect(world.c, "c")
    removal_result: dict[str, httpx.Response] = {}
    original = gmail_action_detection_module.resolve_or_create_person

    def resolve_then_remove(**kwargs: Any) -> UUID:
        node_id = original(**kwargs)
        _remove_c_once(world, removal_result)
        return node_id

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(gmail_action_detection_module, "resolve_or_create_person", resolve_then_remove)
        response = world.sync(world.c, connector)

    assert removal_result["response"].status_code == 200, removal_result["response"].text
    assert response.status_code == 201, response.text
    assert _count("pkos_evidence", world.ws, "source_ref LIKE 'gmail:detect_action:%'") == 0
    assert _count("ai_runs", world.ws) == 0
    assert _count("recommendations", world.ws) == 0
    assert len(_message_fetches(world, "full")) == 1


def test_body_not_stored_when_owner_removed_between_fetch_and_update() -> None:
    """B-3: `fetch_and_store_body` -- C's removal commits after the Gmail
    GET and before the body UPDATE: returns None, the body stays NULL."""
    env = {**_FLAG_ON, "ECC_EMAIL_ACTION_DETECTION_ENABLED": "false"}
    with _race_world(env) as world:
        connector = world.connect(world.c, "c")
        sync = world.sync(world.c, connector)
        assert sync.status_code == 201, sync.text
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
        removal_result: dict[str, httpx.Response] = {}
        original = gmail._request_with_rate_limit_retry

        def get_then_remove(*args: Any, **kwargs: Any) -> Any:
            fetched = original(*args, **kwargs)
            _remove_c_once(world, removal_result)
            return fetched

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(gmail, "_request_with_rate_limit_retry", get_then_remove)
            stored = gmail.fetch_and_store_body(
                workspace_id=world.ws,
                message_id=UUID(message_id),
                external_message_id=external_id,
                headers={"Authorization": f"Bearer {FakeGoogle.access_token('c')}"},
            )

        assert removal_result["response"].status_code == 200, removal_result["response"].text
        assert stored is None
        assert (
            _scalar("SELECT body IS NULL FROM email_messages WHERE id = :id", id=message_id) is True
        )


# --- ownership transfer --------------------------------------------------------------


def test_transfer_to_a_member_being_removed_is_refused(world: RaceWorld) -> None:
    """N15(2): A transfers an incident to C while C's removal holds the
    exclusive lock. The transfer waits on the shared lock and, once removal
    commits, finds C inactive: 404 RECIPIENT_NOT_FOUND, owner unchanged, no
    transfer row (without the lock, the transfer committed after removal's
    owned-resources check and left the incident owned by a removed member)."""
    incident = world.create_incident(world.a)

    with _removal_paused_in_locked_section(world, actor=world.a, target=world.c) as removal:
        transfer = _in_thread(lambda: world.transfer(world.a, incident, world.account_id("c")))
        _wait_for_membership_lock_waiter(world.ws, transfer)
        assert "response" not in transfer.result
        removal.release.set()
        transfer.join()
        removal.call.join()

    assert removal.call.response.status_code == 200, removal.call.response.text
    assert transfer.response.status_code == 404, transfer.response.text
    assert _error_code(transfer.response) == "RECIPIENT_NOT_FOUND"
    assert _scalar("SELECT owner_id FROM incidents WHERE id = :id", id=incident) == world.a
    assert _count("ownership_transfers", world.ws) == 0


# --- lock ordering ---------------------------------------------------------------------


def test_removal_waits_for_an_in_flight_sync_phase1_without_deadlock(world: RaceWorld) -> None:
    """Lock ordering (membership -> idempotency -> rows): C's sync is paused
    inside phase 1 holding the shared membership lock, the idempotency lock
    and C's connector row lock. Removal of C (which then wants the same
    connector row) waits on the membership lock -- not the row -- so there
    is no cycle: releasing the sync lets phase 1 commit, removal proceeds and
    disconnects the connector, and the sync's later writes see C removed."""
    connector = world.connect(world.c, "c")
    gmail = connector_accounts_module.connector_registry.get("gmail")
    assert gmail is not None
    entered, release = threading.Event(), threading.Event()
    original = gmail.ensure_fresh_credential

    def paused(credential: str, **kwargs: Any) -> str:
        entered.set()
        release.wait(timeout=_WAIT_SECONDS)
        return original(credential, **kwargs)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(gmail, "ensure_fresh_credential", paused)
        sync = _in_thread(lambda: world.sync(world.c, connector))
        try:
            assert entered.wait(timeout=_WAIT_SECONDS), "sync never reached phase 1"
            removal = _in_thread(lambda: world.remove(actor=world.a, target=world.c))
            _wait_for_membership_lock_waiter(world.ws, removal)
            assert "response" not in removal.result
        finally:
            release.set()
        sync.join()
        removal.join()

    assert removal.response.status_code == 200, removal.response.text
    assert sync.response.status_code == 403, sync.response.text
    assert _error_code(sync.response) == "MEMBERSHIP_INACTIVE"
    assert _scalar("SELECT status FROM connector_accounts WHERE id = :id", id=connector) == (
        "disconnected"
    )
    assert _knowledge_rows_owned_by(world.ws, world.c) == {
        "pkos_nodes": 0,
        "pkos_evidence": 0,
        "entity_aliases": 0,
    }
    assert _count("sync_runs", world.ws, "status = 'running'") == 0


_ROW_LOCK_OR_WRITE = re.compile(r"\bFOR UPDATE\b|^\s*(INSERT|UPDATE|DELETE)\b", re.IGNORECASE)


@dataclass
class _Txn:
    statements: list[tuple[str, dict[str, Any]]] = field(default_factory=list)


@contextmanager
def _capture_transactions() -> Iterator[list[_Txn]]:
    """Every transaction run on `ecc.database.engine` while active, as its
    ordered statements with their bound parameters."""
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
        is_membership = params.get("lock_key") == membership_mutation_lock_key(ws)
        if not is_membership:
            return "idempotency_or_other_lock"
        return "membership_shared" if "_shared" in statement else "membership_exclusive"
    if _ROW_LOCK_OR_WRITE.search(statement):
        return "row"
    return None


def test_every_membership_locked_transaction_takes_that_lock_first(world: RaceWorld) -> None:
    """Static lock-ordering evidence over real traffic: callback, sync (all
    three phases, including the Gmail phase-2 writes), transfer and removal.
    In every transaction that takes the membership lock, it is taken before
    any other advisory (idempotency) lock and before any row lock or write;
    and each adopter was actually observed taking it."""
    incident = world.create_incident(world.a)
    with _capture_transactions() as txns:
        connector = world.connect(world.c, "c")
        sync = world.sync(world.c, connector)
        assert sync.status_code == 201, sync.text
        assert sync.json()["status"] == "succeeded"
        transfer = world.transfer(world.a, incident, world.account_id("c"))
        assert transfer.status_code == 201, transfer.text
        transfer_back = world.transfer(world.c, incident, world.account_id("a"))
        assert transfer_back.status_code == 201, transfer_back.text
        removal = world.remove(actor=world.a, target=world.c)
        assert removal.status_code == 200, removal.text

    locked: list[tuple[str, list[tuple[str, str]]]] = []
    for txn in txns:
        kinds = [
            (kind, statement)
            for statement, params in txn.statements
            if (kind := _kind(statement, params, world.ws)) is not None
        ]
        membership = [i for i, (k, _) in enumerate(kinds) if k.startswith("membership_")]
        if not membership:
            continue
        assert membership[0] == 0, [k for k, _ in kinds]
        locked.append((kinds[0][0], kinds))

    writes = " ".join(s for _, kinds in locked for _, s in kinds)
    shared = [kinds for first, kinds in locked if first == "membership_shared"]
    exclusive = [kinds for first, kinds in locked if first == "membership_exclusive"]
    assert exclusive, "removal never took the exclusive membership lock"
    for fragment in (
        "INSERT INTO connector_accounts",  # OAuth callback
        "INSERT INTO sync_runs",  # sync phase 1
        "INSERT INTO email_messages",  # Gmail phase 2
        "INSERT INTO pkos_nodes",  # participant resolution
        "INSERT INTO ownership_transfers",  # transfer
    ):
        assert fragment in writes, f"no membership-locked transaction wrote {fragment}"
    # Sync phase 1: membership lock, THEN the idempotency lock, then rows.
    phase1 = [kinds for kinds in shared if any("INSERT INTO sync_runs" in s for _, s in kinds)]
    assert phase1
    for kinds in phase1:
        order = [k for k, _ in kinds]
        assert order[:2] == ["membership_shared", "idempotency_or_other_lock"], order
