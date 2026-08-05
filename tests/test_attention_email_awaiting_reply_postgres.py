"""Phase 10 Task 3: the "awaiting reply" heuristic (`ecc.domains.attention.
attention._score_awaiting_reply`) and its `regenerate_attention` wiring
(`entity_type='email_thread'`).

Covers, per the implementation plan's own Task 3 scope:

1. An `email_threads` row whose own last message (by `sent_at`) is
   inbound, from a sender already resolved into `pkos_nodes` (via
   `entity_aliases`, the same lookup `gmail_adapter.py`'s own
   `_resolve_or_create_person` performs at write time), surfaces in
   `POST /api/v1/attention/regenerate`'s own response as an
   `entity_type='email_thread'` item.
2. A thread whose last message is outbound (already replied) does not.
3. A thread whose last message is inbound but the sender has never been
   resolved into `pkos_nodes` does not.
4. A thread whose owner's `email` personal domain is disabled does not.
5. Staleness factors (`stale_7d`/`stale_14d`) age in as the last inbound
   message's own age crosses those thresholds, reusing the exact
   generic `stale_7d_points`/`stale_14d_points` policy fields
   `_score_waiting` already shares -- not a second, email-specific
   threshold pair.
6. A thread that later receives an outbound reply is pruned from the
   attention queue on the next `regenerate_attention` call (the
   `email_thread` branch of the stale-row `DELETE`), the same lifecycle
   `test_attention_waiting_postgres.py`'s own
   `test_waiting_link_surfaces_in_attention_queue_and_ages` verifies for
   `waiting_link`.
7. A sender whose email address contains a character where Postgres's
   `LOWER()` and Python's `_normalize_email` (`.strip().casefold()`) do
   not agree (the German sharp s, `ß`, which `.casefold()` expands to
   `ss` but Postgres's `LOWER()` leaves untouched) still surfaces as
   awaiting reply -- round 1 review finding: the eligibility query used
   to compare `entity_aliases.normalized_value` against
   `LOWER(TRIM(sender))` computed in SQL, which silently failed to match
   for exactly this kind of address even though the sender genuinely
   resolves to a known contact.
8. A dismissed `email_thread` item stays dismissed across a subsequent
   `regenerate_attention` call where the underlying thread hasn't
   changed -- round 1 review finding: no existing test, for any entity
   type, exercised `_upsert_batch`'s dismissed-state-preservation logic
   end to end, and `email_thread`'s own `source_entity_version` is a
   derived proxy (`email_threads.updated_at`, not a real column) rather
   than the real optimistic-concurrency `version` every other entity
   type has, making this the one place a bug in that derivation would
   show up as a spurious un-dismiss.
"""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from hmac import new
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from identity_fixtures import create_identity
from sqlalchemy import text

from ecc.config import get_settings
from ecc.database import engine
from ecc.main import app

settings = get_settings()
pytestmark = pytest.mark.skipif(
    not settings.database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)


