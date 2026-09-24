"""Real-sync Gmail test fixture (Security Remediation Spec A, T13).

Drives the *production* Gmail pipeline end to end for one or more members
of one workspace -- no hand-inserted Gmail-derived rows -- so later tests
(member removal, private-by-data isolation, the visibility backfill,
evidence readers) can assert against exactly the rows production writes:

1. `POST /api/v1/personal/domains` -- enables the `email` domain with
   consent (`personal_domains` + `domain_consents`).
2. `POST /api/v1/personal/gmail/oauth/start` then
   `GET /api/v1/personal/gmail/oauth/callback` -- the real OAuth callback
   writes the `gmail` `connector_accounts` row (`gmail_oauth._adapter` --
   and the `gmail_revocation`/`gmail_threads` module adapters -- patched to
   a `GmailAdapter` over an in-process fake Google, the same
   `monkeypatch.setattr(gmail_oauth_module, "_adapter", ...)` pattern
   `tests/test_gmail_connector_postgres.py` uses).
3. `POST /api/v1/engineering/connectors/{id}/sync` (`run_type=backfill`,
   `resource_type=message`) -- `_run_connector_sync` writes `sync_runs`/
   `sync_cursors`, `GmailAdapter.backfill` writes `email_threads`/
   `email_messages` and resolves participants (`pkos_nodes` person +
   `entity_aliases` email + `pkos_evidence` `gmail_sync`), then the
   post-sync `detect_actions_since` hook (with
   `ECC_EMAIL_ACTION_DETECTION_ENABLED=true` and `ai_runtime.runtime.
   OllamaAdapter` patched to a mocked-transport adapter, the same fake
   `tests/test_gmail_action_detection_sync_postgres.py` injects) fetches
   bodies, registers `gmail:detect_action:*` evidence, records an
   `email.detect_action` `ai_runs` row and creates an
   `email_action_detected` recommendation.
   `connector_accounts_module.connector_registry` is swapped for a
   registry holding the same fake-transport `GmailAdapter` (the pattern
   `tests/test_engineering_github_sync_postgres.py` uses).
4. `POST /api/v1/attention/regenerate` -- writes `email_thread`
   `attention_items` for threads awaiting a reply.

Every artifact above is produced through the production path; nothing is
inserted directly except the identities/sessions the HTTP calls
authenticate as (`identity_fixtures.create_identity` + a `sessions` row,
the same seeding every `*_postgres.py` HTTP test uses).

Ownership as production writes it today (flag-off baseline; Spec A T14a/
T14b change some of these behind `ECC_PERSONAL_DATA_ISOLATION`):

- `connector_accounts`, `sync_runs`, `sync_cursors`, `email_threads`,
  `email_messages`, `attention_items`, `recommendations`, `ai_runs`:
  `owner_id` = the mailbox owner (the member whose sync produced them).
- `pkos_nodes` (person): `owner_id` = the member whose sync *first*
  resolved that address -- a correspondent shared by both mailboxes is one
  node, owned by whichever member synced first.
- `pkos_evidence` and `entity_aliases`: neither INSERT sets `owner_id`, so
  the `authz_default_owner_from_workspace_original_user` trigger assigns
  the workspace's earliest `users` row -- member A in `gmail_sync_world`,
  for *both* mailboxes' rows. `MemberGmailArtifacts` therefore attributes
  evidence/aliases to a member by *content* (`source_ref` message id +
  node address, recommendation `evidence_ids`), never by `owner_id`.

`build_gmail_sync_world(env=...)` / the `gmail_sync_world` fixture (env
via indirect parametrization) / `gmail_sync_world_factory` keep the
harness -- fake Google (which records `/revoke` calls), patched adapter and
registry, env overrides -- active for the world's whole lifetime and expose
per-member authenticated clients, so follow-up disconnect/removal/purge
calls never reach the real Google endpoints.

`gmail_sync_world` builds the two-member world later tasks need: member A
(`owner`, the workspace's original user) and member B (`member`), each
with their own Google mailbox (distinct emails), one shared correspondent
(one person node), and one `external_message_id` present in both
mailboxes (the cross-owner collision `gmail_revocation.py`'s ambiguity
check and migration `0076` model).
"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from hmac import new
from typing import Any
from urllib.parse import parse_qs
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from identity_fixtures import create_identity
from sqlalchemy import text

import ecc.domains.ai_runtime.runtime as runtime_module
import ecc.domains.engineering.connector_accounts as connector_accounts_module
import ecc.domains.personal.gmail_oauth as gmail_oauth_module
import ecc.domains.personal.gmail_revocation as gmail_revocation_module
import ecc.domains.personal.gmail_threads as gmail_threads_module
from ecc.config import get_settings
from ecc.database import engine
from ecc.domains.ai_runtime.ollama_client import OllamaAdapter
from ecc.domains.ai_runtime.runtime import reset_circuit_breakers
from ecc.domains.engineering.connectors import ConnectorRegistry
from ecc.domains.engineering.connectors import registry as production_registry
from ecc.domains.personal.gmail_adapter import REQUIRED_SCOPES, GmailAdapter
from ecc.main import app

_SCOPE_STRING = " ".join(sorted(REQUIRED_SCOPES))
_ID_PATTERN = re.compile(r'id="([0-9a-fA-F-]{36})"')
_GMAIL_MESSAGES_PREFIX = "/gmail/v1/users/me/messages/"
_GMAIL_ADAPTER_MODULES = (gmail_oauth_module, gmail_revocation_module, gmail_threads_module)

# Tables holding rows this fixture's production calls write, in FK-safe
# delete order (children first). `entity_aliases.source_id` FKs to
# `pkos_evidence`, so aliases go first; `users` is last among the
# workspace-scoped tables because most of the others FK to it without
# `ON DELETE CASCADE`.
_CLEANUP_TABLES: tuple[str, ...] = (
    "generated_artifacts",
    "ai_run_steps",
    "ai_runs",
    "recommendations",
    "attention_items",
    "audit_events",
    "event_outbox",
    "idempotency_records",
    "tasks",
    "commitments",
    "risks",
    "entity_aliases",
    "pkos_evidence",
    "pkos_nodes",
    "email_message_id_purge_log",
    "email_messages",
    "email_threads",
    "sync_runs",
    "sync_cursors",
    "domain_consents",
    "personal_domains",
    "connector_accounts",
    "sessions",
    "workspace_memberships",
    "users",
)


# --- fake Google (OAuth token endpoint + Gmail API) --------------------------


@dataclass(frozen=True)
class FakeGmailMessage:
    """One message in a fake mailbox. `sender_email` is the `From` address;
    the mailbox owner's own address is always the sole `To` recipient.
    Inbound (no `SENT` label) so it is eligible for action detection and
    the awaiting-reply attention factor."""

    external_message_id: str
    external_thread_id: str
    sender_email: str
    sender_name: str
    subject: str
    body_text: str
    sent_at: datetime


@dataclass(frozen=True)
class FakeMailbox:
    google_email: str
    messages: tuple[FakeGmailMessage, ...]

    @property
    def participant_emails(self) -> frozenset[str]:
        return frozenset(
            {self.google_email.casefold()} | {m.sender_email.casefold() for m in self.messages}
        )


def _json(body: Any, *, status_code: int = 200) -> httpx.Response:
    return httpx.Response(status_code=status_code, json=body)


def _b64url(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")


class FakeGoogle:
    """In-process fake of Google's OAuth token endpoint and the Gmail API
    surface `GmailAdapter` calls. Each registered mailbox gets its own
    authorization code / access token / refresh token, and every Gmail API
    request is routed to a mailbox by its `Authorization: Bearer` token --
    so one shared `GmailAdapter` serves several members' connectors, each
    seeing only its own mail (including a message id both mailboxes share).
    """

    def __init__(self) -> None:
        self._by_code: dict[str, FakeMailbox] = {}
        self._by_access_token: dict[str, FakeMailbox] = {}
        self._by_refresh_token: dict[str, FakeMailbox] = {}
        self.requests: list[httpx.Request] = []
        # Every token POSTed to `/revoke` (Google's revocation endpoint),
        # in call order -- a revoke during or after the fixture's lifetime
        # lands here instead of on the network.
        self.revoked_tokens: list[str] = []

    @staticmethod
    def access_token(key: str) -> str:
        return f"access-{key}"

    @staticmethod
    def refresh_token(key: str) -> str:
        return f"refresh-{key}"

    def register(self, key: str, mailbox: FakeMailbox) -> str:
        """Registers `mailbox`; returns the OAuth authorization `code`
        whose exchange yields that mailbox's tokens."""
        code = f"code-{key}"
        self._by_code[code] = mailbox
        self._by_access_token[f"access-{key}"] = mailbox
        self._by_refresh_token[f"refresh-{key}"] = mailbox
        return code

    def _token_for(self, mailbox: FakeMailbox) -> str:
        return next(t for t, m in self._by_access_token.items() if m is mailbox)

    def _refresh_for(self, mailbox: FakeMailbox) -> str:
        return next(t for t, m in self._by_refresh_token.items() if m is mailbox)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/token":
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            if form.get("grant_type") == "refresh_token":
                mailbox = self._by_refresh_token.get(form.get("refresh_token", ""))
            else:
                mailbox = self._by_code.get(form.get("code", ""))
            if mailbox is None:
                return _json({"error": "invalid_grant"}, status_code=400)
            return _json(
                {
                    "access_token": self._token_for(mailbox),
                    "refresh_token": self._refresh_for(mailbox),
                    "expires_in": 3600,
                    "scope": _SCOPE_STRING,
                }
            )
        if path == "/revoke":
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            self.revoked_tokens.append(form.get("token", ""))
            return httpx.Response(200)

        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        mailbox = self._by_access_token.get(token)
        if mailbox is None:
            return _json({"error": "unauthenticated"}, status_code=401)
        history_id = 1000 + len(mailbox.messages)
        if path == "/gmail/v1/users/me/profile":
            return _json({"emailAddress": mailbox.google_email, "historyId": str(history_id)})
        if path == "/gmail/v1/users/me/messages":
            return _json({"messages": [{"id": m.external_message_id} for m in mailbox.messages]})
        if path == "/gmail/v1/users/me/history":
            return _json({"history": [], "historyId": str(history_id)})
        if path.startswith(_GMAIL_MESSAGES_PREFIX):
            message_id = path.removeprefix(_GMAIL_MESSAGES_PREFIX)
            index, message = next(
                (i, m)
                for i, m in enumerate(mailbox.messages)
                if m.external_message_id == message_id
            )
            if request.url.params.get("format") == "full":
                return _json(
                    {
                        "id": message.external_message_id,
                        "payload": {
                            "mimeType": "text/plain",
                            "body": {"data": _b64url(message.body_text)},
                        },
                    }
                )
            return _json(
                {
                    "id": message.external_message_id,
                    "threadId": message.external_thread_id,
                    "labelIds": ["INBOX"],
                    "internalDate": str(int(message.sent_at.timestamp() * 1000)),
                    "historyId": 1001 + index,
                    "payload": {
                        "headers": [
                            {
                                "name": "From",
                                "value": f"{message.sender_name} <{message.sender_email}>",
                            },
                            {"name": "To", "value": mailbox.google_email},
                            {"name": "Subject", "value": message.subject},
                        ]
                    },
                }
            )
        raise AssertionError(f"unexpected fake-Google request to {request.url}")


