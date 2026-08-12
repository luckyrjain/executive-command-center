"""Phase 10 Task 5's sync-pipeline wiring (`docs/superpowers/plans/2026-
08-04-phase-10-gmail-connector.md` Task 5: "sync-pipeline wiring produces
a pending (unconfirmed) recommendation, never an immediate write") --
`GmailAdapter.detect_actions_since` (`backend/ecc/domains/personal/
gmail_adapter.py`, a thin delegator) and `_detect_action_for_message`
(`backend/ecc/domains/personal/gmail_action_detection.py`) end to end, with a
mocked Gmail transport (the `messages.get(format=full)` body fetch) and a
mocked `OllamaAdapter` (injected via the `ollama_adapter` parameter added
for exactly this purpose), against a real Postgres database.

Covers:
1. A `has_action: true` result creates exactly one `recommendations` row
   via `create_recommendation` (Task 4's create-path) -- `status=
   'proposed'`, `source='ai'`, `target_id IS NULL`, `proposed_action->>
   'operation'='create'` -- and never writes directly to `tasks`/
   `commitments`/`risks`, the human-confirmation gate this whole
   mechanism exists to preserve.
2. The triggering message's `body`/`body_fetched_at` are populated
   (encrypted at rest, genuinely decryptable back to the fetched plain
   text) and a `pkos_evidence` row citing it
   (`gmail:detect_action:<external_message_id>`) is registered, cited by
   the created recommendation's own `evidence_ids`.
3. A `has_action: false` result still fetches and stores the body (the
   deterministic half of the pipeline always runs) but creates no
   recommendation.
4. `email_action_detection_enabled` defaults to `False` -- with the flag
   left unset, `detect_actions_since` no-ops entirely: no Gmail request,
   no Ollama call, no body fetched, no recommendation created.
5. A message with no extractable `text/plain` part (HTML-only mail) is
   marked handled with an empty-string body sentinel, not left eligible
   forever -- a second call's transport would raise if the message were
   fetched again (round 4 review finding).
6. A candidate left over after one call's own `_MAX_ACTION_DETECTIONS_
   PER_CALL` bound is picked up by a later call, not permanently lost
   (round 3 review finding).
7. `email` domain consent revoked mid-batch halts the remaining batch --
   round 7 review found `detect_actions_since`'s per-row loop never
   repeated the same per-write consent recheck `_sync_messages`/`_sync_
   history` already do, despite `SYNC-CONTRACT.md`'s own general contract
   covering this write path too.
8. A maliciously deep MIME `parts` nesting (`_extract_plain_text_body`'s
   recursive walk raising `RecursionError`) is marked handled with an
   empty-string body sentinel, not left eligible forever -- the identical
   "poison message" failure class item 5 covers, reached through a new
   trigger found by round 9 review.
"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from identity_fixtures import create_identity
from sqlalchemy import text

from ecc.config import get_settings
from ecc.database import engine
from ecc.domains.ai_runtime.ollama_client import OllamaAdapter
from ecc.domains.ai_runtime.runtime import reset_circuit_breakers
from ecc.domains.engineering.connectors import ConnectorAccountContext
from ecc.domains.engineering.crypto import encrypt_credential
from ecc.domains.personal.crypto import decrypt_field
from ecc.domains.personal.gmail_action_detection import _MAX_ACTION_DETECTIONS_PER_CALL
from ecc.domains.personal.gmail_adapter import (
    GmailAdapter,
    _BodyParseOutcome,
    _parse_message_body_response,
)
from ecc.domains.personal.gmail_shared import _pack_credential

settings = get_settings()
pytestmark = pytest.mark.skipif(
    not settings.database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)

_OWNER_EMAIL = "gmail-action-detect-owner@example.test"
_SENDER = "priya@partner-co.test"
_PLAIN_TEXT_BODY = (
    "Hi -- could you please sign and send back the attached contract by end of "
    "day Friday? We can't move forward until we have it on file."
)
_ID_PATTERN = re.compile(r'id="([0-9a-fA-F-]{36})"')


def _seed_base_rows(
    *, workspace_name: str, account_display_name: str, thread_subject: str
) -> tuple[UUID, UUID, UUID, UUID, bytes, datetime]:
    """Shared setup for `detection_context`/`backlog_context`: a workspace,
    identity, active `gmail` connector account, enabled `email` domain with
    consent granted, and one thread. Returns `(workspace_id, owner_id,
    account_id, thread_id, credential, now)` -- callers insert their own
    `email_messages` rows against `thread_id` (`detection_context` seeds
    one; `backlog_context` seeds `_MAX_ACTION_DETECTIONS_PER_CALL + 2`),
    since that is the one part of each fixture's setup that is not shared.
    `workspace_name`/`account_display_name`/`thread_subject` are the three
    values each caller supplies its own distinct string for -- round 5
    review found the original extraction dropped the workspace name as a
    fourth, undisclosed difference (silently collapsing both fixtures onto
    one hardcoded name); no test asserts on it, but a distinct name per
    fixture is restored here for parity with the other two parameters and
    to match this docstring's own claim about what differs between callers.
    """
    workspace_id = uuid4()
    owner_id = uuid4()
    account_id = uuid4()
    thread_id = uuid4()
    now = datetime.now(UTC)
    credential = _pack_credential("access-1", "refresh-1", now + timedelta(hours=1))

    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, :workspace_name, 'UTC', :now)"
            ),
            {"id": workspace_id, "workspace_name": workspace_name, "now": now},
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
                    created_by, updated_by, created_at, updated_at, owner_id, visibility
                ) VALUES (
                    :id, :workspace_id, 'gmail', :external_account_id, :display_name,
                    ARRAY['https://www.googleapis.com/auth/gmail.readonly'], :encrypted,
                    'active', 1, :actor_id, :actor_id, :now, :now, :actor_id, 'workspace'
                )
                """
            ),
            {
                "id": account_id,
                "workspace_id": workspace_id,
                "external_account_id": _OWNER_EMAIL,
                "display_name": account_display_name,
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
        connection.execute(
            text(
                """
                INSERT INTO email_threads (
                    id, workspace_id, owner_id, domain_key, connector_account_id,
                    external_thread_id, subject, last_message_at, created_at, updated_at
                ) VALUES (
                    :id, :workspace_id, :owner_id, 'email', :connector_account_id,
                    :external_thread_id, :subject, :now, :now, :now
                )
                """
            ),
            {
                "id": thread_id,
                "workspace_id": workspace_id,
                "owner_id": owner_id,
                "connector_account_id": account_id,
                "external_thread_id": f"thread-{thread_id}",
                "subject": thread_subject,
                "now": now,
            },
        )

    return workspace_id, owner_id, account_id, thread_id, credential, now


def _teardown_workspace(workspace_id: UUID) -> None:
    with engine.begin() as connection:
        for table in (
            "generated_artifacts",
            "ai_run_steps",
            "ai_runs",
            "recommendations",
            "audit_events",
            "event_outbox",
            "idempotency_records",
            "tasks",
            "commitments",
            "risks",
            # `entity_aliases.source_id` FKs to `pkos_evidence` (`fk_
            # entity_aliases_workspace_source`) -- deleted before it,
            # not after, or the delete would violate that FK.
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


@pytest.fixture(autouse=True)
def _reset_breakers() -> Iterator[None]:
    reset_circuit_breakers()
    yield
    reset_circuit_breakers()


@pytest.fixture
def detection_context() -> Iterator[dict]:
    workspace_id, owner_id, account_id, thread_id, credential, now = _seed_base_rows(
        workspace_name="Gmail Action Detection Test",
        account_display_name="Detection test account",
        thread_subject="Signed contract needed by Friday",
    )
    message_id = uuid4()
    external_message_id = f"detect-{message_id}"

    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO email_messages (
                    id, workspace_id, owner_id, thread_id, external_message_id,
                    sender, recipients, sent_at, direction, snippet, body,
                    body_fetched_at, created_at, updated_at
                ) VALUES (
                    :id, :workspace_id, :owner_id, :thread_id, :external_message_id,
                    :sender, :recipients, :now, 'inbound', NULL, NULL,
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

    try:
        yield {
            "context": ConnectorAccountContext(
                workspace_id=workspace_id,
                connector_account_id=account_id,
                external_account_id=_OWNER_EMAIL,
                credential=credential,
            ),
            "workspace_id": workspace_id,
            "owner_id": owner_id,
            "message_id": message_id,
            "external_message_id": external_message_id,
            "since": now - timedelta(minutes=5),
        }
    finally:
        _teardown_workspace(workspace_id)


def _gmail_transport(external_message_id: str) -> httpx.MockTransport:
    encoded_body = base64.urlsafe_b64encode(_PLAIN_TEXT_BODY.encode()).decode().rstrip("=")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/gmail/v1/users/me/messages/{external_message_id}"
        return httpx.Response(
            200,
            json={
                "id": external_message_id,
                "payload": {"mimeType": "text/plain", "body": {"data": encoded_body}},
            },
        )

    return httpx.MockTransport(handler)


def _extract_cited_id(prompt: str) -> str:
    match = _ID_PATTERN.search(prompt)
    assert match is not None, f"no message id found in rendered prompt: {prompt!r}"
    return match.group(1)


def _ollama_adapter(*, has_action: bool, calls: list[str] | None = None) -> OllamaAdapter:
    def handler(request: httpx.Request) -> httpx.Response:
        prompt = json.loads(request.content)["prompt"]
        if calls is not None:
            calls.append(prompt)
        if has_action:
            payload = {
                "has_action": True,
                "target_type": "task",
                "operation": "create",
                "proposed_fields": {
                    "title": "Sign and return the contract",
                    "description": "Requested by Friday per the sender's email.",
                },
                "rationale": "The sender explicitly asked for a signed contract by Friday.",
                "confidence": 0.85,
                "cited_message_ids": [_extract_cited_id(prompt)],
            }
        else:
            payload = {
                "has_action": False,
                "target_type": None,
                "operation": None,
                "proposed_fields": None,
                "rationale": "This message is a routine update with nothing actionable.",
                "confidence": 0.7,
                "cited_message_ids": [],
            }
        body = (
            json.dumps(
                {
                    "model": "m",
                    "created_at": "now",
                    "response": json.dumps(payload),
                    "done": True,
                    "eval_count": 12,
                    "prompt_eval_count": 40,
                }
            )
            + "\n"
        )
        return httpx.Response(
            200, content=body.encode(), headers={"content-type": "application/x-ndjson"}
        )

    return OllamaAdapter(transport=httpx.MockTransport(handler))


def _recommendations(workspace_id: UUID) -> list[dict]:
    with engine.begin() as connection:
        rows = (
            connection.execute(
                text(
                    "SELECT status, source, target_type, target_id, proposed_action, "
                    "proposed_fields, evidence_ids FROM recommendations "
                    "WHERE workspace_id = :workspace_id"
                ),
                {"workspace_id": workspace_id},
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


def _message_body(workspace_id: UUID, message_id: UUID) -> tuple[bytes | None, datetime | None]:
    with engine.begin() as connection:
        row = connection.execute(
            text(
                "SELECT body, body_fetched_at FROM email_messages "
                "WHERE workspace_id = :workspace_id AND id = :id"
            ),
            {"workspace_id": workspace_id, "id": message_id},
        ).one()
    return row[0], row[1]


def _evidence_source_refs(workspace_id: UUID) -> list[str]:
    with engine.begin() as connection:
        rows = connection.execute(
            text(
                "SELECT source_ref FROM pkos_evidence WHERE workspace_id = :workspace_id "
                "AND source_ref LIKE 'gmail:detect_action:%'"
            ),
            {"workspace_id": workspace_id},
        ).all()
    return [row[0] for row in rows]


def _table_count(workspace_id: UUID, table: str) -> int:
    with engine.begin() as connection:
        return connection.execute(
            text(f"SELECT count(*) FROM {table} WHERE workspace_id = :workspace_id"),  # noqa: S608
            {"workspace_id": workspace_id},
        ).scalar_one()


def test_has_action_true_creates_exactly_one_pending_recommendation_never_a_direct_write(
    detection_context: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ECC_EMAIL_ACTION_DETECTION_ENABLED", "true")
    get_settings.cache_clear()
    try:
        workspace_id = detection_context["workspace_id"]
        adapter = GmailAdapter(transport=_gmail_transport(detection_context["external_message_id"]))
        ollama_adapter = _ollama_adapter(has_action=True)

        adapter.detect_actions_since(
            detection_context["context"],
            since=detection_context["since"],
            ollama_adapter=ollama_adapter,
        )

        recommendations = _recommendations(workspace_id)
        assert len(recommendations) == 1
        recommendation = recommendations[0]
        assert recommendation["status"] == "proposed"
        assert recommendation["source"] == "ai"
        assert recommendation["target_type"] == "task"
        assert recommendation["target_id"] is None
        assert recommendation["proposed_action"]["operation"] == "create"
        assert recommendation["proposed_fields"]["title"] == "Sign and return the contract"

        # Never a direct, unconfirmed write to the target domain tables --
        # the human-confirmation gate `create_recommendation` exists to
        # preserve stays intact for this, the first non-HTTP caller of it.
        assert _table_count(workspace_id, "tasks") == 0
        assert _table_count(workspace_id, "commitments") == 0
        assert _table_count(workspace_id, "risks") == 0

        body, fetched_at = _message_body(workspace_id, detection_context["message_id"])
        assert body is not None
        assert fetched_at is not None
        assert decrypt_field(body) == _PLAIN_TEXT_BODY

        evidence_refs = _evidence_source_refs(workspace_id)
        assert evidence_refs == [f"gmail:detect_action:{detection_context['external_message_id']}"]
    finally:
        get_settings.cache_clear()


def test_has_action_false_still_fetches_body_but_creates_no_recommendation(
    detection_context: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ECC_EMAIL_ACTION_DETECTION_ENABLED", "true")
    get_settings.cache_clear()
    try:
        workspace_id = detection_context["workspace_id"]
        adapter = GmailAdapter(transport=_gmail_transport(detection_context["external_message_id"]))
        ollama_adapter = _ollama_adapter(has_action=False)

        adapter.detect_actions_since(
            detection_context["context"],
            since=detection_context["since"],
            ollama_adapter=ollama_adapter,
        )

        assert _recommendations(workspace_id) == []
        body, fetched_at = _message_body(workspace_id, detection_context["message_id"])
        assert body is not None
        assert fetched_at is not None
        assert decrypt_field(body) == _PLAIN_TEXT_BODY
    finally:
        get_settings.cache_clear()


def test_feature_flag_disabled_by_default_no_ops_entirely(detection_context: dict) -> None:
    """No `monkeypatch.setenv` here -- `email_action_detection_enabled`
    defaults to `False` (`config.py`), exercising this test file's own
    real default rather than a value another test happened to set.
    """
    workspace_id = detection_context["workspace_id"]

    def _unexpected_gmail_call(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected Gmail request to {request.url}: feature flag is off")

    def _unexpected_ollama_call(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected Ollama request to {request.url}: feature flag is off")

    adapter = GmailAdapter(transport=httpx.MockTransport(_unexpected_gmail_call))
    ollama_adapter = OllamaAdapter(transport=httpx.MockTransport(_unexpected_ollama_call))

    adapter.detect_actions_since(
        detection_context["context"],
        since=detection_context["since"],
        ollama_adapter=ollama_adapter,
    )

    assert _recommendations(workspace_id) == []
    body, fetched_at = _message_body(workspace_id, detection_context["message_id"])
    assert body is None
    assert fetched_at is None


def _gmail_transport_html_only(external_message_id: str) -> httpx.MockTransport:
    """A message whose Gmail `payload` has no `text/plain` part anywhere in
    its MIME tree -- `_extract_plain_text_body` returns `None` for this,
    the same as an HTML-only email.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/gmail/v1/users/me/messages/{external_message_id}"
        return httpx.Response(
            200,
            json={
                "id": external_message_id,
                "payload": {
                    "mimeType": "text/html",
                    "body": {"data": base64.urlsafe_b64encode(b"<p>hi</p>").decode().rstrip("=")},
                },
            },
        )

    return httpx.MockTransport(handler)


def _build_deeply_nested_mime_payload_json(depth: int) -> bytes:
    """The raw JSON *text* (not a Python `dict`) for a `multipart/mixed`
    `payload` wrapping a single child part, `depth` levels deep -- built by
    string concatenation, deliberately never as a nested Python `dict`
    passed through `json.dumps`/`httpx.Response(json=...)`, since building
    (and later re-encoding) a `depth`-deep nested `dict` the ordinary way
    is itself just as recursive as decoding one, and would raise
    `RecursionError` here, in the test's own mock-server setup, before the
    code under test ever runs. `depth` deep enough to exceed the
    interpreter's default recursion limit (1000) for a per-object-level
    recursive walk (`_extract_plain_text_body`'s own), and, independently,
    Python's own pure-Python JSON decoder fallback if the C accelerator
    isn't in play -- whichever of the two `RecursionError`s this
    environment hits first, both are guarded in `_detect_action_for_
    message` and this test doesn't care which one actually fires. The
    innermost part is `text/plain` with real content, precisely so a
    successful (non-crashing) walk *would* find it -- proving the failure
    is a crash, not merely "found nothing."
    """
    inner_data = base64.urlsafe_b64encode(b"unreachable").decode().rstrip("=")
    innermost = f'{{"mimeType":"text/plain","body":{{"data":"{inner_data}"}}}}'
    payload_json = '{"mimeType":"multipart/mixed","parts":[' * depth + innermost + "]}" * depth
    return payload_json.encode("utf-8")


def _gmail_transport_deeply_nested_mime(external_message_id: str) -> httpx.MockTransport:
    """A message whose `payload.parts` MIME tree is nested far deeper than
    `_extract_plain_text_body`'s recursive walk (or, depending on this
    environment's JSON decoder, `get_response.json()` itself) can traverse
    without raising `RecursionError` -- round 9 review: a sender fully
    controls their outgoing MIME structure the same way they control their
    headers, so a crafted, deeply-nested `multipart/mixed` message is a
    realistic attacker input, not merely a synthetic edge case.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/gmail/v1/users/me/messages/{external_message_id}"
        body = b'{"id":"%s","payload":%s}' % (
            external_message_id.encode("ascii"),
            _build_deeply_nested_mime_payload_json(5000),
        )
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})

    return httpx.MockTransport(handler)


# --- `_parse_message_body_response`: pure parsing, no HTTP/DB needed ---------
#
# Code review finding on the `fetch_and_store_body` refactor: "retriable"
# (stays `body IS NULL`, eligible for a future call) and "resolved, possibly
# empty" (permanent, write it) used to both be spelled `None` -- a bare
# `return None` for the former, a local `plain_text = None` that still fell
# through to a write for the latter -- exactly the collision rounds 4 and 9
# review each found once, in two different exception branches. These tests
# exercise the extracted `_parse_message_body_response` directly, with no
# Postgres/live-Gmail-transport machinery, covering every branch the two
# integration tests below (`test_no_extractable_plain_text_is_marked_
# handled_not_retried_forever`/`test_recursion_error_extracting_body_is_
# marked_handled_not_retried_forever`) already prove end-to-end.


def test_parse_message_body_response_is_retriable_when_response_is_none() -> None:
    assert _parse_message_body_response(None) == _BodyParseOutcome(retriable=True)


def test_parse_message_body_response_is_retriable_on_non_200_status() -> None:
    response = httpx.Response(429, json={"error": "rate limited"})
    assert _parse_message_body_response(response) == _BodyParseOutcome(retriable=True)


def test_parse_message_body_response_is_retriable_on_malformed_json() -> None:
    response = httpx.Response(200, content=b"not json")
    assert _parse_message_body_response(response) == _BodyParseOutcome(retriable=True)


def test_parse_message_body_response_is_retriable_on_non_object_response_body() -> None:
    response = httpx.Response(200, json=["not", "an", "object"])
    assert _parse_message_body_response(response) == _BodyParseOutcome(retriable=True)


def test_parse_message_body_response_is_retriable_on_missing_payload() -> None:
    response = httpx.Response(200, json={"id": "msg-1"})
    assert _parse_message_body_response(response) == _BodyParseOutcome(retriable=True)


def test_parse_message_body_response_is_retriable_on_non_object_payload() -> None:
    response = httpx.Response(200, json={"id": "msg-1", "payload": "not an object"})
    assert _parse_message_body_response(response) == _BodyParseOutcome(retriable=True)


def test_parse_message_body_response_resolves_extracted_plain_text() -> None:
    data = base64.urlsafe_b64encode(b"hello world").decode().rstrip("=")
    response = httpx.Response(
        200,
        json={
            "id": "msg-1",
            "payload": {"mimeType": "text/plain", "body": {"data": data}},
        },
    )
    assert _parse_message_body_response(response) == _BodyParseOutcome(
        retriable=False, text="hello world"
    )


def test_parse_message_body_response_resolves_empty_when_no_plain_text_part() -> None:
    """Round 4 review's own finding: a structurally-parsed `payload` with
    no `text/plain` part reachable is a permanent fact about the message,
    not a reason to retry.
    """
    response = httpx.Response(
        200,
        json={
            "id": "msg-1",
            "payload": {"mimeType": "text/html", "body": {"data": "not-plain-text"}},
        },
    )
    assert _parse_message_body_response(response) == _BodyParseOutcome(retriable=False, text="")


def test_parse_message_body_response_resolves_empty_on_deeply_nested_mime() -> None:
    """Round 9 review's own finding -- see `_build_deeply_nested_mime_
    payload_json`'s own docstring for why this doesn't pin down which of
    the two internal `RecursionError` sites actually fires; both resolve
    to the identical outcome.
    """
    body = b'{"id":"msg-1","payload":%s}' % _build_deeply_nested_mime_payload_json(5000)
    response = httpx.Response(200, content=body, headers={"content-type": "application/json"})
    assert _parse_message_body_response(response) == _BodyParseOutcome(retriable=False, text="")


def test_recursion_error_extracting_body_is_marked_handled_not_retried_forever(
    detection_context: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round 9 review finding: `_extract_plain_text_body`'s recursive
    `parts` walk has no depth cap, so a maliciously deep MIME nesting
    raises an uncaught `RecursionError` -- which, before this fix, was not
    caught anywhere between that walk and `_detect_action_for_message`'s
    outer `except Exception: continue` in `detect_actions_since`'s batch
    loop, so the body-UPDATE that marks a message handled never ran,
    reopening the exact "poison message, permanent retry" failure class
    round 4 already fixed once (see `test_no_extractable_plain_text_is_
    marked_handled_not_retried_forever`, immediately above) via a new
    trigger. Proven with the same two-call shape that test uses: a first
    call that fetches the deeply-nested message, then a second call whose
    Gmail transport raises on any request -- if the message were still
    eligible (`body IS NULL`), this second call would hit it again.
    """
    monkeypatch.setenv("ECC_EMAIL_ACTION_DETECTION_ENABLED", "true")
    get_settings.cache_clear()
    try:
        workspace_id = detection_context["workspace_id"]
        message_id = detection_context["message_id"]
        external_message_id = detection_context["external_message_id"]

        first_adapter = GmailAdapter(
            transport=_gmail_transport_deeply_nested_mime(external_message_id)
        )
        first_adapter.detect_actions_since(
            detection_context["context"],
            since=detection_context["since"],
            ollama_adapter=_ollama_adapter(has_action=False),
        )

        assert _recommendations(workspace_id) == []
        body, fetched_at = _message_body(workspace_id, message_id)
        assert body is not None
        assert fetched_at is not None
        assert decrypt_field(body) == ""

        def _unexpected_gmail_call(request: httpx.Request) -> httpx.Response:
            raise AssertionError(
                f"unexpected Gmail request to {request.url}: message should no longer be "
                "eligible (body is no longer NULL)"
            )

        second_adapter = GmailAdapter(transport=httpx.MockTransport(_unexpected_gmail_call))
        second_adapter.detect_actions_since(
            detection_context["context"],
            since=datetime.now(UTC),
            ollama_adapter=_ollama_adapter(has_action=False),
        )

        assert _recommendations(workspace_id) == []
    finally:
        get_settings.cache_clear()


def test_no_extractable_plain_text_is_marked_handled_not_retried_forever(
    detection_context: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round 4 review finding: a message whose Gmail response parses fully
    but has no `text/plain` part used to leave `body IS NULL`, so
    `detect_actions_since`'s own `WHERE ... AND body IS NULL` eligibility
    query re-selected, re-fetched, and re-rejected it on every future call
    forever. Fixed by writing an empty-string body sentinel
    (`_detect_action_for_message`) once the Gmail response has been fully,
    successfully parsed and genuinely has nothing extractable -- proven
    here by a first call that fetches the message, then a second call
    whose Gmail transport raises on any request: if the message were still
    eligible, this second call would hit it again.
    """
    monkeypatch.setenv("ECC_EMAIL_ACTION_DETECTION_ENABLED", "true")
    get_settings.cache_clear()
    try:
        workspace_id = detection_context["workspace_id"]
        message_id = detection_context["message_id"]
        external_message_id = detection_context["external_message_id"]

        first_adapter = GmailAdapter(transport=_gmail_transport_html_only(external_message_id))
        first_adapter.detect_actions_since(
            detection_context["context"],
            since=detection_context["since"],
            ollama_adapter=_ollama_adapter(has_action=False),
        )

        assert _recommendations(workspace_id) == []
        body, fetched_at = _message_body(workspace_id, message_id)
        assert body is not None
        assert fetched_at is not None
        assert decrypt_field(body) == ""

        def _unexpected_gmail_call(request: httpx.Request) -> httpx.Response:
            raise AssertionError(
                f"unexpected Gmail request to {request.url}: message should no longer be "
                "eligible (body is no longer NULL)"
            )

        second_adapter = GmailAdapter(transport=httpx.MockTransport(_unexpected_gmail_call))
        second_adapter.detect_actions_since(
            detection_context["context"],
            since=datetime.now(UTC),
            ollama_adapter=_ollama_adapter(has_action=False),
        )

        assert _recommendations(workspace_id) == []
    finally:
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Loop 2 round 3 review finding: a candidate left over after one call's own
# `_MAX_ACTION_DETECTIONS_PER_CALL` bound must be picked up by a later call,
# not permanently lost. The original `created_at >= since` filter design
# (round 1) would have silently and permanently excluded it, since every
# later call's own `since` only ever advances -- found only by actually
# exercising two sequential calls against more candidates than one call's
# own bound allows, which no test before this one did.
# ---------------------------------------------------------------------------


@pytest.fixture
def backlog_context() -> Iterator[dict]:
    """`_MAX_ACTION_DETECTIONS_PER_CALL + 2` eligible messages in one
    thread -- deliberately more than a single `detect_actions_since` call
    will ever process, so a test can prove the overflow survives into a
    second call rather than being lost.
    """
    workspace_id, owner_id, account_id, thread_id, credential, now = _seed_base_rows(
        workspace_name="Gmail Action Detection Backlog Test",
        account_display_name="Backlog test account",
        thread_subject="Backlog thread",
    )
    message_count = _MAX_ACTION_DETECTIONS_PER_CALL + 2
    message_ids = [uuid4() for _ in range(message_count)]
    external_message_ids = [f"backlog-{message_id}" for message_id in message_ids]

    with engine.begin() as connection:
        for index, (message_id, external_message_id) in enumerate(
            zip(message_ids, external_message_ids, strict=True)
        ):
            connection.execute(
                text(
                    """
                    INSERT INTO email_messages (
                        id, workspace_id, owner_id, thread_id, external_message_id,
                        sender, recipients, sent_at, direction, snippet, body,
                        body_fetched_at, created_at, updated_at
                    ) VALUES (
                        :id, :workspace_id, :owner_id, :thread_id, :external_message_id,
                        :sender, :recipients, :sent_at, 'inbound', NULL, NULL,
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
                    # Distinct `sent_at` per message so `ORDER BY m.sent_at
                    # ASC` (the tiebreak after this query's own priority
                    # ordering) is deterministic across the whole set.
                    "sent_at": now + timedelta(seconds=index),
                    "now": now,
                },
            )

    try:
        yield {
            "context": ConnectorAccountContext(
                workspace_id=workspace_id,
                connector_account_id=account_id,
                external_account_id=_OWNER_EMAIL,
                credential=credential,
            ),
            "workspace_id": workspace_id,
            "owner_id": owner_id,
            "message_ids": message_ids,
            "external_message_ids": external_message_ids,
            "since": now - timedelta(minutes=5),
        }
    finally:
        _teardown_workspace(workspace_id)


def _gmail_transport_for_any_of(external_message_ids: list[str]) -> httpx.MockTransport:
    encoded_body = base64.urlsafe_b64encode(_PLAIN_TEXT_BODY.encode()).decode().rstrip("=")
    valid_ids = set(external_message_ids)

    def handler(request: httpx.Request) -> httpx.Response:
        message_id = request.url.path.rsplit("/", 1)[-1]
        assert message_id in valid_ids, f"unexpected message id {message_id!r}"
        return httpx.Response(
            200,
            json={
                "id": message_id,
                "payload": {"mimeType": "text/plain", "body": {"data": encoded_body}},
            },
        )

    return httpx.MockTransport(handler)


def _fetched_message_ids(workspace_id: UUID, message_ids: list[UUID]) -> set[UUID]:
    with engine.begin() as connection:
        rows = connection.execute(
            text(
                "SELECT id FROM email_messages WHERE workspace_id = :workspace_id "
                "AND id = ANY(:message_ids) AND body IS NOT NULL"
            ),
            {"workspace_id": workspace_id, "message_ids": message_ids},
        ).all()
    return {row[0] for row in rows}


def test_overflow_beyond_one_calls_own_bound_is_picked_up_by_the_next_call_not_lost(
    backlog_context: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ECC_EMAIL_ACTION_DETECTION_ENABLED", "true")
    get_settings.cache_clear()
    try:
        workspace_id = backlog_context["workspace_id"]
        message_ids = backlog_context["message_ids"]
        external_message_ids = backlog_context["external_message_ids"]
        assert len(message_ids) == _MAX_ACTION_DETECTIONS_PER_CALL + 2

        adapter = GmailAdapter(transport=_gmail_transport_for_any_of(external_message_ids))

        adapter.detect_actions_since(
            backlog_context["context"],
            since=backlog_context["since"],
            ollama_adapter=_ollama_adapter(has_action=False),
        )
        fetched_after_first_call = _fetched_message_ids(workspace_id, message_ids)
        assert len(fetched_after_first_call) == _MAX_ACTION_DETECTIONS_PER_CALL, (
            "first call must be bounded by _MAX_ACTION_DETECTIONS_PER_CALL, "
            f"got {len(fetched_after_first_call)}"
        )

        # A second call, with a genuinely later `since` -- the same
        # advancing watermark a real second `/sync` call would pass.
        adapter.detect_actions_since(
            backlog_context["context"],
            since=datetime.now(UTC),
            ollama_adapter=_ollama_adapter(has_action=False),
        )
        fetched_after_second_call = _fetched_message_ids(workspace_id, message_ids)
        assert fetched_after_second_call == set(message_ids), (
            "every message must eventually be fetched across calls, not permanently "
            f"excluded once its own created_at falls behind a later call's since; "
            f"still unfetched: {set(message_ids) - fetched_after_second_call}"
        )
    finally:
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Round 7 review finding: `detect_actions_since`'s per-row loop never
# repeated the same per-write consent recheck `_sync_messages`/`_sync_
# history` already do, despite `SYNC-CONTRACT.md`'s own general contract
# ("An active `email` domain consent is checked before external fetch and
# again before each message write. Revocation during a call stops further
# writes.") covering this write path too -- found only by actually revoking
# consent mid-batch and asserting later messages are never fetched, which no
# test before this one did.
# ---------------------------------------------------------------------------


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


def test_batch_halts_when_consent_is_revoked_mid_batch(
    backlog_context: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`SYNC-CONTRACT.md`'s own general contract -- consent checked before
    external fetch and again before each message write, revocation during
    a call stops further writes -- applies to this write path exactly like
    it already does to `_sync_messages`/`_sync_history`. Revokes consent as
    a side effect of fetching the *first* message's body; a second message
    must never be fetched afterward.
    """
    monkeypatch.setenv("ECC_EMAIL_ACTION_DETECTION_ENABLED", "true")
    get_settings.cache_clear()
    try:
        workspace_id = backlog_context["workspace_id"]
        owner_id = backlog_context["owner_id"]
        message_ids = backlog_context["message_ids"]
        external_message_ids = backlog_context["external_message_ids"]

        fetched_external_ids: list[str] = []
        encoded_body = base64.urlsafe_b64encode(_PLAIN_TEXT_BODY.encode()).decode().rstrip("=")

        def handler(request: httpx.Request) -> httpx.Response:
            message_id = request.url.path.rsplit("/", 1)[-1]
            assert message_id in external_message_ids, f"unexpected message id {message_id!r}"
            fetched_external_ids.append(message_id)
            if len(fetched_external_ids) == 1:
                _revoke_consent(workspace_id, owner_id)
            return httpx.Response(
                200,
                json={
                    "id": message_id,
                    "payload": {"mimeType": "text/plain", "body": {"data": encoded_body}},
                },
            )

        adapter = GmailAdapter(transport=httpx.MockTransport(handler))

        adapter.detect_actions_since(
            backlog_context["context"],
            since=backlog_context["since"],
            ollama_adapter=_ollama_adapter(has_action=False),
        )

        assert len(fetched_external_ids) == 1, (
            "only the message that triggered the mid-batch revocation may be fetched; "
            f"got {len(fetched_external_ids)} fetches: {fetched_external_ids}"
        )
        fetched = _fetched_message_ids(workspace_id, message_ids)
        assert len(fetched) == 1, (
            f"only one message's body may be stored after consent is revoked, got {len(fetched)}"
        )
    finally:
        get_settings.cache_clear()
