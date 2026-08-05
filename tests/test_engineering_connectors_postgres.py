"""Phase 6 Engineering Workspace Task 1: connector framework and source
projections (`docs/superpowers/specs/2026-07-27-phase-6-engineering-
workspace-design.md`, `docs/phases/phase-006/CONNECTOR-CONTRACT.md`).

Covers, per this task's own scope ("connector framework and source
projections -- the platform layer only"):

1. `ConnectorRegistry`/`ConnectorAdapter` contract shape, exercised
   directly against `sandbox_adapter.SandboxGithubAdapter` (unit-level, no
   database).
2. Credential encryption round-trip (`ecc.domains.engineering.crypto`) --
   ciphertext never equals plaintext, decrypt recovers the original.
3. `POST /api/v1/engineering/connectors`: authorization success/failure,
   unsupported-provider rejection, duplicate-connection rejection, and
   that no response ever carries the credential or the encrypted column.
4. `GET /api/v1/engineering/connectors` cross-workspace isolation.
5. `POST .../{id}/sync` backfill then incremental (cursor progression),
   `GET /api/v1/engineering/sync-runs` ordering, and the disconnected-
   connector rejection.
6. `POST .../{id}/disable`: revokes (best-effort, sandbox no-ops),
   transitions to `disconnected`, and is idempotent when called again.
7. `Idempotency-Key` replay on create, and `IDEMPOTENCY_CONFLICT` when the
   same key is reused with a different payload.
8. Cross-workspace 404s on sync/disable (workspace B cannot act on
   workspace A's connector).

Plus a second, post-first-review batch closing gaps a multi-persona review
of this task's first PR found:

9. The adapter-raises-exception failure path on `/sync` (`sync_runs.
   status='failed'`, `connector_accounts.status='error'`, `updated_by` set,
   an audit/outbox row written) -- exercised via a throwaway raising fake
   adapter substituted into `connector_accounts.connector_registry`
   (monkeypatched), since the real `sandbox.github` adapter never raises.
10. Direct-SQL rejection tests for all five migration CHECK constraints.
11. `run_type='webhook'`/an out-of-set `resource_type` on `/sync` is now a
    Pydantic 422 validation error, not a runtime branch -- both request-
    shape rejections are tested.
12. `disable` actually invokes `adapter.disconnect()` (spy adapter), and
    still succeeds when that call raises (a second, raising fake adapter).
13. Two different workspaces connecting with the identical credential both
    succeed (the unique constraint is workspace-scoped, not global).
14. `GET /sync-runs` with no `connector_account_id` filter.
15. `Idempotency-Key` replay (not just conflict) on `/sync` and `/disable`.
16. `CONNECTOR_PROVIDER_NOT_SUPPORTED` reachable from `/sync` (a connector
    row for a provider with no registered adapter, inserted directly).
17. `updated_by` is set correctly by a sync call (success and failure).
18. `disable` still succeeds when the stored credential fails to decrypt
    (e.g. after an encryption-key rotation) -- corrupted ciphertext
    inserted directly, decrypt failure must not block disconnecting.

Plus a third batch closing gaps a multi-persona review of Task 2's PR
found in the pool-exhaustion restructuring `sync_connector_endpoint`
underwent for that task (`connector_accounts.py`'s module docstring has
the full mechanism):

19. `uq_sync_runs_running_per_account` (migration 0046) rejects a `/sync`
    call while another run for the same account is still `running` with
    `409 CONNECTOR_SYNC_IN_PROGRESS`, closing the lost-cursor-update and
    idempotency-double-execution races the phase restructuring reopened.
20. The audit trail's `aggregate_version` matches the account's actual
    post-update `version` (via `_finalize_account_version`/`RETURNING`),
    not phase 1's now-stale in-memory snapshot -- both the sync-success
    and sync-failure event paths.
21. `_finalize_account_version`'s `AND status != 'disconnected'` guard,
    exercised directly (the concurrent-disable scenario it protects
    requires real thread concurrency to reproduce over HTTP).

Plus a fourth batch closing gaps a second review round of the third
batch's own fixes found:

22. `test_two_concurrent_syncs_for_same_account_second_gets_conflict`:
    the migration-0046 guard proven against two *real*, genuinely
    concurrent `/sync` requests (`ThreadPoolExecutor` + a blocking fake
    adapter), not just a synthetically pre-inserted `running` row.
23. A `running` `sync_runs` row old enough to be a crashed request's
    leftover is reaped (marked `failed`) before a new `/sync` call is
    blocked by it; a recent one is not reaped and still correctly blocks.
24. `create_connector_endpoint`'s own idempotency-retry race (a
    same-key retry racing ahead of phase 3), proven against two real
    concurrent requests the same way as item 22, and closed by re-
    checking the idempotency cache after an `IntegrityError` rather than
    unconditionally returning `409 CONNECTOR_ALREADY_CONNECTED`.
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

import pytest
from fastapi.testclient import TestClient
from identity_fixtures import create_identity
from sqlalchemy import text

from ecc.config import get_settings
from ecc.database import STATEMENT_TIMEOUT_MS, engine
from ecc.domains.engineering import connector_accounts as connector_accounts_module
from ecc.domains.engineering.connectors import (
    AdapterAuthorizationError,
    ConnectorAccountContext,
    ConnectorAuthorization,
    ConnectorRegistry,
    SyncOutcome,
)
from ecc.domains.engineering.crypto import decrypt_credential, encrypt_credential
from ecc.domains.engineering.sandbox_adapter import SandboxGithubAdapter
from ecc.main import app

settings = get_settings()
pytestmark = pytest.mark.skipif(
    not settings.database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)


# --- throwaway fake adapters for the failure/spy paths ----------------------
# All registered under `provider = "sandbox"` -- `ck_connector_accounts_
# provider`'s CHECK constraint only allows `github`/`gitlab`/`jira`/
# `sandbox` (proven directly by the constraint-rejection tests below), so a
# test-only provider slug is not an option. Each test that uses one of
# these inserts its `connector_accounts` row directly via SQL (bypassing
# `create_connector_endpoint`, which would otherwise resolve `"sandbox"`
# against the *real* `SandboxGithubAdapter`) and monkeypatches
# `connector_accounts.connector_registry` to a throwaway registry mapping
# `"sandbox"` to the fake adapter instead, for that test only. Each fake
# satisfies `ConnectorAdapter` structurally (`@runtime_checkable`),
# matching `sandbox_adapter.SandboxGithubAdapter`'s own shape.


@dataclass
class _RaisingAdapter:
    """Every sync call raises -- the real `sandbox.github` adapter never
    does, so this is the only way to exercise `/sync`'s failure path.
    """

    provider: str = "sandbox"
    required_scopes: frozenset[str] = field(default_factory=lambda: frozenset({"contents:read"}))

    def authorize(self, credential: str) -> ConnectorAuthorization:
        return ConnectorAuthorization(
            external_account_id="raising-account",
            display_name="Raising fake",
            granted_scopes=self.required_scopes,
        )

    def backfill(self, account: ConnectorAccountContext, resource_type: str) -> SyncOutcome:
        raise RuntimeError("simulated adapter failure")

    def incremental_sync(
        self, account: ConnectorAccountContext, resource_type: str, cursor: str | None
    ) -> SyncOutcome:
        raise RuntimeError("simulated adapter failure")

    def handle_webhook(
        self, account: ConnectorAccountContext, payload: bytes, headers: object
    ) -> SyncOutcome:
        raise NotImplementedError

    def refresh_permissions(self, account: ConnectorAccountContext) -> str:
        return "active"

    def disconnect(self, account: ConnectorAccountContext) -> None:
        return None


@dataclass
class _SpyDisconnectAdapter:
    """Records every `disconnect()` call so a test can assert it actually
    happened, rather than only asserting the HTTP-level status transition.
    """

    provider: str = "sandbox"
    required_scopes: frozenset[str] = field(default_factory=lambda: frozenset({"contents:read"}))
    disconnect_calls: list[ConnectorAccountContext] = field(default_factory=list)

    def authorize(self, credential: str) -> ConnectorAuthorization:
        return ConnectorAuthorization(
            external_account_id="spy-account",
            display_name="Spy fake",
            granted_scopes=self.required_scopes,
        )

    def backfill(self, account: ConnectorAccountContext, resource_type: str) -> SyncOutcome:
        return SyncOutcome(
            resource_type=resource_type, items_processed=1, status="succeeded", next_cursor="1"
        )

    def incremental_sync(
        self, account: ConnectorAccountContext, resource_type: str, cursor: str | None
    ) -> SyncOutcome:
        return self.backfill(account, resource_type)

    def handle_webhook(
        self, account: ConnectorAccountContext, payload: bytes, headers: object
    ) -> SyncOutcome:
        raise NotImplementedError

    def refresh_permissions(self, account: ConnectorAccountContext) -> str:
        return "active"

    def disconnect(self, account: ConnectorAccountContext) -> None:
        self.disconnect_calls.append(account)


@dataclass
class _RaisingDisconnectAdapter(_SpyDisconnectAdapter):
    """`disconnect()` always raises -- proves `disable` still succeeds
    (best-effort revocation) rather than only asserting the happy path.
    """

    provider: str = "sandbox"

    def disconnect(self, account: ConnectorAccountContext) -> None:
        self.disconnect_calls.append(account)
        raise RuntimeError("revocation endpoint down")


@dataclass
class _SlowDisconnectAdapter:
    """Blocks inside `disconnect()` until released -- round 23 review found
    `disable_connector_endpoint` used to call `adapter.disconnect(...)`
    *inside* its own transaction, while still holding the `FOR UPDATE` lock
    taken on the account row; every adapter registered up to Phase 10 had
    an in-process no-op `disconnect()`, so this was never observable until
    `GmailAdapter.disconnect()` became the first one to make a real,
    blocking outbound HTTPS call. This fake reproduces that shape for the
    provider-agnostic `sandbox` adapter so the fix can be proven without
    depending on Gmail's own OAuth machinery.
    """

    provider: str = "sandbox"
    required_scopes: frozenset[str] = field(default_factory=lambda: frozenset({"contents:read"}))
    entered_disconnect: threading.Event = field(default_factory=threading.Event)
    release: threading.Event = field(default_factory=threading.Event)

    def authorize(self, credential: str) -> ConnectorAuthorization:
        return ConnectorAuthorization(
            external_account_id="slow-disconnect-account",
            display_name="Slow disconnect fake",
            granted_scopes=self.required_scopes,
        )

    def backfill(self, account: ConnectorAccountContext, resource_type: str) -> SyncOutcome:
        raise NotImplementedError

    def incremental_sync(
        self, account: ConnectorAccountContext, resource_type: str, cursor: str | None
    ) -> SyncOutcome:
        raise NotImplementedError

    def handle_webhook(
        self, account: ConnectorAccountContext, payload: bytes, headers: object
    ) -> SyncOutcome:
        raise NotImplementedError

    def refresh_permissions(self, account: ConnectorAccountContext) -> str:
        return "active"

    def disconnect(self, account: ConnectorAccountContext) -> None:
        self.entered_disconnect.set()
        self.release.wait(timeout=5)


@dataclass
class _SlowAdapter:
    """Blocks inside `backfill`/`incremental_sync` (phase 2, no pooled
    connection held) until released -- lets a test hold a real `/sync`
    request open in phase 2 so a second, genuinely concurrent request can
    race it, rather than only racing against a synthetically pre-inserted
    `running` row.
    """

    provider: str = "sandbox"
    required_scopes: frozenset[str] = field(default_factory=lambda: frozenset({"contents:read"}))
    entered_phase2: threading.Event = field(default_factory=threading.Event)
    release: threading.Event = field(default_factory=threading.Event)

    def authorize(self, credential: str) -> ConnectorAuthorization:
        return ConnectorAuthorization(
            external_account_id="slow-account",
            display_name="Slow fake",
            granted_scopes=self.required_scopes,
        )

    def backfill(self, account: ConnectorAccountContext, resource_type: str) -> SyncOutcome:
        self.entered_phase2.set()
        self.release.wait(timeout=5)
        return SyncOutcome(
            resource_type=resource_type, items_processed=1, status="succeeded", next_cursor="1"
        )

    def incremental_sync(
        self, account: ConnectorAccountContext, resource_type: str, cursor: str | None
    ) -> SyncOutcome:
        return self.backfill(account, resource_type)

    def handle_webhook(
        self, account: ConnectorAccountContext, payload: bytes, headers: object
    ) -> SyncOutcome:
        raise NotImplementedError

    def refresh_permissions(self, account: ConnectorAccountContext) -> str:
        return "active"

    def disconnect(self, account: ConnectorAccountContext) -> None:
        return None


@dataclass
class _SlowAuthorizeAdapter:
    """Blocks inside `authorize` (`create_connector_endpoint`'s own phase
    2, no pooled connection held) on a two-party `threading.Barrier` --
    the `POST /connectors` analogue of `_SlowAdapter` above, needed since
    that fake only blocks in `backfill`/`incremental_sync`, never
    `authorize`.

    A `Barrier`, not an `Event` a first caller sets and a second caller
    waits on, is deliberate: it guarantees *both* concurrent requests have
    already completed their own phase 1 (including a genuine idempotency-
    cache miss -- neither could have found a cached response yet, since
    neither has reached phase 3) before *either* is released into phase 3.
    An Event-based "release immediately after submitting the second
    request" ordering does not guarantee this -- the second request still
    has its own phase 1 ahead of it when released, so it could resolve via
    phase 1's own pre-existing cache check instead of ever reaching phase
    3's `IntegrityError`/cache-recheck path the test exists to exercise.
    """

    provider: str = "sandbox"
    required_scopes: frozenset[str] = field(default_factory=lambda: frozenset({"contents:read"}))
    barrier: threading.Barrier = field(default_factory=lambda: threading.Barrier(2, timeout=5))

    def authorize(self, credential: str) -> ConnectorAuthorization:
        self.barrier.wait()
        return ConnectorAuthorization(
            external_account_id="slow-authorize-account",
            display_name="Slow authorize fake",
            granted_scopes=self.required_scopes,
        )

    def backfill(self, account: ConnectorAccountContext, resource_type: str) -> SyncOutcome:
        raise NotImplementedError

    def incremental_sync(
        self, account: ConnectorAccountContext, resource_type: str, cursor: str | None
    ) -> SyncOutcome:
        raise NotImplementedError

    def handle_webhook(
        self, account: ConnectorAccountContext, payload: bytes, headers: object
    ) -> SyncOutcome:
        raise NotImplementedError

    def refresh_permissions(self, account: ConnectorAccountContext) -> str:
        return "active"

    def disconnect(self, account: ConnectorAccountContext) -> None:
        return None


def _registry_with(adapter: Any) -> ConnectorRegistry:
    registry = ConnectorRegistry()
    registry.register(adapter)
    return registry


# --- unit-level: sandbox adapter / registry / crypto (no database) ---------


def test_sandbox_adapter_authorize_rejects_invalid_credential() -> None:
    adapter = SandboxGithubAdapter()
    with pytest.raises(AdapterAuthorizationError):
        adapter.authorize("this-is-invalid")
    with pytest.raises(AdapterAuthorizationError):
        adapter.authorize("")


def test_sandbox_adapter_authorize_is_deterministic() -> None:
    adapter = SandboxGithubAdapter()
    first = adapter.authorize("token-a")
    second = adapter.authorize("token-a")
    third = adapter.authorize("token-b")
    assert first.external_account_id == second.external_account_id
    assert first.external_account_id != third.external_account_id
    assert first.granted_scopes == adapter.required_scopes


def test_sandbox_adapter_incremental_sync_advances_cursor() -> None:
    adapter = SandboxGithubAdapter()
    context = ConnectorAccountContext(
        workspace_id=uuid4(),
        connector_account_id=uuid4(),
        external_account_id="sandbox-x",
        credential="token",
    )
    backfill = adapter.backfill(context, "repository")
    assert backfill.next_cursor == "1"
    assert backfill.items_processed == 3

    step_two = adapter.incremental_sync(context, "repository", backfill.next_cursor)
    assert step_two.next_cursor == "2"
    assert step_two.items_processed == 1

    fresh = adapter.incremental_sync(context, "repository", None)
    assert fresh.next_cursor == "1"


def test_sandbox_adapter_refresh_permissions() -> None:
    adapter = SandboxGithubAdapter()
    active_context = ConnectorAccountContext(
        workspace_id=uuid4(), connector_account_id=uuid4(), external_account_id="x", credential="ok"
    )
    lost_context = ConnectorAccountContext(
        workspace_id=uuid4(),
        connector_account_id=uuid4(),
        external_account_id="x",
        credential="please lose-access now",
    )
    assert adapter.refresh_permissions(active_context) == "active"
    assert adapter.refresh_permissions(lost_context) == "permission_lost"


def test_sandbox_adapter_handle_webhook() -> None:
    adapter = SandboxGithubAdapter()
    context = ConnectorAccountContext(
        workspace_id=uuid4(), connector_account_id=uuid4(), external_account_id="x", credential="ok"
    )
    with_payload = adapter.handle_webhook(context, b'{"event": "push"}', {})
    assert with_payload.items_processed == 1
    assert with_payload.status == "succeeded"

    empty_payload = adapter.handle_webhook(context, b"", {})
    assert empty_payload.items_processed == 0


def test_connector_registry_rejects_duplicate_provider() -> None:
    from ecc.domains.engineering.connectors import AdapterAlreadyRegistered

    registry = ConnectorRegistry()
    registry.register(SandboxGithubAdapter())
    with pytest.raises(AdapterAlreadyRegistered):
        registry.register(SandboxGithubAdapter())


def test_credential_encryption_round_trip() -> None:
    plaintext = "ghp_super_secret_token_value"
    ciphertext = encrypt_credential(plaintext)
    assert ciphertext != plaintext.encode()
    assert plaintext.encode() not in ciphertext
    assert decrypt_credential(ciphertext) == plaintext


# --- HTTP-level: connector lifecycle ---------------------------------------


@pytest.fixture
def engineering_test_context() -> Iterator[tuple[TestClient, UUID, UUID, str]]:
    workspace_id = uuid4()
    user_id = uuid4()
    token = f"session-{uuid4()}"
    now = datetime.now(UTC)

    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, 'Engineering Connector Test', 'Asia/Kolkata', :now)"
            ),
            {"id": workspace_id, "now": now},
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
    try:
        yield client, workspace_id, user_id, token
    finally:
        client.close()
        _cleanup_workspace(workspace_id)


def _cleanup_workspace(workspace_id: UUID) -> None:
    with engine.begin() as connection:
        for table in (
            "sync_runs",
            "sync_cursors",
            "connector_accounts",
            "event_outbox",
            "audit_events",
            "idempotency_records",
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


def _headers(token: str, key: str | None = None) -> dict[str, str]:
    csrf = new(settings.session_secret.encode(), token.encode(), "sha256").hexdigest()
    headers = {"X-CSRF-Token": csrf, "X-Correlation-ID": str(uuid4())}
    if key is not None:
        headers["Idempotency-Key"] = key
    return headers


def _insert_connector_account(
    workspace_id: UUID,
    user_id: UUID,
    *,
    provider: str = "sandbox",
    external_account_id: str | None = None,
    credential: str = "fixture-credential",
    encrypted_credentials: bytes | None = None,
    status: str = "active",
) -> UUID:
    """Inserts a `connector_accounts` row directly, bypassing `POST
    /connectors` (and therefore its `authorize()` call) -- used by tests
    that need a row for a provider the real registry has no adapter for
    (`CONNECTOR_PROVIDER_NOT_SUPPORTED` on `/sync`), or one a monkeypatched
    fake adapter will handle instead of the real `SandboxGithubAdapter`.
    `encrypted_credentials` overrides the encrypted form of `credential`
    entirely when supplied -- used to seed deliberately-corrupt ciphertext
    for the decrypt-failure-during-disable test.
    """
    account_id = uuid4()
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO connector_accounts (
                    id, workspace_id, provider, external_account_id, display_name,
                    granted_scopes, encrypted_credentials, status, version,
                    created_by, updated_by, created_at, updated_at
                ) VALUES (
                    :id, :workspace_id, :provider, :external_account_id, :display_name,
                    :granted_scopes, :encrypted_credentials, :status, 1,
                    :actor_id, :actor_id, :now, :now
                )
                """
            ),
            {
                "id": account_id,
                "workspace_id": workspace_id,
                "provider": provider,
                "external_account_id": external_account_id or f"fixture-{account_id}",
                "display_name": "Fixture connector",
                "granted_scopes": ["contents:read"],
                "encrypted_credentials": encrypted_credentials or encrypt_credential(credential),
                "status": status,
                "actor_id": user_id,
                "now": now,
            },
        )
    return account_id