def _fake_ollama_adapter() -> OllamaAdapter:
    """Always answers `has_action: true`, citing the first message id in
    the rendered prompt -- same response shape as
    `tests/test_gmail_action_detection_sync_postgres.py::_ollama_adapter`."""

    def handler(request: httpx.Request) -> httpx.Response:
        prompt = json.loads(request.content)["prompt"]
        match = _ID_PATTERN.search(prompt)
        assert match is not None, "no message id in the rendered detection prompt"
        payload = {
            "has_action": True,
            "target_type": "task",
            "operation": "create",
            "proposed_fields": {
                "title": "Reply to the request",
                "description": "Requested in the synced email.",
            },
            "rationale": "The sender explicitly asked for a follow-up.",
            "confidence": 0.85,
            "cited_message_ids": [match.group(1)],
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


# --- collected artifacts ------------------------------------------------------


@dataclass(frozen=True)
class MemberGmailArtifacts:
    """Ids of every row one member's real Gmail sync produced, attributed
    by content (connector/thread/message linkage, `source_ref`, recommendation
    `evidence_ids`, `ai_runs.input_ref`), not by `owner_id` -- so tests can
    assert ownership/visibility of each row independently."""

    user_id: UUID
    account_id: UUID
    google_email: str
    connector_account_id: UUID
    sync_run_ids: tuple[UUID, ...]
    sync_cursor_ids: tuple[UUID, ...]
    email_thread_ids: tuple[UUID, ...]
    # external_message_id -> email_messages.id
    email_message_ids: Mapping[str, UUID]
    # normalized participant email -> pkos_nodes.id (person); includes a
    # shared correspondent's node, which appears under both members.
    person_node_ids: Mapping[str, UUID]
    # normalized participant email -> entity_aliases.id (alias_type email)
    entity_alias_ids: Mapping[str, UUID]
    # `gmail:<external_message_id>` rows written by participant resolution
    resolution_evidence_ids: tuple[UUID, ...]
    # `gmail:detect_action:<external_message_id>` rows written by detection
    detection_evidence_ids: tuple[UUID, ...]
    # `pkos_evidence.source_ref` -> this member's evidence ids with that
    # ref (resolution + detection). A colliding external id's refs map to
    # only *this* member's rows here, though the other member has rows
    # with the identical `source_ref`.
    evidence_ids_by_source_ref: Mapping[str, tuple[UUID, ...]]
    attention_item_ids: tuple[UUID, ...]
    recommendation_ids: tuple[UUID, ...]
    ai_run_ids: tuple[UUID, ...]

    @property
    def evidence_ids(self) -> tuple[UUID, ...]:
        return self.resolution_evidence_ids + self.detection_evidence_ids


@dataclass(frozen=True)
class GmailSyncWorld:
    workspace_id: UUID
    members: Mapping[str, MemberGmailArtifacts]
    mailboxes: Mapping[str, FakeMailbox]
    shared_correspondent_email: str
    shared_person_node_id: UUID
    colliding_external_message_id: str
    # Still active for the world's whole lifetime (fakes, patched adapter/
    # registry, env overrides) -- follow-up disconnect/removal/purge calls
    # made through `client`/`harness` hit the fake Google, never the network.
    harness: GmailSyncHarness

    @property
    def fake_google(self) -> FakeGoogle:
        return self.harness.fake_google

    def client(self, key: str) -> TestClient:
        """Authenticated `TestClient` (session cookie set) for member `key`."""
        return self.harness.client_for(self.workspace_id, self.members[key].user_id)[0]

    def session_token(self, key: str) -> str:
        return self.harness.client_for(self.workspace_id, self.members[key].user_id)[1]

    def headers(self, key: str, *, idempotency_key: str | None = None) -> dict[str, str]:
        """CSRF (+ optional `Idempotency-Key`) headers for member `key`."""
        return csrf_headers(self.session_token(key), idempotency_key)

    @property
    def a(self) -> MemberGmailArtifacts:
        return self.members["a"]

    @property
    def b(self) -> MemberGmailArtifacts:
        return self.members["b"]


# --- harness ------------------------------------------------------------------


def csrf_headers(token: str, idempotency_key: str | None = None) -> dict[str, str]:
    csrf = new(get_settings().session_secret.encode(), token.encode(), "sha256").hexdigest()
    headers = {"X-CSRF-Token": csrf, "X-Correlation-ID": str(uuid4())}
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    return headers


@dataclass
class GmailSyncHarness:
    """Installs the fakes (fake Google behind both `gmail_oauth._adapter`
    and the connector registry, fake Ollama, detection flag + OAuth
    settings) for its lifetime and drives the production endpoints for any
    member. Use via `gmail_sync_harness()`."""

    fake_google: FakeGoogle
    _monkeypatch: pytest.MonkeyPatch
    _clients: dict[UUID, tuple[TestClient, str]] = field(default_factory=dict)
    _allowlist: list[str] = field(default_factory=list)

    def _allow(self, google_email: str) -> None:
        """`ECC_GMAIL_OAUTH_ALLOWLIST` must list every connecting mailbox
        (`GmailAdapter.handle_oauth_callback` rejects anything else)."""
        self._allowlist.append(google_email)
        self._monkeypatch.setenv("ECC_GMAIL_OAUTH_ALLOWLIST", ",".join(self._allowlist))
        get_settings.cache_clear()

    def client_for(self, workspace_id: UUID, user_id: UUID) -> tuple[TestClient, str]:
        """`(TestClient, session_token)` authenticated as `user_id`, creating
        the `sessions` row on first use."""
        if user_id not in self._clients:
            token = f"session-{uuid4()}"
            now = datetime.now(UTC)
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO sessions (id, workspace_id, user_id, token_hash, "
                        "expires_at, last_seen_at) "
                        "VALUES (:id, :workspace_id, :user_id, :token_hash, :expires_at, :now)"
                    ),
                    {
                        "id": uuid4(),
                        "workspace_id": workspace_id,
                        "user_id": user_id,
                        "token_hash": sha256(token.encode()).hexdigest(),
                        "expires_at": now + timedelta(hours=1),
                        "now": now,
                    },
                )
            client = TestClient(app)
            client.cookies.set("ecc_session", token)
            self._clients[user_id] = (client, token)
        return self._clients[user_id]

    def close(self) -> None:
        for client, _token in self._clients.values():
            client.close()
        self._clients.clear()

    def connect_and_sync(
        self, *, workspace_id: UUID, user_id: UUID, key: str, mailbox: FakeMailbox
    ) -> UUID:
        """Enables `email` consent, connects `mailbox` via the real OAuth
        callback, and runs one real backfill `/sync` for `user_id`.
        Returns the new `connector_accounts.id`."""
        client, token = self.client_for(workspace_id, user_id)
        code = self.fake_google.register(key, mailbox)
        self._allow(mailbox.google_email)

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
        connector_account_id = UUID(callback.json()["id"])

        sync = client.post(
            f"/api/v1/engineering/connectors/{connector_account_id}/sync",
            headers=csrf_headers(token, str(uuid4())),
            json={"run_type": "backfill", "resource_type": "message"},
        )
        assert sync.status_code == 201, sync.text
        assert sync.json()["status"] == "succeeded", sync.json()
        return connector_account_id

    def regenerate_attention(self, *, workspace_id: UUID, user_id: UUID) -> None:
        client, token = self.client_for(workspace_id, user_id)
        response = client.post("/api/v1/attention/regenerate", headers=csrf_headers(token), json={})
        assert response.status_code == 200, response.text