@pytest.fixture
def email_attention_test_context() -> Iterator[tuple[TestClient, UUID, UUID, str]]:
    """A `workspaces`/`users`/`sessions` row set (for the HTTP-level
    `regenerate`/`list` calls) plus `personal_domains`/`domain_consents`
    for the `email` domain (required by `regenerate_attention`'s own
    eligibility query, mirroring `test_gmail_connector_sync_postgres.py`'s
    `seeded_gmail_account` fixture's identical two inserts) -- no
    `connector_accounts` row, since this task's own eligibility query
    never reads one.
    """
    workspace_id = uuid4()
    user_id = uuid4()
    token = f"session-{uuid4()}"
    now = datetime.now(UTC)

    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, 'Email Attention Test', 'UTC', :created_at)"
            ),
            {"id": workspace_id, "created_at": now},
        )
        create_identity(
            connection,
            workspace_id=workspace_id,
            user_id=user_id,
            email=f"{user_id}@example.test",
            now=now,
        )
        connection.execute(
            text(
                "INSERT INTO sessions (id, workspace_id, user_id, token_hash, "
                "expires_at, last_seen_at) "
                "VALUES (:id, :workspace_id, :user_id, :token_hash, :expires_at, :last_seen_at)"
            ),
            {
                "id": uuid4(),
                "workspace_id": workspace_id,
                "user_id": user_id,
                "token_hash": sha256(token.encode()).hexdigest(),
                "expires_at": now + timedelta(hours=1),
                "last_seen_at": now,
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
            {"id": uuid4(), "workspace_id": workspace_id, "owner_id": user_id, "now": now},
        )
        connection.execute(
            text(
                "INSERT INTO domain_consents (id, workspace_id, owner_id, domain_key, "
                "granted_at, created_at) "
                "VALUES (:id, :workspace_id, :owner_id, 'email', :now, :now)"
            ),
            {"id": uuid4(), "workspace_id": workspace_id, "owner_id": user_id, "now": now},
        )

    client = TestClient(app)
    client.cookies.set("ecc_session", token)
    try:
        yield client, workspace_id, user_id, token
    finally:
        client.close()
        with engine.begin() as connection:
            for table in (
                "attention_items",
                "event_outbox",
                "audit_events",
                "idempotency_records",
                "entity_aliases",
                "pkos_evidence",
                "pkos_nodes",
                "email_messages",
                "email_threads",
                # `_seed_thread`'s own `connector_account_id` (see its
                # docstring) -- each thread seeds a fresh row here, deleted
                # before `users` since `connector_accounts.created_by`/
                # `updated_by`/`owner_id` all FK to it.
                "connector_accounts",
                "domain_consents",
                "personal_domains",
                "sessions",
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


def _headers(token: str, key: str | None = None) -> dict[str, str]:
    csrf = new(settings.session_secret.encode(), token.encode(), "sha256").hexdigest()
    headers = {"X-CSRF-Token": csrf, "X-Correlation-ID": str(uuid4())}
    if key is not None:
        headers["Idempotency-Key"] = key
    return headers


def _seed_thread(
    workspace_id: UUID, owner_id: UUID, subject: str, last_message_at: datetime
) -> UUID:
    thread_id = uuid4()
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO email_threads (
                    id, workspace_id, owner_id, domain_key, connector_account_id,
                    external_thread_id, subject, last_message_at, created_at, updated_at
                ) VALUES (
                    :id, :workspace_id, :owner_id, 'email', :fake_connector_account_id,
                    :external_thread_id, :subject, :last_message_at, :now, :now
                )
                """
            ),
            {
                "id": thread_id,
                "workspace_id": workspace_id,
                "owner_id": owner_id,
                # No real `connector_accounts` row exists in this fixture
                # (see the fixture's own docstring) -- `email_threads.
                # connector_account_id` has no FK to it in isolation, only
                # a composite one alongside `workspace_id`; a random UUID
                # here would violate that FK, so `email_threads` itself
                # would need `connector_accounts` seeded too. Simpler: seed
                # a `connector_accounts` row per thread here instead of
                # widening the shared fixture for a column no eligibility
                # query in this file's own scope reads.
                "fake_connector_account_id": _seed_connector_account(workspace_id, owner_id),
                "external_thread_id": f"thread-{thread_id}",
                "subject": subject,
                "last_message_at": last_message_at,
                "now": now,
            },
        )
    return thread_id


def _seed_connector_account(workspace_id: UUID, owner_id: UUID) -> UUID:
    account_id = uuid4()
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO connector_accounts (
                    id, workspace_id, provider, external_account_id, display_name,
                    granted_scopes, encrypted_credentials, status, version,
                    owner_id, created_by, updated_by, created_at, updated_at
                ) VALUES (
                    :id, :workspace_id, 'gmail', :external_account_id, 'Awaiting reply test',
                    ARRAY['https://www.googleapis.com/auth/gmail.metadata'], :encrypted,
                    'active', 1, :owner_id, :owner_id, :owner_id, :now, :now
                )
                """
            ),
            {
                "id": account_id,
                "workspace_id": workspace_id,
                "external_account_id": f"{account_id}@example.test",
                "encrypted": b"not-a-real-credential-blob",
                "owner_id": owner_id,
                "now": now,
            },
        )
    return account_id


