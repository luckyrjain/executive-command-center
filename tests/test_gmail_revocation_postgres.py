"""Phase 10 Task 7's consent revocation cascade
(`docs/superpowers/plans/2026-08-04-phase-10-gmail-connector.md` Task 7,
`ecc.domains.personal.gmail_revocation`), against a real Postgres database
and the real `TestClient`-driven HTTP endpoints -- `POST /api/v1/personal/
domains/email/disable`, `POST /api/v1/personal/consents/{id}/revoke`, and
`POST /api/v1/personal/domains/email/delete` (`ecc.domains.personal.
domains`/`export_deletion`).

Covers, in the same order these tests physically appear below:
1. Disabling the `email` domain disconnects the Gmail connector account
   (best-effort revoke attempted) and purges `email_threads`/`email_
   messages`, `attention_items` (`entity_type='email_thread'`), non-
   `executed` `email_action_detected` recommendations, and their
   `gmail_sync`-sourced `pkos_evidence` -- all in the same request.
2. An already-`executed` recommendation survives disable, but is redacted
   in place (`rationale`/`proposed_action`/`evidence_ids` nulled), proving
   the recommendation's own audit trail (and its still-independent
   `target_id`) is not destroyed.
3. `pkos_nodes` (the resolved person entity) survives disable -- only the
   `gmail_sync`-sourced `pkos_evidence` row pointing at it is removed.
4. `revoke_consent_endpoint` reaches the identical cascade via the same
   `_disable_domain` write path (`domains.py`'s own "one write path per
   transition" convention).
5. `delete_domain_endpoint` reaches the identical cascade too.
6. Disabling an already-disabled `email` domain (idempotent replay via a
   fresh request, not the same `Idempotency-Key`) does not re-run the
   cascade or double-disconnect an already-disconnected connector.
7. Disabling a non-`email` domain (`habits`) is completely unaffected --
   no regression to every other domain's own "data retained on disable"
   contract.
8. Idempotency-key replay of `disable_domain_endpoint` returns the cached
   response without a second cascade run.
9. Reusing the *same* `Idempotency-Key` for `disable_domain_endpoint`
   across a real disable -> re-enable -> disable-again sequence is
   rejected (`409 IDEMPOTENCY_CONFLICT`), not silently served item 8's own
   stale cached "success" -- Loop 2 round 21 review finding (MEDIUM-HIGH):
   `req_hash` for this endpoint depends only on `domain_key`, and
   `idempotency_records` rows live 365 days, so replaying the first call's
   key after a real re-enable used to return the original, now-stale
   `enabled: false` response without ever re-running `cascade_email_
   revocation` -- the caller was told the disable succeeded while the
   freshly-reconnected Gmail account and freshly-synced data were
   untouched.
10. The identical fix for `delete_domain_endpoint`, same mechanism and
    same severity as item 9 above.
11. An owner with *two* simultaneously-active `gmail` connector accounts
    (no per-owner uniqueness on `provider = 'gmail'` -- only `external_
    account_id` is unique) has both disconnected by one cascade run --
    Loop 2 round 1 review finding: the original `.one_or_none()` raised
    `MultipleResultsFound` here, aborting the entire cascade.
12. `domains.py:_disable_domain`'s `session.close()` before `finish_
    gmail_revocation(...)` actually releases the pooled connection and the
    cascade's own `FOR UPDATE` lock before the (potentially slow) Google
    revoke call runs -- Loop 2 round 4 review finding: `connector_
    accounts.py:disable_connector_endpoint`'s own identical pattern has a
    dedicated concurrency test proving this; the two call sites that
    actually reach `finish_gmail_revocation` for `gmail` (this module's
    own `_disable_domain`/`delete_domain_endpoint`) had none.
13. `export_deletion.py:delete_domain_endpoint`'s own identical `session.
    close()` before `finish_gmail_revocation(...)` sequence releases the
    pooled connection and row lock before the blocking Google revoke call
    runs, same as item 12 above -- Loop 2 round 5 review finding: item 12
    only ever covered `disable_domain_endpoint`, leaving the OTHER call
    site that reaches `finish_gmail_revocation` for `gmail` unproven.
14. A second owner's own Gmail data (thread/message/attention item/
    connector) in the SAME workspace is completely untouched by the first
    owner's disable -- Loop 2 round 4 review finding: every test above
    exercises exactly one owner, so a regression dropping an `owner_id`
    clause from any of the cascade's five raw-SQL statements would go
    undetected.
15. Two owners whose own Gmail accounts each produce a message sharing the
    same raw `external_message_id` (only unique per `(workspace_id,
    thread_id)`, not per-workspace) do not leak `pkos_evidence` across the
    ownership boundary -- Loop 2 round 4 review finding: `source_ref`
    matching has no owner qualifier of its own; the fix skips (does not
    purge) an evidence row whose id is ambiguous across owners. This same
    test also proves the mixed case: a second, non-colliding evidence row
    for the SAME owner is still purged normally in the same request (Loop
    2 round 5 review finding: the original version only ever seeded one
    message, so it never exercised the `if safe_ids:` purge branch
    alongside a non-empty `ambiguous_ids`).
16. The collision fix above only defends against a colliding id that is
    already *committed* by the time the ambiguity check runs -- neither a
    still-live row nor a *finished* prior cascade's own purge-log entry
    defends against two owners' cascades genuinely *overlapping* in time
    (both in flight, neither committed yet), where each one's own
    ambiguity check (under `READ COMMITTED`) could independently see "not
    ambiguous" -- Loop 2 round 17 review finding. Closed with a `pg_
    advisory_xact_lock` keyed on `workspace_id` alone (round 17 first
    keyed it per candidate `external_message_id`; round 19 review found
    that risked exhausting Postgres's database-wide `max_locks_per_
    transaction` pool for a large mailbox, a cross-tenant denial-of-
    service vector, so it was widened to one lock per workspace), proven
    end-to-end the same way item 20 below proves its own lock: a raw
    connection holds the lock first, a background thread's own `disable`
    call genuinely blocks rather than racing ahead, and once released the
    disable proceeds and correctly treats the id as ambiguous.
17. The collision fix above only defends against a *concurrent* collision
    (both owners' rows still live). Two owners disabling *sequentially* --
    the second only after the first owner's own colliding `email_messages`
    row (the only thing that made the id ambiguous) is already gone -- do
    not leak the first owner's preserved evidence either, via `email_
    message_id_purge_log` (migration `0076`, Loop 2 round 8 review
    finding).
18. `revoke_consent_endpoint` rejects a `consent_id` a *later* grant has
    superseded (`404 CONSENT_NOT_FOUND`) rather than resolving it to
    `domain_key` and re-running the cascade against a domain the owner has
    since re-granted consent for and resynced -- Loop 2 round 8 review
    finding, check refined round 9 (see item 19).
19. The fix for item 18 above must not reject a *legitimate* same-
    `Idempotency-Key` retry of a revoke that already succeeded, with no
    re-grant in between -- Loop 2 round 9 review finding: round 8's own
    first attempt (a plain `revoked_at IS NULL` check) wrongly 404'd that
    case too, since it ran before `_disable_domain`'s own idempotency-
    cache check ever got a chance to fire.
20. The check for item 18 above genuinely serializes against a concurrent
    re-grant, not merely a sequential one -- Loop 2 round 10 review
    finding: items 18/19's own check used to run in a separate transaction
    that committed before `_disable_domain` was ever called, leaving a
    TOCTOU window for a concurrent re-grant to commit in between and have
    `_disable_domain` disable-and-cascade-purge the freshly re-granted
    state anyway. Proven end-to-end (Loop 2 round 11 review: the round-10
    version of this test only proved the underlying lock-contention
    mechanism via an unrelated `POST /consents` call, never actually
    exercising `revoke_consent_endpoint`'s own code path) -- revokes a
    consent for real, holds the `personal_domains` row's lock, replays that
    now-superseded `consent_id` against `revoke_consent_endpoint` itself in
    a background thread (which blocks on the lock), regrants over the SAME
    held connection while the replay is blocked, then releases the lock and
    asserts the replay rejects with `404 CONSENT_NOT_FOUND` once unblocked.
21. Items 9-10's own `domain.enabled` check is necessary but not
    sufficient -- Loop 2 round 22 review finding (MEDIUM-HIGH): a genuine
    Gmail reconnect through `gmail_oauth_callback_endpoint` (which never
    references `personal_domains`/`domain_consents` at all) leaves
    `domain.enabled` still `False`, so a same-`Idempotency-Key` replay
    after that reconnect would still have been served item 9's own stale
    cached response. Closed by also checking for a reactivated `gmail`
    connector account on a cache hit.
22. The identical fix for `delete_domain_endpoint`, same mechanism and
    same severity as item 21 above.
"""

import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from hmac import new
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from identity_fixtures import create_identity
from sqlalchemy import text

from ecc.auth import AuthContext
from ecc.config import get_settings
from ecc.database import STATEMENT_TIMEOUT_MS, SessionFactory, engine
from ecc.domains.engineering.connectors import ConnectorAccountContext
from ecc.domains.engineering.crypto import encrypt_credential
from ecc.domains.personal import gmail_revocation
from ecc.domains.personal.gmail_adapter import GmailAdapter, _pack_credential
from ecc.main import app