@contextmanager
def gmail_sync_harness(*, env: Mapping[str, str] | None = None) -> Iterator[GmailSyncHarness]:
    """Installs the fakes for the `with` body. `env` overrides (or adds to)
    the defaults below -- e.g. `{"ECC_EMAIL_ACTION_DETECTION_ENABLED":
    "false"}` or a feature flag that must be on during sync. On *every*
    exit path the environment and patched attributes are restored first
    and the cached `Settings` dropped after, so no fixture value leaks into
    a later test's `get_settings()`."""
    fake_google = FakeGoogle()
    gmail = GmailAdapter(transport=fake_google.transport())
    # Every production adapter except `gmail`, which is replaced by the
    # fake-transport one (`production_registry` is the object
    # `connector_accounts.connector_registry` is bound to).
    registry = ConnectorRegistry()
    for provider in production_registry.providers():
        adapter = production_registry.get(provider)
        if provider != "gmail" and adapter is not None:
            registry.register(adapter)
    registry.register(gmail)
    fake_ollama = _fake_ollama_adapter()

    defaults = {
        "ECC_EMAIL_ACTION_DETECTION_ENABLED": "true",
        "ECC_GMAIL_OAUTH_CLIENT_ID": "cid",
        "ECC_GMAIL_OAUTH_CLIENT_SECRET": "csecret",
        "ECC_GMAIL_OAUTH_REDIRECT_URI": "https://ecc.example.test/oauth/callback",
    }
    try:
        with pytest.MonkeyPatch.context() as mp:
            for name, value in {**defaults, **(env or {})}.items():
                mp.setenv(name, value)
            # Every module-level production `GmailAdapter` instance (OAuth
            # connect, domain-disable/consent-revoke revocation, thread
            # forget/body fetch) plus the sync registry -- so no call a test
            # makes while the harness is active can reach real Google (a
            # real revoke failure would be swallowed silently).
            for module in _GMAIL_ADAPTER_MODULES:
                mp.setattr(module, "_adapter", gmail)
            mp.setattr(connector_accounts_module, "connector_registry", registry)
            mp.setattr(runtime_module, "OllamaAdapter", lambda *_a, **_k: fake_ollama)
            get_settings.cache_clear()
            reset_circuit_breakers()
            harness = GmailSyncHarness(fake_google=fake_google, _monkeypatch=mp)
            try:
                yield harness
            finally:
                harness.close()
                reset_circuit_breakers()
    finally:
        # Runs after the MonkeyPatch context has restored the environment,
        # whether the body returned or raised: drop `Settings` cached with
        # the patched values.
        get_settings.cache_clear()


