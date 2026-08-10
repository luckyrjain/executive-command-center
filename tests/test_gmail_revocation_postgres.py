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
"""

from collections.abc import Iterator
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
from ecc.database import engine
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


def _cleanup_workspace(workspace_id: UUID, *, email: str = _OWNER_EMAIL) -> None:
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
        connection.execute(text("DELETE FROM accounts WHERE email = :email"), {"email": email})


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
    try:
        yield {
            "client": client,
            "token": token,
            "workspace_id": workspace_id,
            "owner_id": owner_id,
            "account_id": account_id,
            "consent_id": consent_id,
            "now": now,
        }
    finally:
        client.close()
        _cleanup_workspace(workspace_id)


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
    for field in ("id", "domain_key", "enabled", "version"):
        assert second_body[field] == first_body[field]

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