settings = get_settings()
pytestmark = pytest.mark.skipif(
    not settings.database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)

_OWNER_EMAIL = "gmail-revocation-owner@example.test"
_SENDER = "colleague@partner-co.test"


@dataclass
class _SlowDisconnectAdapter:
    """Blocks inside `disconnect()` until released -- mirrors `test_
    engineering_connectors_postgres.py`'s own `_SlowDisconnectAdapter`
    (round 23 review's fake) for `disable_connector_endpoint`, applied
    here to the two call sites that actually reach `finish_gmail_
    revocation` for `gmail` -- `domains.py:_disable_domain` and `export_
    deletion.py:delete_domain_endpoint` -- which had no equivalent
    coverage (Loop 2 round 4 review finding).
    """

    entered_disconnect: threading.Event = field(default_factory=threading.Event)
    release: threading.Event = field(default_factory=threading.Event)

    def disconnect(self, account: ConnectorAccountContext) -> None:
        self.entered_disconnect.set()
        self.release.wait(timeout=5)


def _revoke_transport() -> httpx.MockTransport:
    """Every test below that reaches `GmailAdapter.disconnect` (via
    `finish_gmail_revocation`, after the transaction commits) needs
    Google's `/revoke` endpoint to return success -- without this, each
    cascade test made a real, unmocked outbound HTTPS call to Google's
    live endpoint (Loop 2 round 1 review finding). Unlike `test_gmail_
    threads_postgres.py`'s own per-test transports (which vary message
    content/failure behavior across tests), every cascade test here needs
    identical revoke behavior, so this is installed once by the shared
    `gmail_revocation_context` fixture below rather than per test.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    return httpx.MockTransport(handler)


def _headers(token: str, key: str | None = None) -> dict[str, str]:
    csrf = new(settings.session_secret.encode(), token.encode(), "sha256").hexdigest()
    headers = {"X-CSRF-Token": csrf, "X-Correlation-ID": str(uuid4())}
    if key is not None:
        headers["Idempotency-Key"] = key
    return headers


def _insert_thread(
    connection, *, workspace_id: UUID, owner_id: UUID, account_id: UUID, now: datetime
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
                :external_thread_id, 'Contract renewal', :now, :now, :now
            )
            """
        ),
        {
            "id": thread_id,
            "workspace_id": workspace_id,
            "owner_id": owner_id,
            "connector_account_id": account_id,
            "external_thread_id": f"thread-{thread_id}",
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
) -> UUID:
    message_id = uuid4()
    connection.execute(
        text(
            """
            INSERT INTO email_messages (
                id, workspace_id, owner_id, thread_id, external_message_id,
                sender, recipients, sent_at, direction, snippet, body,
                body_fetched_at, created_at, updated_at
            ) VALUES (
                :id, :workspace_id, :owner_id, :thread_id, :external_message_id,
                :sender, :recipients, :now, 'inbound', 'preview', NULL,
                NULL, :now, :now
            )
            """
        ),
        {
            "id": message_id,
            "workspace_id": workspace_id,
            "owner_id": owner_id,
            "thread_id": thread_id,
            "external_message_id": external_message_id,
            "sender": _SENDER,
            "recipients": [_OWNER_EMAIL],
            "now": now,
        },
    )
    return message_id


def _insert_attention_item(
    connection, *, workspace_id: UUID, owner_id: UUID, thread_id: UUID, now: datetime
) -> None:
    connection.execute(
        text(
            """
            INSERT INTO attention_items (
                id, workspace_id, entity_type, entity_id, source_entity_version,
                score, confidence, factors, explanation, generated_at, expires_at,
                owner_id, visibility
            ) VALUES (
                :id, :workspace_id, 'email_thread', :entity_id, 1,
                80, 0.9, '{}'::jsonb, 'Awaiting reply', :now, :expires_at,
                :owner_id, 'private'
            )
            """
        ),
        {
            "id": uuid4(),
            "workspace_id": workspace_id,
            "entity_id": thread_id,
            "now": now,
            "expires_at": now + timedelta(days=7),
            "owner_id": owner_id,
        },
    )


def _insert_pkos_node(connection, *, workspace_id: UUID, owner_id: UUID, now: datetime) -> UUID:
    """Matches `gmail_adapter.py:_resolve_or_create_person`'s own `INSERT
    INTO pkos_nodes` column list exactly.
    """
    node_id = uuid4()
    connection.execute(
        text(
            """
            INSERT INTO pkos_nodes (
                id, workspace_id, node_type, canonical_name, attributes,
                status, confidence, version, created_at, updated_at,
                owner_id, visibility
            ) VALUES (
                :id, :workspace_id, 'person', :name, '{}'::jsonb,
                'active', 1.00, 1, :now, :now, :owner_id, 'workspace'
            )
            """
        ),
        {
            "id": node_id,
            "workspace_id": workspace_id,
            "name": _SENDER,
            "now": now,
            "owner_id": owner_id,
        },
    )
    return node_id


def _insert_pkos_evidence(
    connection, *, workspace_id: UUID, node_id: UUID, source_ref: str, now: datetime
) -> None:
    connection.execute(
        text(
            """
            INSERT INTO pkos_evidence (
                id, workspace_id, node_id, source_type, source_ref, sha256, captured_at
            ) VALUES (
                :id, :workspace_id, :node_id, 'gmail_sync', :source_ref, :sha256, :now
            )
            """
        ),
        {
            "id": uuid4(),
            "workspace_id": workspace_id,
            "node_id": node_id,
            "source_ref": source_ref,
            "sha256": sha256(source_ref.encode()).hexdigest(),
            "now": now,
        },
    )


def _insert_recommendation(
    connection,
    *,
    workspace_id: UUID,
    owner_id: UUID,
    evidence_id: UUID | None,
    status: str,
    now: datetime,
) -> UUID:
    recommendation_id = uuid4()
    connection.execute(
        text(
            """
            INSERT INTO recommendations (
                id, workspace_id, recommendation_type, target_type, target_id,
                proposed_action, rationale, confidence, status, evidence_ids,
                source, created_by, updated_by, created_at, updated_at, version
            ) VALUES (
                :id, :workspace_id, 'email_action_detected', 'task', :target_id,
                '{"foo": "bar"}'::jsonb, 'The email says a contract is due Friday.',
                0.9, :status, :evidence_ids,
                'ai', :owner_id, :owner_id, :now, :now, 1
            )
            """
        ),
        {
            "id": recommendation_id,
            "workspace_id": workspace_id,
            "target_id": uuid4() if status == "executed" else None,
            "status": status,
            "evidence_ids": [evidence_id] if evidence_id is not None else [],
            "owner_id": owner_id,
            "now": now,
        },
    )
    return recommendation_id