# --- collection -------------------------------------------------------------


def collect_member_artifacts(
    *,
    workspace_id: UUID,
    user_id: UUID,
    account_id: UUID,
    mailbox: FakeMailbox,
    connector_account_id: UUID,
) -> MemberGmailArtifacts:
    ext_ids = [m.external_message_id for m in mailbox.messages]
    params: dict[str, Any] = {
        "workspace_id": workspace_id,
        "connector_account_id": connector_account_id,
        "user_id": user_id,
    }
    with engine.begin() as connection:

        def ids(sql: str, extra: dict[str, Any] | None = None) -> tuple[UUID, ...]:
            rows = connection.execute(text(sql), {**params, **(extra or {})}).all()
            return tuple(row[0] for row in rows)

        sync_run_ids = ids(
            "SELECT id FROM sync_runs WHERE workspace_id = :workspace_id "
            "AND connector_account_id = :connector_account_id ORDER BY started_at"
        )
        sync_cursor_ids = ids(
            "SELECT id FROM sync_cursors WHERE workspace_id = :workspace_id "
            "AND connector_account_id = :connector_account_id"
        )
        email_thread_ids = ids(
            "SELECT id FROM email_threads WHERE workspace_id = :workspace_id "
            "AND connector_account_id = :connector_account_id ORDER BY external_thread_id"
        )
        message_rows = connection.execute(
            text(
                "SELECT m.external_message_id, m.id FROM email_messages m "
                "JOIN email_threads t ON t.id = m.thread_id "
                "WHERE m.workspace_id = :workspace_id "
                "AND t.connector_account_id = :connector_account_id"
            ),
            params,
        ).all()
        alias_rows = connection.execute(
            text(
                "SELECT normalized_value, id, entity_id FROM entity_aliases "
                "WHERE workspace_id = :workspace_id AND alias_type = 'email' "
                "AND normalized_value = ANY(:emails)"
            ),
            {**params, "emails": sorted(mailbox.participant_emails)},
        ).all()
        # Resolution evidence: `gmail:<ext>` rows whose node is a
        # participant of *this* mailbox's message `<ext>` -- disambiguates
        # a colliding `<ext>` present in another member's mailbox too.
        node_by_email = {row[0]: row[2] for row in alias_rows}
        resolution_evidence: list[UUID] = []
        for message in mailbox.messages:
            participants = {message.sender_email.casefold(), mailbox.google_email.casefold()}
            node_ids = [node_by_email[e] for e in participants if e in node_by_email]
            resolution_evidence.extend(
                ids(
                    "SELECT id FROM pkos_evidence WHERE workspace_id = :workspace_id "
                    "AND source_type = 'gmail_sync' AND source_ref = :source_ref "
                    "AND node_id = ANY(:node_ids)",
                    {"source_ref": f"gmail:{message.external_message_id}", "node_ids": node_ids},
                )
            )
        # Detection: `ai_runs.input_ref.message_id` names this mailbox's own
        # `email_messages.id`; recommendations are created as the mailbox
        # owner (`created_by`), and cite the detection evidence row.
        message_ids = [str(row[1]) for row in message_rows]
        ai_run_ids = ids(
            "SELECT id FROM ai_runs WHERE workspace_id = :workspace_id "
            "AND task_type = 'email.detect_action' "
            "AND input_ref->>'message_id' = ANY(:message_ids)",
            {"message_ids": message_ids},
        )
        recommendation_rows = connection.execute(
            text(
                "SELECT id, evidence_ids FROM recommendations "
                "WHERE workspace_id = :workspace_id "
                "AND recommendation_type = 'email_action_detected' AND created_by = :user_id"
            ),
            params,
        ).all()
        cited = [UUID(str(e)) for row in recommendation_rows for e in (row[1] or [])]
        detection_evidence_ids = ids(
            "SELECT id FROM pkos_evidence WHERE workspace_id = :workspace_id "
            "AND source_type = 'gmail_sync' AND id = ANY(:cited) "
            "AND source_ref = ANY(:refs)",
            {"cited": cited, "refs": [f"gmail:detect_action:{e}" for e in ext_ids]},
        )
        all_evidence = list(resolution_evidence) + list(detection_evidence_ids)
        ref_rows = connection.execute(
            text("SELECT source_ref, id FROM pkos_evidence WHERE id = ANY(:ids) ORDER BY id"),
            {"ids": all_evidence},
        ).all()
        evidence_ids_by_source_ref: dict[str, tuple[UUID, ...]] = {}
        for ref, evidence_id in ref_rows:
            evidence_ids_by_source_ref[ref] = (
                *evidence_ids_by_source_ref.get(ref, ()),
                evidence_id,
            )
        attention_item_ids = ids(
            "SELECT id FROM attention_items WHERE workspace_id = :workspace_id "
            "AND entity_type = 'email_thread' AND entity_id = ANY(:thread_ids)",
            {"thread_ids": list(email_thread_ids)},
        )

    return MemberGmailArtifacts(
        user_id=user_id,
        account_id=account_id,
        google_email=mailbox.google_email,
        connector_account_id=connector_account_id,
        sync_run_ids=sync_run_ids,
        sync_cursor_ids=sync_cursor_ids,
        email_thread_ids=email_thread_ids,
        email_message_ids={row[0]: row[1] for row in message_rows},
        person_node_ids=node_by_email,
        entity_alias_ids={row[0]: row[1] for row in alias_rows},
        resolution_evidence_ids=tuple(resolution_evidence),
        detection_evidence_ids=detection_evidence_ids,
        evidence_ids_by_source_ref=evidence_ids_by_source_ref,
        attention_item_ids=attention_item_ids,
        recommendation_ids=tuple(row[0] for row in recommendation_rows),
        ai_run_ids=ai_run_ids,
    )