def test_create_connector_success_never_returns_credential(
    engineering_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, _user_id, token = engineering_test_context
    response = client.post(
        "/api/v1/engineering/connectors",
        json={"provider": "sandbox", "credential": "token-abc"},
        headers=_headers(token, key=str(uuid4())),
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert "credential" not in body
    assert "encrypted_credentials" not in body
    assert body["provider"] == "sandbox"
    assert body["status"] == "active"
    assert sorted(body["granted_scopes"]) == ["contents:read", "metadata:read"]

    with engine.begin() as connection:
        row = connection.execute(
            text(
                "SELECT encrypted_credentials FROM connector_accounts "
                "WHERE workspace_id = :workspace_id"
            ),
            {"workspace_id": workspace_id},
        ).one()
        stored = bytes(row[0])
        assert b"token-abc" not in stored
        assert decrypt_credential(stored) == "token-abc"


def test_create_connector_rejects_invalid_credential(
    engineering_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = engineering_test_context
    response = client.post(
        "/api/v1/engineering/connectors",
        json={"provider": "sandbox", "credential": "this-is-invalid"},
        headers=_headers(token, key=str(uuid4())),
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "CONNECTOR_AUTHORIZATION_FAILED"


def test_create_connector_rejects_unsupported_provider(
    engineering_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = engineering_test_context
    response = client.post(
        "/api/v1/engineering/connectors",
        json={"provider": "bitbucket", "credential": "token-abc"},
        headers=_headers(token, key=str(uuid4())),
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "CONNECTOR_PROVIDER_NOT_SUPPORTED"


def test_create_connector_rejects_duplicate_connection(
    engineering_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = engineering_test_context
    first = client.post(
        "/api/v1/engineering/connectors",
        json={"provider": "sandbox", "credential": "token-dup"},
        headers=_headers(token, key=str(uuid4())),
    )
    assert first.status_code == 201

    second = client.post(
        "/api/v1/engineering/connectors",
        json={"provider": "sandbox", "credential": "token-dup"},
        headers=_headers(token, key=str(uuid4())),
    )
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "CONNECTOR_ALREADY_CONNECTED"


def test_create_connector_idempotency_replay_and_conflict(
    engineering_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = engineering_test_context
    key = str(uuid4())
    payload = {"provider": "sandbox", "credential": "token-idem"}

    first = client.post(
        "/api/v1/engineering/connectors", json=payload, headers=_headers(token, key=key)
    )
    assert first.status_code == 201

    replay = client.post(
        "/api/v1/engineering/connectors", json=payload, headers=_headers(token, key=key)
    )
    assert replay.status_code == 201
    assert replay.json()["id"] == first.json()["id"]

    conflict = client.post(
        "/api/v1/engineering/connectors",
        json={"provider": "sandbox", "credential": "different-token"},
        headers=_headers(token, key=key),
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"

    with engine.begin() as connection:
        count = connection.execute(
            text("SELECT count(*) FROM connector_accounts WHERE workspace_id = :workspace_id"),
            {"workspace_id": _workspace_id},
        ).scalar_one()
        assert count == 1


def test_list_connectors_cross_workspace_isolation(
    engineering_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = engineering_test_context
    client.post(
        "/api/v1/engineering/connectors",
        json={"provider": "sandbox", "credential": "token-list"},
        headers=_headers(token, key=str(uuid4())),
    )

    workspace_b_id = uuid4()
    user_b_id = uuid4()
    token_b = f"session-{uuid4()}"
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, 'Other Workspace', 'Asia/Kolkata', :now)"
            ),
            {"id": workspace_b_id, "now": now},
        )
        create_identity(
            connection,
            workspace_id=workspace_b_id,
            user_id=user_b_id,
            email=f"{user_b_id}@example.test",
            now=now,
        )
        connection.execute(
            text(
                "INSERT INTO sessions (id, workspace_id, user_id, token_hash, "
                "expires_at, last_seen_at) "
                "VALUES (:id, :workspace_id, :user_id, :token_hash, :expires_at, :now)"
            ),
            {
                "id": uuid4(),
                "workspace_id": workspace_b_id,
                "user_id": user_b_id,
                "token_hash": sha256(token_b.encode()).hexdigest(),
                "expires_at": now + timedelta(hours=1),
                "now": now,
            },
        )
    try:
        client_b = TestClient(app)
        client_b.cookies.set("ecc_session", token_b)
        response = client_b.get("/api/v1/engineering/connectors")
        assert response.status_code == 200
        assert response.json()["connectors"] == []
        client_b.close()
    finally:
        _cleanup_workspace(workspace_b_id)


def test_sync_backfill_then_incremental_advances_cursor_and_records_runs(
    engineering_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, _user_id, token = engineering_test_context
    created = client.post(
        "/api/v1/engineering/connectors",
        json={"provider": "sandbox", "credential": "token-sync"},
        headers=_headers(token, key=str(uuid4())),
    ).json()
    account_id = created["id"]

    backfill = client.post(
        f"/api/v1/engineering/connectors/{account_id}/sync",
        json={"run_type": "backfill", "resource_type": "repository"},
        headers=_headers(token, key=str(uuid4())),
    )
    assert backfill.status_code == 201, backfill.text
    assert backfill.json()["status"] == "succeeded"
    assert backfill.json()["items_processed"] == 3

    incremental = client.post(
        f"/api/v1/engineering/connectors/{account_id}/sync",
        json={"run_type": "incremental", "resource_type": "repository"},
        headers=_headers(token, key=str(uuid4())),
    )
    assert incremental.status_code == 201, incremental.text
    assert incremental.json()["items_processed"] == 1

    with engine.begin() as connection:
        cursor = connection.execute(
            text(
                "SELECT cursor_value FROM sync_cursors WHERE workspace_id = :workspace_id "
                "AND connector_account_id = :account_id AND resource_type = 'repository'"
            ),
            {"workspace_id": workspace_id, "account_id": UUID(account_id)},
        ).scalar_one()
        assert cursor == "2"

        last_synced = connection.execute(
            text("SELECT last_synced_at FROM connector_accounts WHERE id = :account_id"),
            {"account_id": UUID(account_id)},
        ).scalar_one()
        assert last_synced is not None

    runs = client.get("/api/v1/engineering/sync-runs", params={"connector_account_id": account_id})
    assert runs.status_code == 200
    run_bodies = runs.json()["sync_runs"]
    assert len(run_bodies) == 2
    assert run_bodies[0]["started_at"] >= run_bodies[1]["started_at"]


def test_sync_rejected_after_disconnect(
    engineering_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = engineering_test_context
    created = client.post(
        "/api/v1/engineering/connectors",
        json={"provider": "sandbox", "credential": "token-disconnect"},
        headers=_headers(token, key=str(uuid4())),
    ).json()
    account_id = created["id"]

    disable = client.post(
        f"/api/v1/engineering/connectors/{account_id}/disable",
        headers=_headers(token, key=str(uuid4())),
    )
    assert disable.status_code == 200
    assert disable.json()["status"] == "disconnected"
    assert disable.json()["disconnected_at"] is not None

    # Idempotent: disabling an already-disconnected connector again is a no-op 200.
    disable_again = client.post(
        f"/api/v1/engineering/connectors/{account_id}/disable",
        headers=_headers(token, key=str(uuid4())),
    )
    assert disable_again.status_code == 200
    assert disable_again.json()["status"] == "disconnected"

    sync_after_disconnect = client.post(
        f"/api/v1/engineering/connectors/{account_id}/sync",
        json={"run_type": "backfill", "resource_type": "repository"},
        headers=_headers(token, key=str(uuid4())),
    )
    assert sync_after_disconnect.status_code == 409
    assert sync_after_disconnect.json()["error"]["code"] == "CONNECTOR_DISCONNECTED"


def test_sync_and_disable_cross_workspace_404(
    engineering_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = engineering_test_context
    created = client.post(
        "/api/v1/engineering/connectors",
        json={"provider": "sandbox", "credential": "token-cross"},
        headers=_headers(token, key=str(uuid4())),
    ).json()
    account_id = created["id"]

    workspace_b_id = uuid4()
    user_b_id = uuid4()
    token_b = f"session-{uuid4()}"
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, 'Other Workspace B', 'Asia/Kolkata', :now)"
            ),
            {"id": workspace_b_id, "now": now},
        )
        create_identity(
            connection,
            workspace_id=workspace_b_id,
            user_id=user_b_id,
            email=f"{user_b_id}@example.test",
            now=now,
        )
        connection.execute(
            text(
                "INSERT INTO sessions (id, workspace_id, user_id, token_hash, "
                "expires_at, last_seen_at) "
                "VALUES (:id, :workspace_id, :user_id, :token_hash, :expires_at, :now)"
            ),
            {
                "id": uuid4(),
                "workspace_id": workspace_b_id,
                "user_id": user_b_id,
                "token_hash": sha256(token_b.encode()).hexdigest(),
                "expires_at": now + timedelta(hours=1),
                "now": now,
            },
        )
    try:
        client_b = TestClient(app)
        client_b.cookies.set("ecc_session", token_b)

        sync_response = client_b.post(
            f"/api/v1/engineering/connectors/{account_id}/sync",
            json={"run_type": "backfill", "resource_type": "repository"},
            headers=_headers(token_b, key=str(uuid4())),
        )
        assert sync_response.status_code == 404
        assert sync_response.json()["error"]["code"] == "CONNECTOR_NOT_FOUND"

        disable_response = client_b.post(
            f"/api/v1/engineering/connectors/{account_id}/disable",
            headers=_headers(token_b, key=str(uuid4())),
        )
        assert disable_response.status_code == 404
        assert disable_response.json()["error"]["code"] == "CONNECTOR_NOT_FOUND"

        client_b.close()
    finally:
        _cleanup_workspace(workspace_b_id)


# --- second review batch: failure path, constraints, spies, edge cases -----


def test_sync_adapter_failure_marks_run_and_account_failed(
    engineering_test_context: tuple[TestClient, UUID, UUID, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, workspace_id, user_id, token = engineering_test_context
    monkeypatch.setattr(
        connector_accounts_module, "connector_registry", _registry_with(_RaisingAdapter())
    )
    account_id = _insert_connector_account(workspace_id, user_id)

    response = client.post(
        f"/api/v1/engineering/connectors/{account_id}/sync",
        json={"run_type": "backfill", "resource_type": "repository"},
        headers=_headers(token, key=str(uuid4())),
    )
    # Still 201: a real sync_runs row was created and is returned, even
    # though the sync itself failed -- see connector_accounts.py's own
    # module docstring for this status-code reasoning.
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "failed"
    assert "simulated adapter failure" in body["error_summary"]

    with engine.begin() as connection:
        account_row = (
            connection.execute(
                text(
                    "SELECT status, last_error, updated_by FROM connector_accounts WHERE id = :id"
                ),
                {"id": account_id},
            )
            .mappings()
            .one()
        )
        assert account_row["status"] == "error"
        assert account_row["last_error"] is not None
        assert account_row["updated_by"] == user_id

        audit_count = connection.execute(
            text(
                "SELECT count(*) FROM audit_events WHERE workspace_id = :workspace_id "
                "AND event_type = 'connector_account.sync_failed'"
            ),
            {"workspace_id": workspace_id},
        ).scalar_one()
        assert audit_count == 1


def test_sync_success_sets_updated_by(
    engineering_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, user_id, token = engineering_test_context
    account_id = _insert_connector_account(workspace_id, user_id)

    response = client.post(
        f"/api/v1/engineering/connectors/{account_id}/sync",
        json={"run_type": "backfill", "resource_type": "repository"},
        headers=_headers(token, key=str(uuid4())),
    )
    assert response.status_code == 201, response.text

    with engine.begin() as connection:
        updated_by = connection.execute(
            text("SELECT updated_by FROM connector_accounts WHERE id = :id"),
            {"id": account_id},
        ).scalar_one()
        assert updated_by == user_id


@pytest.mark.parametrize(
    ("table", "column", "value"),
    [
        ("connector_accounts", "provider", "not-a-real-provider"),
        ("connector_accounts", "status", "not-a-real-status"),
    ],
)
def test_connector_accounts_check_constraints_reject_invalid_values(
    engineering_test_context: tuple[TestClient, UUID, UUID, str],
    table: str,
    column: str,
    value: str,
) -> None:
    from psycopg.errors import CheckViolation
    from sqlalchemy.exc import IntegrityError

    _client, workspace_id, user_id, _token = engineering_test_context
    now = datetime.now(UTC)
    with pytest.raises(IntegrityError) as excinfo:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO connector_accounts (
                        id, workspace_id, provider, external_account_id, display_name,
                        granted_scopes, encrypted_credentials, status, version,
                        created_by, updated_by, created_at, updated_at
                    ) VALUES (
                        :id, :workspace_id, :provider, 'x', 'x',
                        ARRAY['contents:read'], :creds, :status, 1,
                        :actor_id, :actor_id, :now, :now
                    )
                    """
                ),
                {
                    "id": uuid4(),
                    "workspace_id": workspace_id,
                    "provider": value if column == "provider" else "sandbox",
                    "status": value if column == "status" else "active",
                    "creds": b"x",
                    "actor_id": user_id,
                    "now": now,
                },
            )
    assert isinstance(excinfo.value.orig, CheckViolation)


def test_sync_cursors_and_sync_runs_check_constraints_reject_invalid_values(
    engineering_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    from psycopg.errors import CheckViolation
    from sqlalchemy.exc import IntegrityError

    _client, workspace_id, user_id, _token = engineering_test_context
    account_id = _insert_connector_account(workspace_id, user_id)
    now = datetime.now(UTC)

    with pytest.raises(IntegrityError) as excinfo:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO sync_cursors (id, workspace_id, connector_account_id, "
                    "resource_type, cursor_value, updated_at) VALUES "
                    "(:id, :workspace_id, :account_id, 'not-a-real-resource-type', '1', :now)"
                ),
                {
                    "id": uuid4(),
                    "workspace_id": workspace_id,
                    "account_id": account_id,
                    "now": now,
                },
            )
    assert isinstance(excinfo.value.orig, CheckViolation)

    with pytest.raises(IntegrityError) as excinfo:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO sync_runs (id, workspace_id, connector_account_id, run_type, "
                    "status, items_processed, started_at, created_at) VALUES "
                    "(:id, :workspace_id, :account_id, 'not-a-real-run-type', 'running', "
                    "0, :now, :now)"
                ),
                {
                    "id": uuid4(),
                    "workspace_id": workspace_id,
                    "account_id": account_id,
                    "now": now,
                },
            )
    assert isinstance(excinfo.value.orig, CheckViolation)

    with pytest.raises(IntegrityError) as excinfo:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO sync_runs (id, workspace_id, connector_account_id, run_type, "
                    "status, items_processed, started_at, created_at) VALUES "
                    "(:id, :workspace_id, :account_id, 'backfill', 'not-a-real-status', "
                    "0, :now, :now)"
                ),
                {
                    "id": uuid4(),
                    "workspace_id": workspace_id,
                    "account_id": account_id,
                    "now": now,
                },
            )
    assert isinstance(excinfo.value.orig, CheckViolation)


def test_sync_rejects_invalid_run_type_and_resource_type(
    engineering_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, user_id, token = engineering_test_context
    account_id = _insert_connector_account(workspace_id, user_id)

    webhook_attempt = client.post(
        f"/api/v1/engineering/connectors/{account_id}/sync",
        json={"run_type": "webhook", "resource_type": "repository"},
        headers=_headers(token, key=str(uuid4())),
    )
    assert webhook_attempt.status_code == 422

    bad_resource_type = client.post(
        f"/api/v1/engineering/connectors/{account_id}/sync",
        json={"run_type": "backfill", "resource_type": "not-a-real-resource-type"},
        headers=_headers(token, key=str(uuid4())),
    )
    assert bad_resource_type.status_code == 422

    with engine.begin() as connection:
        run_count = connection.execute(
            text("SELECT count(*) FROM sync_runs WHERE connector_account_id = :id"),
            {"id": account_id},
        ).scalar_one()
        assert run_count == 0


def test_disable_invokes_adapter_disconnect(
    engineering_test_context: tuple[TestClient, UUID, UUID, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, workspace_id, user_id, token = engineering_test_context
    spy = _SpyDisconnectAdapter()
    monkeypatch.setattr(connector_accounts_module, "connector_registry", _registry_with(spy))
    account_id = _insert_connector_account(workspace_id, user_id)

    response = client.post(
        f"/api/v1/engineering/connectors/{account_id}/disable",
        headers=_headers(token, key=str(uuid4())),
    )
    assert response.status_code == 200
    assert response.json()["status"] == "disconnected"
    assert len(spy.disconnect_calls) == 1
    assert spy.disconnect_calls[0].connector_account_id == account_id


def test_disable_succeeds_when_adapter_disconnect_raises(
    engineering_test_context: tuple[TestClient, UUID, UUID, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, workspace_id, user_id, token = engineering_test_context
    raising = _RaisingDisconnectAdapter()
    monkeypatch.setattr(connector_accounts_module, "connector_registry", _registry_with(raising))
    account_id = _insert_connector_account(workspace_id, user_id)

    response = client.post(
        f"/api/v1/engineering/connectors/{account_id}/disable",
        headers=_headers(token, key=str(uuid4())),
    )
    assert response.status_code == 200
    assert response.json()["status"] == "disconnected"
    assert len(raising.disconnect_calls) == 1


def test_disable_succeeds_when_credential_cannot_be_decrypted(
    engineering_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, user_id, token = engineering_test_context
    account_id = _insert_connector_account(
        workspace_id, user_id, encrypted_credentials=b"not-a-valid-fernet-token"
    )

    response = client.post(
        f"/api/v1/engineering/connectors/{account_id}/disable",
        headers=_headers(token, key=str(uuid4())),
    )
    assert response.status_code == 200
    assert response.json()["status"] == "disconnected"


def test_disable_releases_pool_connection_and_row_lock_before_disconnect_call(
    engineering_test_context: tuple[TestClient, UUID, UUID, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 23 review: `disable_connector_endpoint` used to call
    `adapter.disconnect(...)` while still holding both a pooled DB
    connection and the `FOR UPDATE` lock taken on the account row --
    invisible until `GmailAdapter.disconnect()` became the first real,
    blocking outbound call any adapter's `disconnect()` ever made.
    Reproduced here for the provider-agnostic `sandbox` adapter via
    `_SlowDisconnectAdapter`: while a `/disable` request is genuinely
    blocked inside `disconnect()`, a concurrent raw `SELECT ... FOR UPDATE
    NOWAIT` on the same row must succeed immediately -- if the connection/
    lock were still held, it would raise `LockNotAvailable` instead.
    """
    client, workspace_id, user_id, token = engineering_test_context
    slow_adapter = _SlowDisconnectAdapter()
    monkeypatch.setattr(
        connector_accounts_module, "connector_registry", _registry_with(slow_adapter)
    )
    account_id = _insert_connector_account(workspace_id, user_id)

    def _disable() -> Any:
        return client.post(
            f"/api/v1/engineering/connectors/{account_id}/disable",
            headers=_headers(token, key=str(uuid4())),
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
                    {"id": account_id},
                )
                .mappings()
                .one()
            )
            assert row["id"] == account_id
            probe.rollback()
            # `SET` (not `SET LOCAL`) under AUTOCOMMIT is session-scoped, and
            # this physical connection returns to the pool when this `with`
            # block exits -- `probe.rollback()` above only ends the SELECT's
            # own micro-transaction, never a plain session-level `SET`.
            # Explicitly restore the connection's established baseline
            # before checkin so a later test drawing this same pooled
            # connection doesn't inherit the 2s override. (`RESET
            # statement_timeout` does NOT work here: it reverts to
            # Postgres's boot-time/config default -- verified empirically to
            # be `0` in this environment -- not to the value
            # `_set_statement_timeout` committed when the connection was
            # first created.)
            probe.execute(text(f"SET statement_timeout = {STATEMENT_TIMEOUT_MS}"))

        slow_adapter.release.set()
        response = future.result(timeout=5)

    assert response.status_code == 200
    assert response.json()["status"] == "disconnected"


def test_create_connector_same_credential_different_workspaces_both_succeed(
    engineering_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = engineering_test_context
    first = client.post(
        "/api/v1/engineering/connectors",
        json={"provider": "sandbox", "credential": "shared-credential"},
        headers=_headers(token, key=str(uuid4())),
    )
    assert first.status_code == 201

    workspace_b_id = uuid4()
    user_b_id = uuid4()
    token_b = f"session-{uuid4()}"
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, 'Workspace B Shared Credential', 'Asia/Kolkata', :now)"
            ),
            {"id": workspace_b_id, "now": now},
        )
        create_identity(
            connection,
            workspace_id=workspace_b_id,
            user_id=user_b_id,
            email=f"{user_b_id}@example.test",
            now=now,
        )
        connection.execute(
            text(
                "INSERT INTO sessions (id, workspace_id, user_id, token_hash, "
                "expires_at, last_seen_at) "
                "VALUES (:id, :workspace_id, :user_id, :token_hash, :expires_at, :now)"
            ),
            {
                "id": uuid4(),
                "workspace_id": workspace_b_id,
                "user_id": user_b_id,
                "token_hash": sha256(token_b.encode()).hexdigest(),
                "expires_at": now + timedelta(hours=1),
                "now": now,
            },
        )
    try:
        client_b = TestClient(app)
        client_b.cookies.set("ecc_session", token_b)
        second = client_b.post(
            "/api/v1/engineering/connectors",
            json={"provider": "sandbox", "credential": "shared-credential"},
            headers=_headers(token_b, key=str(uuid4())),
        )
        assert second.status_code == 201
        assert second.json()["id"] != first.json()["id"]
        client_b.close()
    finally:
        _cleanup_workspace(workspace_b_id)


def test_list_sync_runs_without_connector_filter(
    engineering_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, user_id, token = engineering_test_context
    account_id = _insert_connector_account(workspace_id, user_id)
    client.post(
        f"/api/v1/engineering/connectors/{account_id}/sync",
        json={"run_type": "backfill", "resource_type": "repository"},
        headers=_headers(token, key=str(uuid4())),
    )

    response = client.get("/api/v1/engineering/sync-runs")
    assert response.status_code == 200
    runs = response.json()["sync_runs"]
    assert len(runs) == 1
    assert runs[0]["connector_account_id"] == str(account_id)


def test_sync_idempotency_replay(
    engineering_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, user_id, token = engineering_test_context
    account_id = _insert_connector_account(workspace_id, user_id)
    key = str(uuid4())
    payload = {"run_type": "backfill", "resource_type": "repository"}

    first = client.post(
        f"/api/v1/engineering/connectors/{account_id}/sync",
        json=payload,
        headers=_headers(token, key=key),
    )
    assert first.status_code == 201

    replay = client.post(
        f"/api/v1/engineering/connectors/{account_id}/sync",
        json=payload,
        headers=_headers(token, key=key),
    )
    assert replay.status_code == 201
    assert replay.json()["id"] == first.json()["id"]

    with engine.begin() as connection:
        run_count = connection.execute(
            text("SELECT count(*) FROM sync_runs WHERE connector_account_id = :id"),
            {"id": account_id},
        ).scalar_one()
        assert run_count == 1


def test_disable_idempotency_replay(
    engineering_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, user_id, token = engineering_test_context
    account_id = _insert_connector_account(workspace_id, user_id)
    key = str(uuid4())

    first = client.post(
        f"/api/v1/engineering/connectors/{account_id}/disable",
        headers=_headers(token, key=key),
    )
    assert first.status_code == 200

    replay = client.post(
        f"/api/v1/engineering/connectors/{account_id}/disable",
        headers=_headers(token, key=key),
    )
    assert replay.status_code == 200
    # `request_id`/`correlation_id` are injected fresh per HTTP request by
    # `ecc.main.response_contract_middleware`, not part of the cached
    # response body itself -- excluded here matching `test_automation_
    # workflows_postgres.py`'s identical replay-comparison precedent.
    ignored = {"request_id", "correlation_id"}
    first_body = {k: v for k, v in first.json().items() if k not in ignored}
    replay_body = {k: v for k, v in replay.json().items() if k not in ignored}
    assert replay_body == first_body


def test_sync_connector_provider_not_supported(
    engineering_test_context: tuple[TestClient, UUID, UUID, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every provider in `ck_connector_accounts_provider`'s closed set
    (`github`/`gitlab`/`jira`/`sandbox`) now has a registered adapter as
    of Task 4 -- there is no longer a real, CHECK-constraint-valid
    provider value this test can use to reach `CONNECTOR_PROVIDER_NOT_
    SUPPORTED` the way Tasks 1-3 each did in turn (each real adapter this
    phase added retired the previous task's own "use this still-
    unregistered provider" fixture). Monkeypatching in an empty registry
    exercises the identical code path (`connector_registry.get(...)
    returns None`) without depending on a real provider slug staying
    unimplemented, which is no longer true for any of them.
    """
    client, workspace_id, user_id, token = engineering_test_context
    monkeypatch.setattr(connector_accounts_module, "connector_registry", ConnectorRegistry())
    account_id = _insert_connector_account(workspace_id, user_id, provider="sandbox")

    response = client.post(
        f"/api/v1/engineering/connectors/{account_id}/sync",
        json={"run_type": "backfill", "resource_type": "repository"},
        headers=_headers(token, key=str(uuid4())),
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "CONNECTOR_PROVIDER_NOT_SUPPORTED"


# --- third review batch: pool-exhaustion-fix review-fix (migration 0046) ---
# `sync_connector_endpoint`'s restructuring into phases across two pooled
# connections (Task 2) initially reintroduced three races since closing the
# pooled connection between phases also released the guarantees a single
# held transaction used to provide across the whole handler -- see
# `connector_accounts.py`'s module docstring for the full mechanism these
# tests exercise.


def test_sync_conflict_when_another_run_still_in_progress(
    engineering_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    """`uq_sync_runs_running_per_account` (migration 0046) makes starting a
    sync itself atomic -- a second `/sync` call for the same account while
    another `sync_runs` row is still `running` (simulated here by inserting
    one directly, standing in for a real concurrent request still in its
    slow adapter-call phase) gets `409 CONNECTOR_SYNC_IN_PROGRESS` rather
    than racing the cursor UPSERT or double-executing the adapter call.
    """
    client, workspace_id, _user_id, token = engineering_test_context
    created = client.post(
        "/api/v1/engineering/connectors",
        json={"provider": "sandbox", "credential": "token-in-progress"},
        headers=_headers(token, key=str(uuid4())),
    ).json()
    account_id = UUID(created["id"])
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO sync_runs (
                    id, workspace_id, connector_account_id, run_type, status,
                    items_processed, started_at, created_at
                ) VALUES (:id, :workspace_id, :account_id, 'backfill', 'running', 0, :now, :now)
                """
            ),
            {"id": uuid4(), "workspace_id": workspace_id, "account_id": account_id, "now": now},
        )

    response = client.post(
        f"/api/v1/engineering/connectors/{account_id}/sync",
        json={"run_type": "backfill", "resource_type": "repository"},
        headers=_headers(token, key=str(uuid4())),
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "CONNECTOR_SYNC_IN_PROGRESS"

    with engine.begin() as connection:
        run_count = connection.execute(
            text("SELECT count(*) FROM sync_runs WHERE connector_account_id = :account_id"),
            {"account_id": account_id},
        ).scalar_one()
        # Only the one directly-inserted `running` row -- the conflicting
        # request never got as far as inserting its own.
        assert run_count == 1


def test_sync_audit_version_matches_actual_account_version_not_stale_snapshot(
    engineering_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    """Phase 3 previously wrote `account.version + 1` (phase 1's in-memory
    snapshot) into the audit trail's `aggregate_version` instead of the
    database's real post-update value -- `_finalize_account_version` fixes
    this via `UPDATE ... RETURNING version`. Two syncs in a row prove the
    audit trail tracks the account's real version at each step (2, then 3),
    not a value that could silently diverge once other writes land on the
    account between phase 1 and phase 3.
    """
    client, workspace_id, _user_id, token = engineering_test_context
    created = client.post(
        "/api/v1/engineering/connectors",
        json={"provider": "sandbox", "credential": "token-version"},
        headers=_headers(token, key=str(uuid4())),
    ).json()
    account_id = UUID(created["id"])
    assert created["version"] == 1

    for expected_version in (2, 3):
        sync = client.post(
            f"/api/v1/engineering/connectors/{account_id}/sync",
            json={"run_type": "backfill", "resource_type": "repository"},
            headers=_headers(token, key=str(uuid4())),
        )
        assert sync.status_code == 201, sync.text

        with engine.begin() as connection:
            db_version = connection.execute(
                text("SELECT version FROM connector_accounts WHERE id = :id"),
                {"id": account_id},
            ).scalar_one()
            audit_version = connection.execute(
                text(
                    "SELECT aggregate_version FROM audit_events WHERE aggregate_id = :id "
                    "AND event_type = 'connector_account.synced' ORDER BY occurred_at DESC LIMIT 1"
                ),
                {"id": account_id},
            ).scalar_one()
        assert db_version == expected_version
        assert audit_version == expected_version


def test_sync_failure_audit_version_matches_actual_account_version(
    engineering_test_context: tuple[TestClient, UUID, UUID, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same fix as the success path above, exercised on the failure branch
    (`connector_account.sync_failed`), which computed its own separate
    stale `account.version + 1` before this fix.
    """
    client, workspace_id, user_id, token = engineering_test_context
    monkeypatch.setattr(
        connector_accounts_module, "connector_registry", _registry_with(_RaisingAdapter())
    )
    account_id = _insert_connector_account(workspace_id, user_id)

    response = client.post(
        f"/api/v1/engineering/connectors/{account_id}/sync",
        json={"run_type": "backfill", "resource_type": "repository"},
        headers=_headers(token, key=str(uuid4())),
    )
    assert response.status_code == 201, response.text

    with engine.begin() as connection:
        db_version = connection.execute(
            text("SELECT version FROM connector_accounts WHERE id = :id"), {"id": account_id}
        ).scalar_one()
        audit_version = connection.execute(
            text(
                "SELECT aggregate_version FROM audit_events WHERE aggregate_id = :id "
                "AND event_type = 'connector_account.sync_failed'"
            ),
            {"id": account_id},
        ).scalar_one()
    assert db_version == 2
    assert audit_version == 2


def test_finalize_account_version_guard_skips_disconnected_account(
    engineering_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    """Direct unit-level test of `_finalize_account_version`'s `AND status
    != 'disconnected'` guard -- the scenario it protects (a concurrent
    `/disable` committing between phase 1 and phase 3 of `/sync`) requires
    real thread-level concurrency to reproduce end-to-end through the HTTP
    API; this proves the guard mechanism itself directly, mirroring this
    file's existing precedent of testing internal helpers when a race
    can't be reproduced synchronously (see `_RaisingAdapter` et al. above).
    """
    client, workspace_id, user_id, token = engineering_test_context
    account_id = _insert_connector_account(workspace_id, user_id, status="disconnected")
    now = datetime.now(UTC)

    from ecc.database import SessionFactory

    with SessionFactory() as session, session.begin():
        version = connector_accounts_module._finalize_account_version(
            session,
            account_id,
            update_sql=(
                "UPDATE connector_accounts SET last_synced_at = :now, last_error = :error, "
                "updated_at = :now, updated_by = :actor_id, version = version + 1 "
                "WHERE id = :id AND status != 'disconnected' RETURNING version"
            ),
            params={"now": now, "error": None, "actor_id": user_id, "id": account_id},
        )
    # Real current version (1), not incremented -- the guard skipped the
    # UPDATE because the account is disconnected, and _finalize_account_
    # version fell back to a plain SELECT rather than returning a
    # would-be-incremented value that was never actually written.
    assert version == 1

    with engine.begin() as connection:
        row = (
            connection.execute(
                text("SELECT version, status FROM connector_accounts WHERE id = :id"),
                {"id": account_id},
            )
            .mappings()
            .one()
        )
    assert row["version"] == 1
    assert row["status"] == "disconnected"


def test_two_concurrent_syncs_for_same_account_second_gets_conflict(
    engineering_test_context: tuple[TestClient, UUID, UUID, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real concurrent HTTP requests against real, separate database
    connections -- mirrors `test_automation_scheduler_postgres.py`'s
    `test_two_concurrent_ticks_racing_the_same_due_trigger_fire_exactly_
    once` precedent for the identical class of property. Thread A's
    `/sync` call is held genuinely mid-phase-2 (via `_SlowAdapter` blocking
    on an `Event`, with no pooled connection held) until thread B's own
    concurrent `/sync` call has been issued and its response observed --
    proving `uq_sync_runs_running_per_account` (migration 0046) serializes
    two real competing requests, not just a synthetic pre-seeded row.
    """
    client, workspace_id, _user_id, token = engineering_test_context
    slow_adapter = _SlowAdapter()
    monkeypatch.setattr(
        connector_accounts_module, "connector_registry", _registry_with(slow_adapter)
    )
    created = client.post(
        "/api/v1/engineering/connectors",
        json={"provider": "sandbox", "credential": "token-concurrent"},
        headers=_headers(token, key=str(uuid4())),
    ).json()
    account_id = created["id"]

    def _sync(sync_client: TestClient) -> Any:
        return sync_client.post(
            f"/api/v1/engineering/connectors/{account_id}/sync",
            json={"run_type": "backfill", "resource_type": "repository"},
            headers=_headers(token, key=str(uuid4())),
        )

    client_a = TestClient(app)
    client_a.cookies.set("ecc_session", token)
    client_b = TestClient(app)
    client_b.cookies.set("ecc_session", token)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            future_a = pool.submit(_sync, client_a)
            assert slow_adapter.entered_phase2.wait(timeout=5), "thread A never reached phase 2"
            future_b = pool.submit(_sync, client_b)
            response_b = future_b.result(timeout=5)
            slow_adapter.release.set()
            response_a = future_a.result(timeout=5)
    finally:
        client_a.close()
        client_b.close()

    assert response_a.status_code == 201, response_a.text
    assert response_a.json()["status"] == "succeeded"
    assert response_b.status_code == 409, response_b.text
    assert response_b.json()["error"]["code"] == "CONNECTOR_SYNC_IN_PROGRESS"

    with engine.begin() as connection:
        run_count = connection.execute(
            text("SELECT count(*) FROM sync_runs WHERE connector_account_id = :account_id"),
            {"account_id": UUID(account_id)},
        ).scalar_one()
    assert run_count == 1


def test_stale_running_sync_run_is_reaped_not_permanently_stuck(
    engineering_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    """Security review found the pool-exhaustion restructuring's own fix
    (making a `running` row the serialization point) has a failure mode a
    single spanning transaction never had: a process crash/kill between
    phase 1's commit and phase 3 leaves the row `running` forever, wedging
    every future `/sync` call behind it (`409 CONNECTOR_SYNC_IN_PROGRESS`)
    with no recovery path. Phase 1 reaps any `running` row older than
    `_STALE_RUNNING_SYNC_THRESHOLD` before its own `INSERT` -- simulated
    here via a directly-inserted `running` row backdated well past that
    threshold, standing in for a request that crashed long ago.
    """
    client, workspace_id, _user_id, token = engineering_test_context
    created = client.post(
        "/api/v1/engineering/connectors",
        json={"provider": "sandbox", "credential": "token-stale"},
        headers=_headers(token, key=str(uuid4())),
    ).json()
    account_id = UUID(created["id"])
    stale_started_at = datetime.now(UTC) - timedelta(hours=1)
    stale_run_id = uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO sync_runs (
                    id, workspace_id, connector_account_id, run_type, status,
                    items_processed, started_at, created_at
                ) VALUES (:id, :workspace_id, :account_id, 'backfill', 'running', 0, :now, :now)
                """
            ),
            {
                "id": stale_run_id,
                "workspace_id": workspace_id,
                "account_id": account_id,
                "now": stale_started_at,
            },
        )

    response = client.post(
        f"/api/v1/engineering/connectors/{account_id}/sync",
        json={"run_type": "backfill", "resource_type": "repository"},
        headers=_headers(token, key=str(uuid4())),
    )
    assert response.status_code == 201, response.text
    assert response.json()["status"] == "succeeded"

    with engine.begin() as connection:
        stale_row = (
            connection.execute(
                text("SELECT status FROM sync_runs WHERE id = :id"), {"id": stale_run_id}
            )
            .mappings()
            .one()
        )
    assert stale_row["status"] == "failed"


def test_recent_running_sync_run_is_not_reaped(
    engineering_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    """The reap in the test above must never touch a genuinely still-in-
    flight sync -- a `running` row within `_STALE_RUNNING_SYNC_THRESHOLD`
    still correctly blocks a new `/sync` call with `409
    CONNECTOR_SYNC_IN_PROGRESS`, exactly as `test_sync_conflict_when_
    another_run_still_in_progress` above already proves for a fresh row;
    this proves the reap's age boundary itself, not just its absence.
    """
    client, workspace_id, _user_id, token = engineering_test_context
    created = client.post(
        "/api/v1/engineering/connectors",
        json={"provider": "sandbox", "credential": "token-recent"},
        headers=_headers(token, key=str(uuid4())),
    ).json()
    account_id = UUID(created["id"])
    recent_started_at = datetime.now(UTC) - timedelta(minutes=1)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO sync_runs (
                    id, workspace_id, connector_account_id, run_type, status,
                    items_processed, started_at, created_at
                ) VALUES (:id, :workspace_id, :account_id, 'backfill', 'running', 0, :now, :now)
                """
            ),
            {
                "id": uuid4(),
                "workspace_id": workspace_id,
                "account_id": account_id,
                "now": recent_started_at,
            },
        )

    response = client.post(
        f"/api/v1/engineering/connectors/{account_id}/sync",
        json={"run_type": "backfill", "resource_type": "repository"},
        headers=_headers(token, key=str(uuid4())),
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "CONNECTOR_SYNC_IN_PROGRESS"


def test_create_connector_idempotent_retry_racing_phase_three_replays_response(
    engineering_test_context: tuple[TestClient, UUID, UUID, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Correctness and API-contract review both found `create_connector_
    endpoint`'s phase split could turn a genuine same-Idempotency-Key retry
    (racing ahead of the first call's own phase 3, e.g. after a client-side
    timeout during a slow `authorize()`) into `409
    CONNECTOR_ALREADY_CONNECTED` instead of the replayed response the
    `Idempotency-Key` contract promises. Real concurrent requests, same
    mechanism as the `/sync` race test above, but synchronized via
    `_SlowAuthorizeAdapter`'s two-party `Barrier` rather than an `Event`
    one thread sets and the other waits on -- see that fake's own
    docstring for why an `Event`-based ordering here would let this test
    pass without ever reaching the code path it claims to exercise
    (thread B resolving via phase 1's own pre-existing cache check instead
    of phase 3's `IntegrityError`/cache-recheck).
    """
    client, _workspace_id, _user_id, token = engineering_test_context
    slow_adapter = _SlowAuthorizeAdapter()
    monkeypatch.setattr(
        connector_accounts_module, "connector_registry", _registry_with(slow_adapter)
    )
    idempotency_key = str(uuid4())

    def _create(create_client: TestClient) -> Any:
        return create_client.post(
            "/api/v1/engineering/connectors",
            json={"provider": "sandbox", "credential": "token-idempotent-race"},
            headers=_headers(token, key=idempotency_key),
        )

    client_a = TestClient(app)
    client_a.cookies.set("ecc_session", token)
    client_b = TestClient(app)
    client_b.cookies.set("ecc_session", token)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            future_a = pool.submit(_create, client_a)
            future_b = pool.submit(_create, client_b)
            response_a = future_a.result(timeout=10)
            response_b = future_b.result(timeout=10)
    finally:
        client_a.close()
        client_b.close()

    assert response_a.status_code == 201, response_a.text
    assert response_b.status_code == 201, response_b.text
    ignored = {"request_id", "correlation_id"}
    body_a = {k: v for k, v in response_a.json().items() if k not in ignored}
    body_b = {k: v for k, v in response_b.json().items() if k not in ignored}
    assert body_a == body_b

    with engine.begin() as connection:
        account_count = connection.execute(
            text(
                "SELECT count(*) FROM connector_accounts WHERE workspace_id = :workspace_id "
                "AND provider = 'sandbox' AND external_account_id = 'slow-authorize-account'"
            ),
            {"workspace_id": _workspace_id},
        ).scalar_one()
    assert account_count == 1