def _cleanup_workspace(workspace_id: UUID, *, emails: list[str]) -> None:
    """`emails` is `[_OWNER_EMAIL, *ctx["extra_owner_emails"]]` -- tests
    that seed a second owner (Loop 2 round 4-5's cross-owner tests) append
    that owner's own email to `extra_owner_emails` themselves. Cleanup
    must happen here, in the SAME transaction sequence as the workspace-
    scoped `users` deletion below, not inline in the test body: `accounts`
    has a `RESTRICT` FK from `users` (`fk_users_account`), so deleting a
    second owner's `accounts` row before their own `users` row is gone
    raises `RestrictViolation` (Loop 2 round 6 review: CI caught this
    exact ordering bug in the original version of this fix).
    """
    with engine.begin() as connection:
        for table in (
            "deletion_jobs",
            "idempotency_records",
            "audit_events",
            "event_outbox",
            "recommendations",
            "pkos_evidence",
            "pkos_nodes",
            "attention_items",
            "email_message_id_purge_log",
            "email_messages",
            "email_threads",
            "domain_consents",
            "personal_domains",
            "connector_accounts",
            "goals",
            "routines",
            "check_ins",
            "domain_records",
            "domain_sources",
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
        # `accounts` is not workspace-scoped -- matches `test_gmail_threads_
        # postgres.py`'s own established `_cleanup_workspace` precedent.
        # Runs only after every `users` row referencing these accounts
        # (workspace-scoped, deleted above) is gone.
        connection.execute(
            text("DELETE FROM accounts WHERE email = ANY(:emails)"),
            {"emails": emails},
        )


@pytest.fixture
def gmail_revocation_context(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict]:
    monkeypatch.setattr(gmail_revocation, "_adapter", GmailAdapter(transport=_revoke_transport()))
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
                "VALUES (:id, 'Gmail Revocation Test', 'UTC', :now)"
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
                    :id, :workspace_id, 'gmail', :external_account_id, 'Revocation test account',
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
        consent_id = uuid4()
        connection.execute(
            text(
                "INSERT INTO domain_consents (id, workspace_id, owner_id, domain_key, "
                "granted_at, created_at) "
                "VALUES (:id, :workspace_id, :owner_id, 'email', :now, :now)"
            ),
            {"id": consent_id, "workspace_id": workspace_id, "owner_id": owner_id, "now": now},
        )

    client = TestClient(app)
    client.cookies.set("ecc_session", token)
    extra_owner_emails: list[str] = []
    try:
        yield {
            "client": client,
            "token": token,
            "workspace_id": workspace_id,
            "owner_id": owner_id,
            "account_id": account_id,
            "consent_id": consent_id,
            "now": now,
            "extra_owner_emails": extra_owner_emails,
        }
    finally:
        client.close()
        _cleanup_workspace(workspace_id, emails=[_OWNER_EMAIL, *extra_owner_emails])


def _insert_gmail_connector_account(
    connection, *, workspace_id: UUID, owner_id: UUID, external_account_id: str, now: datetime
) -> UUID:
    account_id = uuid4()
    credential = _pack_credential("access-2", "refresh-2", now + timedelta(hours=1))
    connection.execute(
        text(
            """
            INSERT INTO connector_accounts (
                id, workspace_id, provider, external_account_id, display_name,
                granted_scopes, encrypted_credentials, status, version,
                created_by, updated_by, created_at, updated_at, owner_id, visibility
            ) VALUES (
                :id, :workspace_id, 'gmail', :external_account_id, 'Second linked account',
                ARRAY['https://www.googleapis.com/auth/gmail.readonly'], :encrypted,
                'active', 1, :actor_id, :actor_id, :now, :now, :actor_id, 'workspace'
            )
            """
        ),
        {
            "id": account_id,
            "workspace_id": workspace_id,
            "external_account_id": external_account_id,
            "encrypted": encrypt_credential(credential),
            "actor_id": owner_id,
            "now": now,
        },
    )
    return account_id


def _insert_enabled_email_domain(
    connection, *, workspace_id: UUID, owner_id: UUID, now: datetime
) -> None:
    """`email_threads`'s own FK to `personal_domains(workspace_id, owner_
    id, domain_key)` (migration `0069`) means a second owner's own thread
    cannot be inserted without this row existing for them first -- Loop 2
    round 5 review's own new cross-owner tests found this the hard way
    (CI: `ForeignKeyViolation` on `email_threads_workspace_id_owner_id_
    domain_key_fkey`), since `gmail_revocation_context` only ever creates
    this row for the primary owner.
    """
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


def _row_count(table: str, workspace_id: UUID) -> int:
    with engine.begin() as connection:
        return connection.execute(
            text(f"SELECT COUNT(*) FROM {table} WHERE workspace_id = :workspace_id"),  # noqa: S608
            {"workspace_id": workspace_id},
        ).scalar_one()


def _connector_status(workspace_id: UUID, account_id: UUID) -> str:
    with engine.begin() as connection:
        return connection.execute(
            text(
                "SELECT status FROM connector_accounts "
                "WHERE workspace_id = :workspace_id AND id = :id"
            ),
            {"workspace_id": workspace_id, "id": account_id},
        ).scalar_one()


def _seed_full_cascade_fixture(ctx: dict) -> dict[str, Any]:
    """Inserts one thread/message/attention-item/recommendation/evidence-
    node combination this owner's own Gmail sync could plausibly have
    produced -- used by every test below that exercises the cascade.
    """
    with engine.begin() as connection:
        thread_id = _insert_thread(
            connection,
            workspace_id=ctx["workspace_id"],
            owner_id=ctx["owner_id"],
            account_id=ctx["account_id"],
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
        )
        _insert_attention_item(
            connection,
            workspace_id=ctx["workspace_id"],
            owner_id=ctx["owner_id"],
            thread_id=thread_id,
            now=ctx["now"],
        )
        node_id = _insert_pkos_node(
            connection, workspace_id=ctx["workspace_id"], owner_id=ctx["owner_id"], now=ctx["now"]
        )
        _insert_pkos_evidence(
            connection,
            workspace_id=ctx["workspace_id"],
            node_id=node_id,
            source_ref=f"gmail:{external_message_id}",
            now=ctx["now"],
        )
        _insert_pkos_evidence(
            connection,
            workspace_id=ctx["workspace_id"],
            node_id=node_id,
            source_ref=f"gmail:detect_action:{external_message_id}",
            now=ctx["now"],
        )
        proposed_recommendation_id = _insert_recommendation(
            connection,
            workspace_id=ctx["workspace_id"],
            owner_id=ctx["owner_id"],
            evidence_id=None,
            status="proposed",
            now=ctx["now"],
        )
    return {
        "thread_id": thread_id,
        "message_id": message_id,
        "external_message_id": external_message_id,
        "node_id": node_id,
        "proposed_recommendation_id": proposed_recommendation_id,
    }


def test_disable_domain_disconnects_connector_and_purges_email_data(
    gmail_revocation_context: dict,
) -> None:
    ctx = gmail_revocation_context
    _seed_full_cascade_fixture(ctx)

    resp = ctx["client"].post(
        "/api/v1/personal/domains/email/disable",
        headers=_headers(ctx["token"], str(uuid4())),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["enabled"] is False

    assert _connector_status(ctx["workspace_id"], ctx["account_id"]) == "disconnected"
    assert _row_count("email_threads", ctx["workspace_id"]) == 0
    assert _row_count("email_messages", ctx["workspace_id"]) == 0
    assert _row_count("attention_items", ctx["workspace_id"]) == 0
    assert _row_count("pkos_evidence", ctx["workspace_id"]) == 0
    # The `proposed` (non-`executed`) recommendation is deleted outright.
    assert _row_count("recommendations", ctx["workspace_id"]) == 0


def test_disable_domain_redacts_executed_recommendation_instead_of_deleting(
    gmail_revocation_context: dict,
) -> None:
    ctx = gmail_revocation_context
    with engine.begin() as connection:
        thread_id = _insert_thread(
            connection,
            workspace_id=ctx["workspace_id"],
            owner_id=ctx["owner_id"],
            account_id=ctx["account_id"],
            now=ctx["now"],
        )
        external_message_id = f"msg-{uuid4()}"
        _insert_message(
            connection,
            workspace_id=ctx["workspace_id"],
            owner_id=ctx["owner_id"],
            thread_id=thread_id,
            external_message_id=external_message_id,
            now=ctx["now"],
        )
        node_id = _insert_pkos_node(
            connection, workspace_id=ctx["workspace_id"], owner_id=ctx["owner_id"], now=ctx["now"]
        )
        _insert_pkos_evidence(
            connection,
            workspace_id=ctx["workspace_id"],
            node_id=node_id,
            source_ref=f"gmail:detect_action:{external_message_id}",
            now=ctx["now"],
        )
        executed_id = _insert_recommendation(
            connection,
            workspace_id=ctx["workspace_id"],
            owner_id=ctx["owner_id"],
            evidence_id=None,
            status="executed",
            now=ctx["now"],
        )

    resp = ctx["client"].post(
        "/api/v1/personal/domains/email/disable",
        headers=_headers(ctx["token"], str(uuid4())),
    )
    assert resp.status_code == 200, resp.text

    with engine.begin() as connection:
        row = (
            connection.execute(
                text(
                    "SELECT status, rationale, proposed_action, evidence_ids, target_id "
                    "FROM recommendations WHERE workspace_id = :workspace_id AND id = :id"
                ),
                {"workspace_id": ctx["workspace_id"], "id": executed_id},
            )
            .mappings()
            .one()
        )
    assert row["status"] == "executed"
    assert row["rationale"] == "Source email no longer available -- email consent was revoked."
    assert row["proposed_action"] == {}
    assert row["evidence_ids"] == []
    # The confirmed target (a real, independent task/commitment/risk row)
    # is untouched -- this cascade only redacts the recommendation's own
    # Gmail-derived narrative, not what it led to.
    assert row["target_id"] is not None


def test_disable_domain_does_not_delete_pkos_node(gmail_revocation_context: dict) -> None:
    ctx = gmail_revocation_context
    seeded = _seed_full_cascade_fixture(ctx)

    resp = ctx["client"].post(
        "/api/v1/personal/domains/email/disable",
        headers=_headers(ctx["token"], str(uuid4())),
    )
    assert resp.status_code == 200, resp.text

    with engine.begin() as connection:
        row = connection.execute(
            text("SELECT id FROM pkos_nodes WHERE workspace_id = :workspace_id AND id = :id"),
            {"workspace_id": ctx["workspace_id"], "id": seeded["node_id"]},
        ).one_or_none()
    assert row is not None
    assert _row_count("pkos_evidence", ctx["workspace_id"]) == 0


def test_revoke_consent_endpoint_reaches_the_same_cascade(gmail_revocation_context: dict) -> None:
    ctx = gmail_revocation_context
    _seed_full_cascade_fixture(ctx)

    resp = ctx["client"].post(
        f"/api/v1/personal/consents/{ctx['consent_id']}/revoke",
        headers=_headers(ctx["token"], str(uuid4())),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["enabled"] is False

    assert _connector_status(ctx["workspace_id"], ctx["account_id"]) == "disconnected"
    assert _row_count("email_threads", ctx["workspace_id"]) == 0
    assert _row_count("attention_items", ctx["workspace_id"]) == 0
    assert _row_count("recommendations", ctx["workspace_id"]) == 0


def test_delete_domain_endpoint_reaches_the_same_cascade(gmail_revocation_context: dict) -> None:
    ctx = gmail_revocation_context
    _seed_full_cascade_fixture(ctx)

    resp = ctx["client"].post(
        "/api/v1/personal/domains/email/delete",
        headers=_headers(ctx["token"], str(uuid4())),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "completed"

    assert _connector_status(ctx["workspace_id"], ctx["account_id"]) == "disconnected"
    assert _row_count("email_threads", ctx["workspace_id"]) == 0
    assert _row_count("email_messages", ctx["workspace_id"]) == 0
    assert _row_count("attention_items", ctx["workspace_id"]) == 0
    assert _row_count("pkos_evidence", ctx["workspace_id"]) == 0
    assert _row_count("recommendations", ctx["workspace_id"]) == 0
    with engine.begin() as connection:
        job = (
            connection.execute(
                text(
                    "SELECT scope, status FROM deletion_jobs "
                    "WHERE workspace_id = :workspace_id AND domain_key = 'email'"
                ),
                {"workspace_id": ctx["workspace_id"]},
            )
            .mappings()
            .one()
        )
    assert job["scope"] == "domain"
    assert job["status"] == "completed"


def test_disabling_an_already_disabled_domain_does_not_rerun_cascade(
    gmail_revocation_context: dict,
) -> None:
    ctx = gmail_revocation_context
    first = ctx["client"].post(
        "/api/v1/personal/domains/email/disable",
        headers=_headers(ctx["token"], str(uuid4())),
    )
    assert first.status_code == 200, first.text
    assert _connector_status(ctx["workspace_id"], ctx["account_id"]) == "disconnected"

    # A brand new thread inserted AFTER the first disable -- if a second
    # disable request re-ran the cascade (rather than being a no-op on an
    # already-disabled domain), this would be purged too, which would be
    # wrong: the domain is already off, this is a fresh request with a
    # fresh Idempotency-Key, not a replay of the first one.
    with engine.begin() as connection:
        thread_id = _insert_thread(
            connection,
            workspace_id=ctx["workspace_id"],
            owner_id=ctx["owner_id"],
            account_id=ctx["account_id"],
            now=ctx["now"],
        )

    second = ctx["client"].post(
        "/api/v1/personal/domains/email/disable",
        headers=_headers(ctx["token"], str(uuid4())),
    )
    assert second.status_code == 200, second.text

    with engine.begin() as connection:
        row = connection.execute(
            text("SELECT id FROM email_threads WHERE workspace_id = :workspace_id AND id = :id"),
            {"workspace_id": ctx["workspace_id"], "id": thread_id},
        ).one_or_none()
    assert row is not None, "second disable on an already-disabled domain must not re-run cascade"


def test_disabling_a_non_email_domain_is_unaffected(gmail_revocation_context: dict) -> None:
    ctx = gmail_revocation_context
    enable_resp = ctx["client"].post(
        "/api/v1/personal/domains",
        json={"domain_key": "habits"},
        headers=_headers(ctx["token"], str(uuid4())),
    )
    assert enable_resp.status_code == 201, enable_resp.text

    resp = ctx["client"].post(
        "/api/v1/personal/domains/habits/disable",
        headers=_headers(ctx["token"], str(uuid4())),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["enabled"] is False
    # The `email` domain's own connector/data must be untouched by a
    # completely unrelated domain's disable.
    assert _connector_status(ctx["workspace_id"], ctx["account_id"]) == "active"

    with engine.begin() as connection:
        connection.execute(
            text(
                "DELETE FROM personal_domains "
                "WHERE workspace_id = :workspace_id AND domain_key = 'habits'"
            ),
            {"workspace_id": ctx["workspace_id"]},
        )
        connection.execute(
            text(
                "DELETE FROM domain_consents "
                "WHERE workspace_id = :workspace_id AND domain_key = 'habits'"
            ),
            {"workspace_id": ctx["workspace_id"]},
        )


def test_disable_domain_idempotency_replay_does_not_rerun_cascade(
    gmail_revocation_context: dict,
) -> None:
    ctx = gmail_revocation_context
    _seed_full_cascade_fixture(ctx)
    key = str(uuid4())

    first = ctx["client"].post(
        "/api/v1/personal/domains/email/disable", headers=_headers(ctx["token"], key)
    )
    assert first.status_code == 200, first.text
    assert _connector_status(ctx["workspace_id"], ctx["account_id"]) == "disconnected"

    # A fresh thread inserted between the two calls -- a replay must
    # return the cached response without touching it, proving the
    # cascade did not run a second time.
    with engine.begin() as connection:
        thread_id = _insert_thread(
            connection,
            workspace_id=ctx["workspace_id"],
            owner_id=ctx["owner_id"],
            account_id=ctx["account_id"],
            now=ctx["now"],
        )

    second = ctx["client"].post(
        "/api/v1/personal/domains/email/disable", headers=_headers(ctx["token"], key)
    )
    assert second.status_code == 200, second.text
    # Not a full-body comparison: `response_contract_middleware` stamps a
    # fresh `request_id`/`correlation_id` onto every response, including a
    # cached replay, so two calls' bodies never match exactly -- matches
    # `test_gmail_threads_postgres.py`'s own established idempotency-
    # replay pattern (compare only the specific business fields).
    first_body = first.json()
    second_body = second.json()
    for field_name in ("id", "domain_key", "enabled", "version"):
        assert second_body[field_name] == first_body[field_name]

    with engine.begin() as connection:
        row = connection.execute(
            text("SELECT id FROM email_threads WHERE workspace_id = :workspace_id AND id = :id"),
            {"workspace_id": ctx["workspace_id"], "id": thread_id},
        ).one_or_none()
    assert row is not None, "idempotency replay must not re-run the cascade"


def test_disable_domain_idempotency_key_reused_after_a_regrant_is_rejected(
    gmail_revocation_context: dict,
) -> None:
    """Loop 2 round 21 review finding (MEDIUM-HIGH): unlike item 8 above (a
    replay with nothing else happening in between), this reuses the same
    `Idempotency-Key` for a *second*, materially different real-world
    request -- a disable, then a genuine re-enable/re-grant, then a second
    disable attempt with the SAME key. Before this fix, `_disable_domain`
    checked the idempotency cache before ever reading `personal_domains`,
    so this returned the FIRST call's stale `enabled: false` response
    without running `cascade_email_revocation` a second time -- silently
    telling the caller the (new) consent was revoked and data purged when
    neither had happened. Now rejected with `409 IDEMPOTENCY_CONFLICT`
    instead, forcing the caller to retry with a fresh key rather than
    trusting a stale "success."
    """
    ctx = gmail_revocation_context
    _seed_full_cascade_fixture(ctx)
    key = str(uuid4())

    first = ctx["client"].post(
        "/api/v1/personal/domains/email/disable", headers=_headers(ctx["token"], key)
    )
    assert first.status_code == 200, first.text

    regrant = ctx["client"].post(
        "/api/v1/personal/domains/email/enable", headers=_headers(ctx["token"], str(uuid4()))
    )
    assert regrant.status_code == 200, regrant.text
    assert regrant.json()["enabled"] is True

    replay = ctx["client"].post(
        "/api/v1/personal/domains/email/disable", headers=_headers(ctx["token"], key)
    )
    assert replay.status_code == 409, replay.text
    assert replay.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"

    # The rejected replay must not have mutated anything -- the domain is
    # still enabled from the regrant above, not silently disabled either.
    with engine.begin() as connection:
        enabled = connection.execute(
            text(
                "SELECT enabled FROM personal_domains "
                "WHERE workspace_id = :workspace_id AND owner_id = :owner_id "
                "AND domain_key = 'email'"
            ),
            {"workspace_id": ctx["workspace_id"], "owner_id": ctx["owner_id"]},
        ).scalar_one()
    assert enabled is True


def test_delete_domain_endpoint_idempotency_key_reused_after_a_regrant_is_rejected(
    gmail_revocation_context: dict,
) -> None:
    """Loop 2 round 21 review finding: the identical gap as the test above,
    for `delete_domain_endpoint` -- a delete, then a genuine re-enable, then
    a second delete attempt with the SAME `Idempotency-Key` used to
    silently no-op (return the first delete's cached "completed" response
    without ever calling `_delete_domain_data` again), leaving the
    freshly-reconnected/resynced data unpurged while the caller was told
    the delete had already completed.
    """
    ctx = gmail_revocation_context
    _seed_full_cascade_fixture(ctx)
    key = str(uuid4())

    first = ctx["client"].post(
        "/api/v1/personal/domains/email/delete", headers=_headers(ctx["token"], key)
    )
    assert first.status_code == 200, first.text

    regrant = ctx["client"].post(
        "/api/v1/personal/domains/email/enable", headers=_headers(ctx["token"], str(uuid4()))
    )
    assert regrant.status_code == 200, regrant.text
    assert regrant.json()["enabled"] is True

    replay = ctx["client"].post(
        "/api/v1/personal/domains/email/delete", headers=_headers(ctx["token"], key)
    )
    assert replay.status_code == 409, replay.text
    assert replay.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"

    with engine.begin() as connection:
        enabled = connection.execute(
            text(
                "SELECT enabled FROM personal_domains "
                "WHERE workspace_id = :workspace_id AND owner_id = :owner_id "
                "AND domain_key = 'email'"
            ),
            {"workspace_id": ctx["workspace_id"], "owner_id": ctx["owner_id"]},
        ).scalar_one()
    assert enabled is True


def test_disable_domain_with_multiple_connector_accounts_disconnects_all(
    gmail_revocation_context: dict,
) -> None:
    """Loop 2 round 1 review finding: `connector_accounts` has no per-owner
    uniqueness on `provider = 'gmail'` (only `external_account_id` is
    unique), so an owner who connected two separate Google accounts has
    two simultaneously-`active` rows. `cascade_email_revocation`'s
    original `SELECT ... .one_or_none()` raised `MultipleResultsFound`
    here, uncaught, aborting the entire disable request with a 500 --
    proving that regression requires seeding a second row, which no other
    test in this file does.
    """
    ctx = gmail_revocation_context
    _seed_full_cascade_fixture(ctx)
    with engine.begin() as connection:
        second_account_id = _insert_gmail_connector_account(
            connection,
            workspace_id=ctx["workspace_id"],
            owner_id=ctx["owner_id"],
            external_account_id="second-google-account@example.test",
            now=ctx["now"],
        )

    resp = ctx["client"].post(
        "/api/v1/personal/domains/email/disable",
        headers=_headers(ctx["token"], str(uuid4())),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["enabled"] is False

    assert _connector_status(ctx["workspace_id"], ctx["account_id"]) == "disconnected"
    assert _connector_status(ctx["workspace_id"], second_account_id) == "disconnected"


def test_disable_domain_releases_pool_connection_and_row_lock_before_revoke_call(
    gmail_revocation_context: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Loop 2 round 4 review finding: `domains.py:_disable_domain` calls
    `session.close()` immediately before `finish_gmail_revocation(...)`'s
    potentially slow, blocking Google revoke call -- matching `connector_
    accounts.py:disable_connector_endpoint`'s own established split
    (round 23 review) exactly, for the identical reason. That endpoint has
    its own dedicated concurrency test proving the pool connection and row
    lock are actually released before the blocking call runs; this module
    (the only other call site that ever reaches `finish_gmail_revocation`
    for `gmail`) had none. Reproduced here the same way: while a `/disable`
    request is genuinely blocked inside `disconnect()`, a concurrent raw
    `SELECT ... FOR UPDATE NOWAIT` on the same `connector_accounts` row
    must succeed immediately -- if the connection/lock were still held, it
    would raise `LockNotAvailable` instead.
    """
    ctx = gmail_revocation_context
    slow_adapter = _SlowDisconnectAdapter()
    monkeypatch.setattr(gmail_revocation, "_adapter", slow_adapter)

    def _disable() -> Any:
        return ctx["client"].post(
            "/api/v1/personal/domains/email/disable",
            headers=_headers(ctx["token"], str(uuid4())),
        )

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(_disable)
        assert slow_adapter.entered_disconnect.wait(timeout=5), "disconnect() never entered"

        with engine.connect() as probe:
            probe.execution_options(isolation_level="AUTOCOMMIT")
            probe.execute(text("SET statement_timeout = '2s'"))
            row = (
                probe.execute(
                    text(
                        "SELECT id, status FROM connector_accounts WHERE id = :id FOR UPDATE NOWAIT"
                    ),
                    {"id": ctx["account_id"]},
                )
                .mappings()
                .one()
            )
            assert row["id"] == ctx["account_id"]
            probe.rollback()
            # Session-scoped `SET` (not `SET LOCAL`) survives past `probe.
            # rollback()` -- restore the connection's baseline before it
            # returns to the pool, matching `test_engineering_connectors_
            # postgres.py`'s own identical concurrency test exactly.
            probe.execute(text(f"SET statement_timeout = {STATEMENT_TIMEOUT_MS}"))

        slow_adapter.release.set()
        response = future.result(timeout=5)

    assert response.status_code == 200, response.text
    assert _connector_status(ctx["workspace_id"], ctx["account_id"]) == "disconnected"


def test_delete_domain_releases_pool_connection_and_row_lock_before_revoke_call(
    gmail_revocation_context: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Loop 2 round 5 review finding: the round-4 concurrency test above
    only covers `disable_domain_endpoint`; `export_deletion.py:delete_
    domain_endpoint`'s own identical `session.close()` before `finish_
    gmail_revocation(...)` sequence (the OTHER call site that ever reaches
    it for `gmail`) had no equivalent proof, despite this file's own
    `_SlowDisconnectAdapter` docstring claiming both were covered.
    """
    ctx = gmail_revocation_context
    slow_adapter = _SlowDisconnectAdapter()
    monkeypatch.setattr(gmail_revocation, "_adapter", slow_adapter)

    def _delete() -> Any:
        return ctx["client"].post(
            "/api/v1/personal/domains/email/delete",
            headers=_headers(ctx["token"], str(uuid4())),
        )

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(_delete)
        assert slow_adapter.entered_disconnect.wait(timeout=5), "disconnect() never entered"

        with engine.connect() as probe:
            probe.execution_options(isolation_level="AUTOCOMMIT")
            probe.execute(text("SET statement_timeout = '2s'"))
            row = (
                probe.execute(
                    text(
                        "SELECT id, status FROM connector_accounts WHERE id = :id FOR UPDATE NOWAIT"
                    ),
                    {"id": ctx["account_id"]},
                )
                .mappings()
                .one()
            )
            assert row["id"] == ctx["account_id"]
            probe.rollback()
            probe.execute(text(f"SET statement_timeout = {STATEMENT_TIMEOUT_MS}"))

        slow_adapter.release.set()
        response = future.result(timeout=5)

    assert response.status_code == 200, response.text
    assert _connector_status(ctx["workspace_id"], ctx["account_id"]) == "disconnected"


def test_disable_domain_does_not_affect_a_different_owners_gmail_data(
    gmail_revocation_context: dict,
) -> None:
    """Loop 2 round 4 review finding: every test above exercises exactly
    one owner, so a regression that dropped an `owner_id` clause from one
    of `cascade_email_revocation`'s five hand-written raw-SQL statements
    would go undetected -- there is no `workspace_id`-only row for it to
    accidentally also match. Seeds a second owner in the SAME workspace
    with their own connector account, thread, message, and attention item,
    then disables only the first owner's `email` domain and asserts the
    second owner's data (and connector) is completely untouched.
    """
    ctx = gmail_revocation_context
    _seed_full_cascade_fixture(ctx)

    other_owner_id = uuid4()
    other_account_id = uuid4()
    other_email = f"other-owner-{uuid4()}@example.test"
    # Registered immediately, not after the test's own assertions -- Loop 2
    # round 7 review finding: registering only at the end left this email
    # unregistered (an orphaned `accounts` row) if any earlier assertion in
    # this test failed.
    ctx["extra_owner_emails"].append(other_email)
    credential = _pack_credential("other-access", "other-refresh", ctx["now"] + timedelta(hours=1))
    with engine.begin() as connection:
        create_identity(
            connection,
            workspace_id=ctx["workspace_id"],
            user_id=other_owner_id,
            email=other_email,
            now=ctx["now"],
        )
        _insert_enabled_email_domain(
            connection, workspace_id=ctx["workspace_id"], owner_id=other_owner_id, now=ctx["now"]
        )
        connection.execute(
            text(
                """
                INSERT INTO connector_accounts (
                    id, workspace_id, provider, external_account_id, display_name,
                    granted_scopes, encrypted_credentials, status, version,
                    created_by, updated_by, created_at, updated_at, owner_id, visibility
                ) VALUES (
                    :id, :workspace_id, 'gmail', :external_account_id, 'Other owner account',
                    ARRAY['https://www.googleapis.com/auth/gmail.readonly'], :encrypted,
                    'active', 1, :actor_id, :actor_id, :now, :now, :actor_id, 'workspace'
                )
                """
            ),
            {
                "id": other_account_id,
                "workspace_id": ctx["workspace_id"],
                "external_account_id": other_email,
                "encrypted": encrypt_credential(credential),
                "actor_id": other_owner_id,
                "now": ctx["now"],
            },
        )
        other_thread_id = _insert_thread(
            connection,
            workspace_id=ctx["workspace_id"],
            owner_id=other_owner_id,
            account_id=other_account_id,
            now=ctx["now"],
        )
        _insert_message(
            connection,
            workspace_id=ctx["workspace_id"],
            owner_id=other_owner_id,
            thread_id=other_thread_id,
            external_message_id=f"other-msg-{uuid4()}",
            now=ctx["now"],
        )
        _insert_attention_item(
            connection,
            workspace_id=ctx["workspace_id"],
            owner_id=other_owner_id,
            thread_id=other_thread_id,
            now=ctx["now"],
        )

    resp = ctx["client"].post(
        "/api/v1/personal/domains/email/disable",
        headers=_headers(ctx["token"], str(uuid4())),
    )
    assert resp.status_code == 200, resp.text

    # This owner's own data (and connector) is purged as usual.
    assert _connector_status(ctx["workspace_id"], ctx["account_id"]) == "disconnected"
    assert _row_count("email_threads", ctx["workspace_id"]) == 1  # the other owner's, only

    # The other owner's data and connector are completely untouched.
    with engine.begin() as connection:
        other_thread_row = connection.execute(
            text("SELECT id FROM email_threads WHERE workspace_id = :workspace_id AND id = :id"),
            {"workspace_id": ctx["workspace_id"], "id": other_thread_id},
        ).one_or_none()
        other_attention_count = connection.execute(
            text(
                "SELECT COUNT(*) FROM attention_items "
                "WHERE workspace_id = :workspace_id AND owner_id = :owner_id"
            ),
            {"workspace_id": ctx["workspace_id"], "owner_id": other_owner_id},
        ).scalar_one()
    assert other_thread_row is not None
    assert other_attention_count == 1
    assert _connector_status(ctx["workspace_id"], other_account_id) == "active"


def test_disable_domain_does_not_purge_evidence_with_a_colliding_external_message_id(
    gmail_revocation_context: dict,
) -> None:
    """Loop 2 round 4 review finding: `email_messages.external_message_id`
    is only uniqueness-constrained per `(workspace_id, thread_id)`
    (migration `0069`), not per-workspace -- nothing rules out two
    different owners' own connected Gmail accounts each producing a
    message that happens to share the same raw id. `source_ref` matching
    against `pkos_evidence` (this file's own `_seed_full_cascade_fixture`
    and every test above use exactly one owner, so this collision was
    previously untested) has no owner qualifier of its own. Seeds a
    second owner with a message sharing this owner's own `external_
    message_id`, then asserts the cascade leaves the now-ambiguous
    evidence row alone (`gmail_revocation.py`'s fix: an id that also
    belongs to another owner is skipped, not purged) rather than risk
    deleting across the ownership boundary -- while a second, non-
    colliding evidence row for this same owner is still purged normally
    (Loop 2 round 5 review finding: the original version of this test
    only ever seeded one message, so it exercised the "every candidate id
    is ambiguous" branch and never proved the mixed case -- a future
    refactor collapsing the `if safe_ids:` purge into "purge nothing
    whenever any id is ambiguous" would have passed unnoticed).
    """
    ctx = gmail_revocation_context
    seeded = _seed_full_cascade_fixture(ctx)
    colliding_external_id = seeded["external_message_id"]

    with engine.begin() as connection:
        safe_thread_id = _insert_thread(
            connection,
            workspace_id=ctx["workspace_id"],
            owner_id=ctx["owner_id"],
            account_id=ctx["account_id"],
            now=ctx["now"],
        )
        safe_external_id = f"safe-msg-{uuid4()}"
        _insert_message(
            connection,
            workspace_id=ctx["workspace_id"],
            owner_id=ctx["owner_id"],
            thread_id=safe_thread_id,
            external_message_id=safe_external_id,
            now=ctx["now"],
        )
        _insert_pkos_evidence(
            connection,
            workspace_id=ctx["workspace_id"],
            node_id=seeded["node_id"],
            source_ref=f"gmail:{safe_external_id}",
            now=ctx["now"],
        )

    other_owner_id = uuid4()
    other_email = f"other-owner-{uuid4()}@example.test"
    # Registered immediately -- see the identical comment in `test_disable_
    # domain_does_not_affect_a_different_owners_gmail_data` above.
    ctx["extra_owner_emails"].append(other_email)
    with engine.begin() as connection:
        create_identity(
            connection,
            workspace_id=ctx["workspace_id"],
            user_id=other_owner_id,
            email=other_email,
            now=ctx["now"],
        )
        _insert_enabled_email_domain(
            connection, workspace_id=ctx["workspace_id"], owner_id=other_owner_id, now=ctx["now"]
        )
        other_thread_id = _insert_thread(
            connection,
            workspace_id=ctx["workspace_id"],
            owner_id=other_owner_id,
            account_id=ctx["account_id"],
            now=ctx["now"],
        )
        # Same raw `external_message_id` as this owner's own message above
        # -- only `(workspace_id, thread_id)` is unique, so a *different*
        # thread (this other owner's) can legally reuse it.
        _insert_message(
            connection,
            workspace_id=ctx["workspace_id"],
            owner_id=other_owner_id,
            thread_id=other_thread_id,
            external_message_id=colliding_external_id,
            now=ctx["now"],
        )

    resp = ctx["client"].post(
        "/api/v1/personal/domains/email/disable",
        headers=_headers(ctx["token"], str(uuid4())),
    )
    assert resp.status_code == 200, resp.text

    # This owner's own message/thread are still purged as usual.
    assert _row_count("email_messages", ctx["workspace_id"]) == 1  # the other owner's, only
    # The now-ambiguous evidence row (shared `source_ref` with the other
    # owner's still-live message) survives -- skipped, not purged.
    with engine.begin() as connection:
        evidence_row = connection.execute(
            text(
                "SELECT id FROM pkos_evidence WHERE workspace_id = :workspace_id "
                "AND source_ref = :source_ref"
            ),
            {
                "workspace_id": ctx["workspace_id"],
                "source_ref": f"gmail:detect_action:{colliding_external_id}",
            },
        ).one_or_none()
    assert evidence_row is not None, "ambiguous evidence must be left alone, not purged"

    # The mixed case: this owner's OTHER, non-colliding evidence row is
    # still purged normally in the same request -- the `safe_ids` branch
    # still fires even while `ambiguous_ids` is non-empty.
    with engine.begin() as connection:
        safe_evidence_row = connection.execute(
            text(
                "SELECT id FROM pkos_evidence WHERE workspace_id = :workspace_id "
                "AND source_ref = :source_ref"
            ),
            {"workspace_id": ctx["workspace_id"], "source_ref": f"gmail:{safe_external_id}"},
        ).one_or_none()
    assert safe_evidence_row is None, "non-ambiguous evidence must still be purged normally"


def test_disable_domain_serializes_against_a_genuinely_concurrent_colliding_cascade(
    gmail_revocation_context: dict,
) -> None:
    """Loop 2 round 17 review finding: the fix in the test above (round 4)
    and the fix below (round 8, sequential case) both only ever defend
    against a colliding id that is already *committed* by the time the
    ambiguity check runs -- either a still-live row or a prior cascade's
    own finished purge-log entry. Neither defends against two owners'
    cascades genuinely *overlapping* in time (both in flight, neither
    committed yet): under default `READ COMMITTED` isolation, each
    cascade's ambiguity check only ever sees committed data, so two
    truly-concurrent cascades for a colliding id could each independently
    conclude "not ambiguous" and both purge the `pkos_evidence` row --
    the one DELETE in this cascade with no `owner_id` qualifier of its
    own. Fixed with a `pg_advisory_xact_lock` keyed on `workspace_id`
    alone, acquired before the ambiguity check -- Loop 2 round 19 review
    finding: an earlier version of this fix keyed one lock per candidate
    id instead, which for a large mailbox could exhaust Postgres's shared
    `max_locks_per_transaction` pool (a database-wide resource, so one
    owner's own large mailbox could transiently break lock acquisition
    for *other* tenants too); a single workspace-scoped lock is O(1)
    regardless of mailbox size and still fully closes the race, at the
    cost of over-serializing every gmail cascade in the same workspace
    against this section, not only genuinely colliding ones -- an
    acceptable trade-off for an action this infrequent.

    This test proves the lock itself is real and load-bearing, the same
    way `test_revoke_consent_endpoint_serializes_against_a_concurrent_
    regrant` below proves its own row lock: a raw connection holds the
    workspace-scoped advisory lock first, a background thread's own
    `disable` call (which needs that same lock inside `cascade_email_
    revocation`) genuinely blocks rather than racing ahead, and once the
    lock is released the disable proceeds and correctly treats the
    colliding id as ambiguous (the other owner's message is still live),
    leaving the shared evidence row unpurged -- the same outcome the
    sequential-case fix already proves, now proven to hold under genuine
    concurrency too.
    """
    ctx = gmail_revocation_context
    seeded = _seed_full_cascade_fixture(ctx)
    colliding_external_id = seeded["external_message_id"]

    other_owner_id = uuid4()
    other_email = f"other-owner-{uuid4()}@example.test"
    ctx["extra_owner_emails"].append(other_email)
    with engine.begin() as connection:
        create_identity(
            connection,
            workspace_id=ctx["workspace_id"],
            user_id=other_owner_id,
            email=other_email,
            now=ctx["now"],
        )
        _insert_enabled_email_domain(
            connection, workspace_id=ctx["workspace_id"], owner_id=other_owner_id, now=ctx["now"]
        )
        other_thread_id = _insert_thread(
            connection,
            workspace_id=ctx["workspace_id"],
            owner_id=other_owner_id,
            account_id=ctx["account_id"],
            now=ctx["now"],
        )
        _insert_message(
            connection,
            workspace_id=ctx["workspace_id"],
            owner_id=other_owner_id,
            thread_id=other_thread_id,
            external_message_id=colliding_external_id,
            now=ctx["now"],
        )

    def _disable() -> Any:
        return ctx["client"].post(
            "/api/v1/personal/domains/email/disable",
            headers=_headers(ctx["token"], str(uuid4())),
        )

    with engine.connect() as holder:
        holder.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
            {"lock_key": f"{ctx['workspace_id']}:gmail_revocation_evidence"},
        )
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(_disable)
            with pytest.raises(FutureTimeoutError):
                future.result(timeout=1)

            holder.commit()
            resp = future.result(timeout=5)
        assert resp.status_code == 200, resp.text

    # This owner's own message is purged, but the shared evidence row
    # survives -- the id was correctly treated as ambiguous once the
    # blocked cascade finally acquired the lock and ran its check.
    with engine.begin() as connection:
        evidence_row = connection.execute(
            text(
                "SELECT id FROM pkos_evidence WHERE workspace_id = :workspace_id "
                "AND source_ref = :source_ref"
            ),
            {
                "workspace_id": ctx["workspace_id"],
                "source_ref": f"gmail:detect_action:{colliding_external_id}",
            },
        ).one_or_none()
    assert evidence_row is not None, "ambiguous evidence must be left alone, not purged"


def test_sequential_disables_by_two_colliding_owners_do_not_leak_the_first_owners_evidence(
    gmail_revocation_context: dict,
) -> None:
    """Loop 2 round 8 review finding: the fix directly above only ever
    defends against a *concurrent* collision -- both owners' `email_
    messages` rows for the shared id are still live at the moment either
    owner's cascade runs. It does not defend against a *sequential* one:
    owner A disables first, correctly leaving the shared evidence row
    alone (owner B's row is still live) but still deleting *A's own*
    `email_messages` row for that id -- which was the only thing that made
    the id ambiguous in the first place. If owner B then disables
    independently afterwards, B's own ambiguity check would find no
    *other* owner's live row left for that id and (before this fix) wrongly
    conclude it is safe, purging the shared evidence row after all. This
    proves `email_message_id_purge_log` (migration `0076`) closes that
    window: A's own cascade logs the id before deleting A's `email_
    messages` row, and B's own later, independent cascade run still finds
    A's log entry and correctly treats the id as ambiguous.

    Owner B's own cascade is invoked directly (`gmail_revocation.cascade_
    email_revocation`), not via the HTTP endpoint the rest of this file
    uses -- a deliberate simplification to keep this test isolated to the
    one thing it exists to prove (the ledger closes the sequential leak),
    rather than a constraint (a second `sessions` row for owner B is no
    harder to add here than the second identity this test already creates
    below). This does mean owner B's own leg of this test does not also
    exercise `_disable_domain`'s own commit -> `session.close()` ->
    `finish_gmail_revocation` sequencing the way owner A's leg does (via
    the real HTTP call below) -- that sequencing is already proven
    generically, for a single owner, by `test_disable_domain_releases_
    pool_connection_and_row_lock_before_revoke_call` above.
    """
    ctx = gmail_revocation_context
    seeded = _seed_full_cascade_fixture(ctx)
    colliding_external_id = seeded["external_message_id"]

    other_owner_id = uuid4()
    other_email = f"other-owner-{uuid4()}@example.test"
    ctx["extra_owner_emails"].append(other_email)
    with engine.begin() as connection:
        create_identity(
            connection,
            workspace_id=ctx["workspace_id"],
            user_id=other_owner_id,
            email=other_email,
            now=ctx["now"],
        )
        _insert_enabled_email_domain(
            connection, workspace_id=ctx["workspace_id"], owner_id=other_owner_id, now=ctx["now"]
        )
        other_thread_id = _insert_thread(
            connection,
            workspace_id=ctx["workspace_id"],
            owner_id=other_owner_id,
            account_id=ctx["account_id"],
            now=ctx["now"],
        )
        _insert_message(
            connection,
            workspace_id=ctx["workspace_id"],
            owner_id=other_owner_id,
            thread_id=other_thread_id,
            external_message_id=colliding_external_id,
            now=ctx["now"],
        )

    # Owner A (the fixture's own owner) disables first. The shared evidence
    # survives -- owner B's own row is still live -- but A's own `email_
    # messages` row for the colliding id is gone afterwards.
    resp = ctx["client"].post(
        "/api/v1/personal/domains/email/disable",
        headers=_headers(ctx["token"], str(uuid4())),
    )
    assert resp.status_code == 200, resp.text
    with engine.begin() as connection:
        evidence_after_a = connection.execute(
            text(
                "SELECT id FROM pkos_evidence WHERE workspace_id = :workspace_id "
                "AND source_ref = :source_ref"
            ),
            {
                "workspace_id": ctx["workspace_id"],
                "source_ref": f"gmail:detect_action:{colliding_external_id}",
            },
        ).one_or_none()
    assert evidence_after_a is not None, "A's own cascade must leave the ambiguous evidence alone"

    # Owner B disables independently afterwards. Before migration 0076, B's
    # own ambiguity check would find no other owner's live row left (A's is
    # gone) and wrongly purge the shared evidence row here.
    with SessionFactory() as session, session.begin():
        gmail_revocation.cascade_email_revocation(
            session,
            AuthContext(workspace_id=ctx["workspace_id"], user_id=other_owner_id, timezone="UTC"),
            ctx["now"],
        )

    with engine.begin() as connection:
        evidence_after_b = connection.execute(
            text(
                "SELECT id FROM pkos_evidence WHERE workspace_id = :workspace_id "
                "AND source_ref = :source_ref"
            ),
            {
                "workspace_id": ctx["workspace_id"],
                "source_ref": f"gmail:detect_action:{colliding_external_id}",
            },
        ).one_or_none()
    assert evidence_after_b is not None, (
        "B's own later, independent cascade must not leak A's preserved evidence"
    )


def test_revoke_consent_endpoint_rejects_a_stale_already_revoked_consent_id(
    gmail_revocation_context: dict,
) -> None:
    """Loop 2 round 8 review finding, check refined round 9: `revoke_
    consent_endpoint`'s own consent lookup did not check whether a `consent_
    id` had since been superseded by a later grant, so a stale/already-
    revoked `consent_id` -- reused after the owner re-granted `email`
    consent -- would still resolve to `domain_key = 'email'` and trigger a
    fresh disable-and-purge cascade against the *current* (freshly re-
    granted) state. Revokes the fixture's own original consent once (which
    disables the domain and purges the fixture's own seeded data), then
    re-grants a brand new consent/domain-enable, seeds fresh cascade data
    under the new grant, and replays the ORIGINAL (now stale) `consent_id`
    -- asserting it is rejected (`404 CONSENT_NOT_FOUND`, the same non-
    disclosing shape every other lookup in this module already uses) and
    the freshly re-granted data survives untouched. See the companion test
    directly below for the case this must NOT reject: a same-`Idempotency-
    Key` retry of a revoke that already succeeded, with no re-grant in
    between (round 9 review finding: a plain `revoked_at IS NULL` check,
    round 8's own first attempt at this fix, wrongly rejected that case).
    """
    ctx = gmail_revocation_context
    stale_consent_id = ctx["consent_id"]

    first_resp = ctx["client"].post(
        f"/api/v1/personal/consents/{stale_consent_id}/revoke",
        headers=_headers(ctx["token"], str(uuid4())),
    )
    assert first_resp.status_code == 200, first_resp.text
    assert _connector_status(ctx["workspace_id"], ctx["account_id"]) == "disconnected"

    # Re-grant: a fresh `POST /consents` enable, then reactivate the
    # connector and reconnect fresh Gmail data under the new grant.
    enable_resp = ctx["client"].post(
        "/api/v1/personal/consents",
        json={"domain_key": "email"},
        headers=_headers(ctx["token"], str(uuid4())),
    )
    assert enable_resp.status_code == 201, enable_resp.text
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE connector_accounts SET status = 'active', disconnected_at = NULL "
                "WHERE workspace_id = :workspace_id AND id = :id"
            ),
            {"workspace_id": ctx["workspace_id"], "id": ctx["account_id"]},
        )
    seeded = _seed_full_cascade_fixture(ctx)

    # Replay the ORIGINAL, now-stale `consent_id` from the first revoke.
    replay_resp = ctx["client"].post(
        f"/api/v1/personal/consents/{stale_consent_id}/revoke",
        headers=_headers(ctx["token"], str(uuid4())),
    )
    assert replay_resp.status_code == 404, replay_resp.text
    assert replay_resp.json()["error"]["code"] == "CONSENT_NOT_FOUND"

    # The freshly re-granted state is untouched by the rejected replay.
    assert _connector_status(ctx["workspace_id"], ctx["account_id"]) == "active"
    with engine.begin() as connection:
        thread_row = connection.execute(
            text("SELECT id FROM email_threads WHERE workspace_id = :workspace_id AND id = :id"),
            {"workspace_id": ctx["workspace_id"], "id": seeded["thread_id"]},
        ).one_or_none()
    assert thread_row is not None, "the replayed stale consent_id must not re-trigger the cascade"


def test_revoke_consent_endpoint_idempotency_key_replay_without_regrant_still_succeeds(
    gmail_revocation_context: dict,
) -> None:
    """Loop 2 round 9 review finding: round 8's own first fix for the stale-
    consent-id replay above (a plain `AND revoked_at IS NULL`) over-
    corrected -- it also rejected this test's own scenario, a *legitimate*
    `Idempotency-Key` retry of a revoke that had already succeeded, with no
    re-grant in between. The first successful call itself sets `revoked_
    at`, and that plain filter ran before `_disable_domain`'s own
    idempotency-cache check ever got a chance to fire, so the identical
    retry 404'd instead of returning the cached response. The refined
    check (`revoked_at IS NULL OR NOT EXISTS (a later grant)`) still
    resolves `domain_key` for a revoked consent nothing has superseded, so
    the retry reaches `_disable_domain`'s own idempotency cache -- proven
    here with the SAME `Idempotency-Key` on both calls, asserting the
    second call is a `200`, not a `404`, with the identical cached
    response body. Response-body equality alone would not by itself rule
    out a harmless independent recompute landing on the same values
    (`domain.enabled` is already `False` either way, so a non-cached
    second pass through `_disable_domain` would skip the cascade and still
    return an identically-shaped response) -- Loop 2 round 10 review
    finding: the direct proof is that `write_side_effects` inserts one
    fresh `audit_events` row per real execution, with no `ON CONFLICT`
    dedup, so a genuine second execution would leave two rows for this
    domain's `personal_domain.disabled` event; asserting exactly one below
    is what actually distinguishes a real cache hit from a coincidence.
    """
    ctx = gmail_revocation_context
    key = str(uuid4())

    first_resp = ctx["client"].post(
        f"/api/v1/personal/consents/{ctx['consent_id']}/revoke",
        headers=_headers(ctx["token"], key),
    )
    assert first_resp.status_code == 200, first_resp.text

    replay_resp = ctx["client"].post(
        f"/api/v1/personal/consents/{ctx['consent_id']}/revoke",
        headers=_headers(ctx["token"], key),
    )
    assert replay_resp.status_code == 200, replay_resp.text
    # `request_id`/`correlation_id` are per-HTTP-call tracing fields the
    # outer middleware injects into every response envelope -- they are not
    # part of `DomainResponse` itself (see that model's own field list) and
    # legitimately differ between these two calls even though the second is
    # served from the idempotency cache. Compare only the cached payload's
    # own fields; the `audit_event_count == 1` assertion below is the actual
    # proof of a cache hit, not this equality check.
    untraced = {"request_id", "correlation_id"}
    assert {k: v for k, v in replay_resp.json().items() if k not in untraced} == {
        k: v for k, v in first_resp.json().items() if k not in untraced
    }

    with engine.begin() as connection:
        audit_event_count = connection.execute(
            text(
                "SELECT COUNT(*) FROM audit_events WHERE workspace_id = :workspace_id "
                "AND event_type = 'personal_domain.disabled'"
            ),
            {"workspace_id": ctx["workspace_id"]},
        ).scalar_one()
    assert audit_event_count == 1, "the replay must be served from cache, not re-executed"


def test_revoke_consent_endpoint_serializes_against_a_concurrent_regrant(
    gmail_revocation_context: dict,
) -> None:
    """Loop 2 round 10 review finding: the stale-consent-id check two
    tests above used to run in its own, separately-committed transaction
    *before* `_disable_domain`'s own `FOR UPDATE` lock on the `personal_
    domains` row -- a genuine TOCTOU window existed between that check and
    the actual mutation, where a concurrent re-grant could commit in
    between and `_disable_domain` would go on to disable-and-cascade-purge
    the freshly re-granted state anyway. Fixed by moving the re-validation
    *inside* `_disable_domain`'s own transaction, immediately after it
    acquires the same `personal_domains` row lock `_enable_domain`'s own
    re-grant path (`existing = get_domain(..., for_update=True)`) also
    acquires.

    Loop 2 round 11 review finding: this test used to only prove the
    underlying lock-contention mechanism in the abstract (a raw connection
    holding the row locked blocks an unrelated `POST /consents` call) and
    never actually exercised `revoke_consent_endpoint`'s own code path
    under real concurrency, so a regression in the `consent_id` re-check
    itself would not have failed this test; it also managed the raw
    connection with `text("BEGIN")`/`text("COMMIT")` executed before a
    `try` block, a genuine leak window (a raise during setup would skip
    `finally`) that also bypasses SQLAlchemy's own autobegin tracking.
    Rewritten to prove the real end-to-end behavior with a properly scoped
    connection: revoke the consent for real first (so `revoked_at` is
    genuinely set), hold the `personal_domains` row lock, submit a
    *replay* of that now-superseded `consent_id` via `revoke_consent_
    endpoint` in a background thread (blocks acquiring the same lock
    inside `_disable_domain`), perform the exact regrant `_enable_domain`
    would over the SAME held connection while the replay is blocked, then
    release the lock -- the replay must then observe the fresh grant and
    reject with `404 CONSENT_NOT_FOUND`, not silently disable-and-cascade
    the just-re-granted domain.
    """
    ctx = gmail_revocation_context

    first_resp = ctx["client"].post(
        f"/api/v1/personal/consents/{ctx['consent_id']}/revoke",
        headers=_headers(ctx["token"], str(uuid4())),
    )
    assert first_resp.status_code == 200, first_resp.text

    def _replay_stale_revoke() -> Any:
        return ctx["client"].post(
            f"/api/v1/personal/consents/{ctx['consent_id']}/revoke",
            headers=_headers(ctx["token"], str(uuid4())),
        )

    with engine.connect() as holder:
        holder.execute(
            text(
                "SELECT id FROM personal_domains WHERE workspace_id = :workspace_id "
                "AND owner_id = :owner_id AND domain_key = 'email' FOR UPDATE"
            ),
            {"workspace_id": ctx["workspace_id"], "owner_id": ctx["owner_id"]},
        )
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(_replay_stale_revoke)
            with pytest.raises(FutureTimeoutError):
                future.result(timeout=1)

            regrant_now = datetime.now(UTC)
            holder.execute(
                text(
                    "UPDATE personal_domains SET enabled = true, enabled_at = :now, "
                    "updated_at = :now, updated_by = :owner_id, version = version + 1 "
                    "WHERE workspace_id = :workspace_id AND owner_id = :owner_id "
                    "AND domain_key = 'email'"
                ),
                {
                    "now": regrant_now,
                    "workspace_id": ctx["workspace_id"],
                    "owner_id": ctx["owner_id"],
                },
            )
            holder.execute(
                text(
                    "INSERT INTO domain_consents (id, workspace_id, owner_id, domain_key, "
                    "granted_at, created_at) "
                    "VALUES (:id, :workspace_id, :owner_id, 'email', :now, :now)"
                ),
                {
                    "id": uuid4(),
                    "workspace_id": ctx["workspace_id"],
                    "owner_id": ctx["owner_id"],
                    "now": regrant_now,
                },
            )
            holder.commit()

            replay_resp = future.result(timeout=5)

    assert replay_resp.status_code == 404, replay_resp.text
    assert replay_resp.json()["error"]["code"] == "CONSENT_NOT_FOUND"


def test_disable_domain_idempotency_key_reused_after_an_oauth_reconnect_is_rejected(
    gmail_revocation_context: dict,
) -> None:
    """Loop 2 round 22 review finding (MEDIUM-HIGH): round 21's own fix
    (item 9 above) checked `domain.enabled` on a cache hit, but that is
    not the only way a reused `Idempotency-Key` can be replayed against a
    materially different, later request. `gmail_oauth.py:gmail_oauth_
    callback_endpoint` -- the sole reconnect path for a `gmail`-provider
    `connector_accounts` row -- reactivates it straight back to `status=
    'active'` with no reference whatsoever to `personal_domains`/`domain_
    consents`. A real disable, followed by a genuine Gmail reconnect
    through that unrelated OAuth flow (simulated here the same way the
    concurrent-regrant test above does, by updating `connector_accounts`
    directly, since exercising the real OAuth callback needs Google
    transport mocking this file does not otherwise set up), leaves
    `domain.enabled` still `False` -- round 21's own fix alone would have
    let the replay through and silently served the stale cached response
    again, leaving the freshly-reconnected connector row untouched.
    """
    ctx = gmail_revocation_context
    _seed_full_cascade_fixture(ctx)
    key = str(uuid4())

    first = ctx["client"].post(
        "/api/v1/personal/domains/email/disable", headers=_headers(ctx["token"], key)
    )
    assert first.status_code == 200, first.text
    assert _connector_status(ctx["workspace_id"], ctx["account_id"]) == "disconnected"

    # Simulates a genuine Gmail reconnect via the real OAuth callback
    # endpoint, which never touches `personal_domains`/`domain_consents`.
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE connector_accounts SET status = 'active', disconnected_at = NULL "
                "WHERE workspace_id = :workspace_id AND id = :id"
            ),
            {"workspace_id": ctx["workspace_id"], "id": ctx["account_id"]},
        )

    replay = ctx["client"].post(
        "/api/v1/personal/domains/email/disable", headers=_headers(ctx["token"], key)
    )
    assert replay.status_code == 409, replay.text
    assert replay.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"

    # The rejected replay must not have disconnected the freshly-
    # reconnected account either.
    assert _connector_status(ctx["workspace_id"], ctx["account_id"]) == "active"


def test_delete_domain_endpoint_idempotency_key_reused_after_an_oauth_reconnect_is_rejected(
    gmail_revocation_context: dict,
) -> None:
    """Loop 2 round 22 review finding: the identical gap as the test above,
    for `delete_domain_endpoint`.
    """
    ctx = gmail_revocation_context
    _seed_full_cascade_fixture(ctx)
    key = str(uuid4())

    first = ctx["client"].post(
        "/api/v1/personal/domains/email/delete", headers=_headers(ctx["token"], key)
    )
    assert first.status_code == 200, first.text
    assert _connector_status(ctx["workspace_id"], ctx["account_id"]) == "disconnected"

    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE connector_accounts SET status = 'active', disconnected_at = NULL "
                "WHERE workspace_id = :workspace_id AND id = :id"
            ),
            {"workspace_id": ctx["workspace_id"], "id": ctx["account_id"]},
        )

    replay = ctx["client"].post(
        "/api/v1/personal/domains/email/delete", headers=_headers(ctx["token"], key)
    )
    assert replay.status_code == 409, replay.text
    assert replay.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"

    assert _connector_status(ctx["workspace_id"], ctx["account_id"]) == "active"