# --- cleanup ------------------------------------------------------------------


def cleanup_workspace(workspace_id: UUID, account_ids: Sequence[UUID]) -> None:
    """Deletes every row this fixture's production calls can write for
    `workspace_id`, the workspace itself, and the identities' `accounts`
    rows (`accounts.email` is globally unique, so leftovers would collide
    with a later run)."""
    with engine.begin() as connection:
        for table in _CLEANUP_TABLES:
            connection.execute(
                text(f"DELETE FROM {table} WHERE workspace_id = :workspace_id"),  # noqa: S608
                {"workspace_id": workspace_id},
            )
        connection.execute(
            text("DELETE FROM workspaces WHERE id = :workspace_id"), {"workspace_id": workspace_id}
        )
        for account_id in account_ids:
            connection.execute(text("DELETE FROM accounts WHERE id = :id"), {"id": account_id})


# --- the two-member world --------------------------------------------------


def _mailbox(
    *, key: str, google_email: str, shared_email: str, colliding_id: str, now: datetime
) -> FakeMailbox:
    return FakeMailbox(
        google_email=google_email,
        messages=(
            FakeGmailMessage(
                external_message_id=f"{key}-shared-{colliding_id}",
                external_thread_id=f"{key}-thread-shared",
                sender_email=shared_email,
                sender_name="Shared Correspondent",
                subject=f"Contract review ({key})",
                body_text="Could you please review and sign the attached contract by Friday?",
                sent_at=now - timedelta(minutes=10),
            ),
            FakeGmailMessage(
                external_message_id=colliding_id,
                external_thread_id=f"{key}-thread-collide",
                sender_email=f"only-{key}-{colliding_id}@partner-{key}.test",
                sender_name=f"Only {key.upper()} Sender",
                subject=f"Quarterly numbers ({key})",
                body_text="Please send over the quarterly numbers when you get a chance.",
                sent_at=now - timedelta(minutes=5),
            ),
        ),
    )


