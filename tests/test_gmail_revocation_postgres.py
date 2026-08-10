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
9. An owner with *two* simultaneously-active `gmail` connector accounts
   (no per-owner uniqueness on `provider = 'gmail'` -- only `external_
   account_id` is unique) has both disconnected by one cascade run --
   Loop 2 round 1 review finding: the original `.one_or_none()` raised
   `MultipleResultsFound` here, aborting the entire cascade.
10. `domains.py:_disable_domain`'s `session.close()` before `finish_
    gmail_revocation(...)` actually releases the pooled connection and the
    cascade's own `FOR UPDATE` lock before the (potentially slow) Google
    revoke call runs -- Loop 2 round 4 review finding: `connector_
    accounts.py:disable_connector_endpoint`'s own identical pattern has a
    dedicated concurrency test proving this; the two call sites that
    actually reach `finish_gmail_revocation` for `gmail` (this module's
    own `_disable_domain`/`delete_domain_endpoint`) had none.
11. `export_deletion.py:delete_domain_endpoint`'s own identical `session.
    close()` before `finish_gmail_revocation(...)` sequence releases the
    pooled connection and row lock before the blocking Google revoke call
    runs, same as item 10 above -- Loop 2 round 5 review finding: item 10
    only ever covered `disable_domain_endpoint`, leaving the OTHER call
    site that reaches `finish_gmail_revocation` for `gmail` unproven.
12. A second owner's own Gmail data (thread/message/attention item/
    connector) in the SAME workspace is completely untouched by the first
    owner's disable -- Loop 2 round 4 review finding: every test above
    exercises exactly one owner, so a regression dropping an `owner_id`
    clause from any of the cascade's five raw-SQL statements would go
    undetected.
13. Two owners whose own Gmail accounts each produce a message sharing the
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
"""

import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
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

from ecc.config import get_settings
from ecc.database import STATEMENT_TIMEOUT_MS, engine
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


def _cleanup_workspace(workspace_id: UUID, *, emails: list[str] | None = None) -> None:
    """`emails` defaults to just `_OWNER_EMAIL`; tests that seed a second
    owner (Loop 2 round 4-5's cross-owner tests) pass that owner's own
    email too via `ctx["extra_owner_emails"]`, appended by the test itself
    -- must happen here, in the SAME transaction sequence as the
    workspace-scoped `users` deletion below, not inline in the test body:
    `accounts` has a `RESTRICT` FK from `users` (`fk_users_account`), so
    deleting a second owner's `accounts` row before their own `users` row
    is gone raises `RestrictViolation` (Loop 2 round 6 review: CI caught
    this exact ordering bug in the original version of this fix).
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
            {"emails": emails if emails is not None else [_OWNER_EMAIL]},
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

    # `_cleanup_workspace` (the fixture's own teardown) only ever knew the
    # primary owner's `accounts` row by email -- register this test's own
    # second identity so the fixture cleans it up too, in the correct
    # order (after this workspace's `users` rows are gone). Deleting it
    # here directly would raise `RestrictViolation` on `fk_users_account`
    # since the second owner's own `users` row still exists at this point
    # (Loop 2 round 6 review: CI caught this exact ordering bug).
    ctx["extra_owner_emails"].append(other_email)


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

    # See the identical comment in `test_disable_domain_does_not_affect_a_
    # different_owners_gmail_data` above -- deleting this second identity's
    # `accounts` row directly here would raise `RestrictViolation`.
    ctx["extra_owner_emails"].append(other_email)