def _seed_message(
    workspace_id: UUID,
    owner_id: UUID,
    thread_id: UUID,
    *,
    sender: str,
    direction: str,
    sent_at: datetime,
) -> UUID:
    message_id = uuid4()
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO email_messages (
                    id, workspace_id, owner_id, thread_id, external_message_id,
                    sender, recipients, sent_at, direction, created_at, updated_at
                ) VALUES (
                    :id, :workspace_id, :owner_id, :thread_id, :external_message_id,
                    :sender, ARRAY[:owner_email], :sent_at, :direction, :now, :now
                )
                """
            ),
            {
                "id": message_id,
                "workspace_id": workspace_id,
                "owner_id": owner_id,
                "thread_id": thread_id,
                "external_message_id": f"msg-{message_id}",
                "sender": sender,
                "owner_email": f"{owner_id}@example.test",
                "sent_at": sent_at,
                "direction": direction,
                "now": now,
            },
        )
        # `_upsert_thread`'s own real production behaviour (`gmail_
        # adapter.py`) bumps `email_threads.updated_at` on every message
        # write that touches the thread -- reproduced here directly since
        # this fixture writes `email_messages` without going through the
        # real adapter, and `regenerate_attention`'s own `email_thread`
        # rows use `updated_at` as their `_upsert_batch` version proxy.
        connection.execute(
            text(
                "UPDATE email_threads SET updated_at = :now, "
                "last_message_at = GREATEST(last_message_at, :sent_at) "
                "WHERE id = :thread_id"
            ),
            {"now": now, "sent_at": sent_at, "thread_id": thread_id},
        )
    return message_id


def _resolve_sender(workspace_id: UUID, email: str) -> None:
    """Mirrors `gmail_adapter.py`'s own `_resolve_or_create_person`: a
    `pkos_nodes` person, a `pkos_evidence` row, and an `entity_aliases` row
    keyed on the normalized (`.strip().casefold()`, reproduced here as
    `LOWER(TRIM(...))`'s Python-side equivalent) email address --
    `regenerate_attention`'s own eligibility query looks this up the same
    way at read time `_resolve_or_create_person` writes it at sync time.
    """
    node_id = uuid4()
    evidence_id = uuid4()
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO pkos_nodes (
                    id, workspace_id, node_type, canonical_name, attributes,
                    status, confidence, version, created_at, updated_at
                ) VALUES (
                    :id, :workspace_id, 'person', :name, '{}'::jsonb,
                    'active', 1.00, 1, :now, :now
                )
                """
            ),
            {"id": node_id, "workspace_id": workspace_id, "name": email, "now": now},
        )
        connection.execute(
            text(
                """
                INSERT INTO pkos_evidence (
                    id, workspace_id, node_id, source_type, source_ref, sha256, captured_at
                ) VALUES (
                    :id, :workspace_id, :node_id, 'gmail_sync', :email, :sha256, :now
                )
                """
            ),
            {
                "id": evidence_id,
                "workspace_id": workspace_id,
                "node_id": node_id,
                "email": email,
                "sha256": sha256(email.encode()).hexdigest(),
                "now": now,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO entity_aliases (
                    id, workspace_id, entity_id, alias_type, normalized_value,
                    source_id, confidence, created_at
                ) VALUES (
                    :id, :workspace_id, :node_id, 'email', :normalized,
                    :source_id, 1.00, :now
                )
                """
            ),
            {
                "id": uuid4(),
                "workspace_id": workspace_id,
                "node_id": node_id,
                "normalized": email.strip().casefold(),
                "source_id": evidence_id,
                "now": now,
            },
        )


def test_inbound_thread_from_known_contact_surfaces_as_awaiting_reply(
    email_attention_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, owner_id, token = email_attention_test_context
    now = datetime.now(UTC)
    sender = "known.contact@example.test"
    _resolve_sender(workspace_id, sender)
    thread_id = _seed_thread(workspace_id, owner_id, "Need your sign-off", now)
    _seed_message(
        workspace_id, owner_id, thread_id, sender=sender, direction="inbound", sent_at=now
    )

    regenerate = client.post("/api/v1/attention/regenerate", headers=_headers(token), json={})
    assert regenerate.status_code == 200, regenerate.text
    item = next(i for i in regenerate.json()["items"] if i["entity_id"] == str(thread_id))
    assert item["entity_type"] == "email_thread"
    factor_codes = {f["code"] for f in item["factors"]}
    assert "awaiting_reply" in factor_codes


def test_outbound_reply_does_not_surface_as_awaiting_reply(
    email_attention_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, owner_id, token = email_attention_test_context
    now = datetime.now(UTC)
    sender = "known.contact@example.test"
    _resolve_sender(workspace_id, sender)
    thread_id = _seed_thread(workspace_id, owner_id, "Already answered", now - timedelta(hours=2))
    _seed_message(
        workspace_id,
        owner_id,
        thread_id,
        sender=sender,
        direction="inbound",
        sent_at=now - timedelta(hours=2),
    )
    # The owner's own reply -- last message in the thread is now outbound.
    _seed_message(
        workspace_id,
        owner_id,
        thread_id,
        sender=f"{owner_id}@example.test",
        direction="outbound",
        sent_at=now,
    )

    regenerate = client.post("/api/v1/attention/regenerate", headers=_headers(token), json={})
    assert regenerate.status_code == 200, regenerate.text
    assert all(i["entity_id"] != str(thread_id) for i in regenerate.json()["items"])


def test_inbound_thread_from_unresolved_sender_does_not_surface(
    email_attention_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    """The heuristic's own third condition -- "sender resolves to a known
    `pkos_nodes` contact" -- is not automatically true just because a
    message exists; a sender nobody has ever been linked into the entity
    graph for must not surface, since `attention.explain_item`'s own
    grounding has nothing to cite for who this even is.
    """
    client, workspace_id, owner_id, token = email_attention_test_context
    now = datetime.now(UTC)
    thread_id = _seed_thread(workspace_id, owner_id, "Stranger danger", now)
    _seed_message(
        workspace_id,
        owner_id,
        thread_id,
        sender="never.resolved@example.test",
        direction="inbound",
        sent_at=now,
    )

    regenerate = client.post("/api/v1/attention/regenerate", headers=_headers(token), json={})
    assert regenerate.status_code == 200, regenerate.text
    assert all(i["entity_id"] != str(thread_id) for i in regenerate.json()["items"])


def test_disabled_email_domain_does_not_surface_awaiting_reply(
    email_attention_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, owner_id, token = email_attention_test_context
    now = datetime.now(UTC)
    sender = "known.contact@example.test"
    _resolve_sender(workspace_id, sender)
    thread_id = _seed_thread(workspace_id, owner_id, "Domain disabled mid-flight", now)
    _seed_message(
        workspace_id, owner_id, thread_id, sender=sender, direction="inbound", sent_at=now
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE personal_domains SET enabled = false "
                "WHERE workspace_id = :workspace_id AND owner_id = :owner_id "
                "AND domain_key = 'email'"
            ),
            {"workspace_id": workspace_id, "owner_id": owner_id},
        )

    regenerate = client.post("/api/v1/attention/regenerate", headers=_headers(token), json={})
    assert regenerate.status_code == 200, regenerate.text
    assert all(i["entity_id"] != str(thread_id) for i in regenerate.json()["items"])


def test_awaiting_reply_thread_ages_into_stale_factors(
    email_attention_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, owner_id, token = email_attention_test_context
    now = datetime.now(UTC)
    sender = "known.contact@example.test"
    _resolve_sender(workspace_id, sender)
    thread_id = _seed_thread(
        workspace_id, owner_id, "Aging without a reply", now - timedelta(days=15)
    )
    _seed_message(
        workspace_id,
        owner_id,
        thread_id,
        sender=sender,
        direction="inbound",
        sent_at=now - timedelta(days=15),
    )

    regenerate = client.post("/api/v1/attention/regenerate", headers=_headers(token), json={})
    assert regenerate.status_code == 200, regenerate.text
    item = next(i for i in regenerate.json()["items"] if i["entity_id"] == str(thread_id))
    factor_codes = {f["code"] for f in item["factors"]}
    assert "awaiting_reply" in factor_codes
    assert "stale_14d" in factor_codes
    assert "stale_7d" not in factor_codes  # 14d supersedes 7d, not both at once


def test_awaiting_reply_thread_is_pruned_once_replied(
    email_attention_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    """The `email_thread` branch of `regenerate_attention`'s own stale-row
    `DELETE` -- mirroring `test_attention_waiting_postgres.py`'s own
    `test_waiting_link_surfaces_in_attention_queue_and_ages`.
    """
    client, workspace_id, owner_id, token = email_attention_test_context
    now = datetime.now(UTC)
    sender = "known.contact@example.test"
    _resolve_sender(workspace_id, sender)
    thread_id = _seed_thread(workspace_id, owner_id, "Will get a reply soon", now)
    _seed_message(
        workspace_id, owner_id, thread_id, sender=sender, direction="inbound", sent_at=now
    )

    regenerate = client.post("/api/v1/attention/regenerate", headers=_headers(token), json={})
    assert regenerate.status_code == 200, regenerate.text
    item = next(i for i in regenerate.json()["items"] if i["entity_id"] == str(thread_id))
    assert item["entity_type"] == "email_thread"

    _seed_message(
        workspace_id,
        owner_id,
        thread_id,
        sender=f"{owner_id}@example.test",
        direction="outbound",
        sent_at=now + timedelta(minutes=5),
    )

    regenerate_again = client.post("/api/v1/attention/regenerate", headers=_headers(token), json={})
    assert regenerate_again.status_code == 200, regenerate_again.text
    assert all(i["entity_id"] != str(thread_id) for i in regenerate_again.json()["items"])


def test_dismissed_email_thread_stays_dismissed_across_an_unchanged_regenerate(
    email_attention_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    """`_upsert_batch`'s dismissed-state-preservation logic (shared by
    every entity type) keeps an item dismissed only if `dismissed_
    entity_version` still matches the freshly-recomputed `source_entity_
    version` on the next `regenerate_attention` call -- otherwise it
    silently un-dismisses. `email_thread` rows don't have a real
    optimistic-concurrency `version` column to supply that value (`email_
    threads` has none, per migration `0069`'s own deliberate design,
    unlike `tasks`/`commitments`/`risks`/`waiting_links`), so `attention.
    py` derives one from `email_threads.updated_at` instead -- this is
    the one place a bug in that derivation (e.g. if it were non-
    deterministic, or recomputed differently between calls) would show up
    as a dismissed item spuriously reappearing. No existing test, for any
    entity type, exercises this specific "dismiss, then regenerate again
    with nothing changed" lifecycle -- round 1 review finding.
    """
    client, workspace_id, owner_id, token = email_attention_test_context
    now = datetime.now(UTC)
    sender = "known.contact@example.test"
    _resolve_sender(workspace_id, sender)
    thread_id = _seed_thread(workspace_id, owner_id, "Please dismiss me", now)
    _seed_message(
        workspace_id, owner_id, thread_id, sender=sender, direction="inbound", sent_at=now
    )

    regenerate = client.post("/api/v1/attention/regenerate", headers=_headers(token), json={})
    assert regenerate.status_code == 200, regenerate.text
    item = next(i for i in regenerate.json()["items"] if i["entity_id"] == str(thread_id))

    dismiss = client.post(
        f"/api/v1/attention/{item['id']}/dismiss", headers=_headers(token), json={}
    )
    assert dismiss.status_code == 200, dismiss.text
    assert dismiss.json()["dismissed_at"] is not None

    regenerate_again = client.post("/api/v1/attention/regenerate", headers=_headers(token), json={})
    assert regenerate_again.status_code == 200, regenerate_again.text
    assert all(i["entity_id"] != str(thread_id) for i in regenerate_again.json()["items"])

    with engine.begin() as connection:
        row = (
            connection.execute(
                text(
                    "SELECT dismissed_at, dismissed_entity_version, source_entity_version "
                    "FROM attention_items WHERE workspace_id = :workspace_id "
                    "AND entity_type = 'email_thread' AND entity_id = :entity_id"
                ),
                {"workspace_id": workspace_id, "entity_id": thread_id},
            )
            .mappings()
            .one()
        )
    assert row["dismissed_at"] is not None
    assert row["dismissed_entity_version"] == row["source_entity_version"]


def test_inbound_thread_from_casefold_divergent_sender_surfaces_as_awaiting_reply(
    email_attention_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    """Round 1 review finding: `regenerate_attention`'s own eligibility
    query used to resolve the last inbound message's sender against
    `entity_aliases` via SQL `ea.normalized_value = LOWER(TRIM(lm.sender))`
    -- but Postgres's `LOWER()` and Python's `_normalize_email`
    (`.strip().casefold()`, what actually wrote every `entity_aliases.
    normalized_value` row) are not the same function. `'straße@example.
    test'.strip().casefold()` expands the German sharp s to `'strasse@
    example.test'` (Unicode full case folding's own special-cased
    mapping); Postgres's `LOWER('straße@example.test')` leaves the ß
    unchanged, confirmed directly against this database's own collation
    (`SELECT LOWER('ß')` = `'ß'`, not `'ss'`). A real contact who has
    already been resolved into `pkos_nodes` (their normalized alias is
    `'strasse@example.test'`, exactly as `_resolve_or_create_person`
    would have written it) sending a message from the literal address
    `'straße@example.test'` must still surface as awaiting reply --
    before the fix, the SQL-side `LOWER(TRIM(...))` comparison silently
    failed to match, and this thread never appeared.
    """
    client, workspace_id, owner_id, token = email_attention_test_context
    now = datetime.now(UTC)
    resolved_email = "straße@example.test"
    _resolve_sender(workspace_id, resolved_email)
    thread_id = _seed_thread(workspace_id, owner_id, "Unicode sender", now)
    # Same raw address as resolved above, sent verbatim as the message's own
    # `sender` -- exactly as `gmail_adapter.py` would store the parsed
    # `From` header's email address, unnormalized.
    _seed_message(
        workspace_id, owner_id, thread_id, sender=resolved_email, direction="inbound", sent_at=now
    )

    regenerate = client.post("/api/v1/attention/regenerate", headers=_headers(token), json={})
    assert regenerate.status_code == 200, regenerate.text
    item = next(i for i in regenerate.json()["items"] if i["entity_id"] == str(thread_id))
    assert item["entity_type"] == "email_thread"
    factor_codes = {f["code"] for f in item["factors"]}
    assert "awaiting_reply" in factor_codes