@contextmanager
def build_gmail_sync_world(*, env: Mapping[str, str] | None = None) -> Iterator[GmailSyncWorld]:
    """Two members of one new workspace, each with a real-synced Gmail
    mailbox: A (`owner`, the workspace's original user) and B (`member`).
    `env` overrides are applied for the sync and stay applied while the
    world is yielded (see `gmail_sync_harness`). The harness stays active
    until the world is torn down; the workspace is deleted afterwards."""
    suffix = uuid4().hex[:10]
    workspace_id = uuid4()
    user_ids = {"a": uuid4(), "b": uuid4()}
    now = datetime.now(UTC)
    colliding_id = f"collide{suffix}"
    shared_email = f"shared-{suffix}@partner.test"
    google_emails = {k: f"gmail-sync-{k}-{suffix}@example.test" for k in user_ids}
    mailboxes = {
        k: _mailbox(
            key=k,
            google_email=google_emails[k],
            shared_email=shared_email,
            colliding_id=colliding_id,
            now=now,
        )
        for k in user_ids
    }
    account_ids: dict[str, UUID] = {}

    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, 'Gmail Real-Sync Fixture', 'UTC', :now)"
            ),
            {"id": workspace_id, "now": now},
        )
        # A first (earliest `users.created_at`) -- the workspace's original
        # user, which the `*_default_owner_from_workspace_original_user`
        # triggers resolve to.
        account_ids["a"] = create_identity(
            connection,
            workspace_id=workspace_id,
            user_id=user_ids["a"],
            email=google_emails["a"],
            now=now,
            role="owner",
        )
        account_ids["b"] = create_identity(
            connection,
            workspace_id=workspace_id,
            user_id=user_ids["b"],
            email=google_emails["b"],
            now=now + timedelta(seconds=1),
            role="member",
        )

    try:
        with gmail_sync_harness(env=env) as harness:
            connector_ids = {
                k: harness.connect_and_sync(
                    workspace_id=workspace_id, user_id=user_ids[k], key=k, mailbox=mailboxes[k]
                )
                for k in ("a", "b")
            }
            for k in ("a", "b"):
                harness.regenerate_attention(workspace_id=workspace_id, user_id=user_ids[k])

            members = {
                k: collect_member_artifacts(
                    workspace_id=workspace_id,
                    user_id=user_ids[k],
                    account_id=account_ids[k],
                    mailbox=mailboxes[k],
                    connector_account_id=connector_ids[k],
                )
                for k in ("a", "b")
            }
            yield GmailSyncWorld(
                workspace_id=workspace_id,
                members=members,
                mailboxes=mailboxes,
                shared_correspondent_email=shared_email,
                shared_person_node_id=members["a"].person_node_ids[shared_email],
                colliding_external_message_id=colliding_id,
                harness=harness,
            )
    finally:
        cleanup_workspace(workspace_id, list(account_ids.values()))


@pytest.fixture
def gmail_sync_world(request: pytest.FixtureRequest) -> Iterator[GmailSyncWorld]:
    """Pytest fixture over `build_gmail_sync_world()`. Import it into a test
    module (`from gmail_sync_fixtures import gmail_sync_world  # noqa: F401`)
    to use it by name. Env overrides via indirect parametrization:
    `@pytest.mark.parametrize("gmail_sync_world", [{"ECC_X": "true"}],
    indirect=True)`."""
    env: Mapping[str, str] | None = getattr(request, "param", None)
    with build_gmail_sync_world(env=env) as world:
        yield world


GmailSyncWorldFactory = Callable[..., GmailSyncWorld]


@pytest.fixture
def gmail_sync_world_factory() -> Iterator[GmailSyncWorldFactory]:
    """Factory form: `world = gmail_sync_world_factory(env={...})`, callable
    more than once per test; every world built is torn down (harness,
    then workspace, most recent first) at the end of the test. Worlds
    nest: while a later world is alive its harness's patches are the
    active ones, so drive follow-up calls through the most recently built
    world."""
    with ExitStack() as stack:

        def build(*, env: Mapping[str, str] | None = None) -> GmailSyncWorld:
            return stack.enter_context(build_gmail_sync_world(env=env))

        yield build
