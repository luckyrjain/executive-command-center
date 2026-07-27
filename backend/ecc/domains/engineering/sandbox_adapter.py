"""`sandbox.github` -- one deliberately-fake, in-memory `ConnectorAdapter`
(design doc Decision 1's Task 1 scope, mirroring `ecc.domains.automation.
local_adapters.FakeExternalActionAdapter`'s identical precedent: "a
deliberately, visibly fake adapter ... used only in tests ... to exercise
the full external-connector shape ... without a real network call or real
external system").

Registered under `provider = "sandbox"` (the `ck_connector_accounts_
provider` CHECK constraint's closed set includes it specifically for this
purpose) -- never a value a production GitHub/GitLab/Jira connection would
use, so it can never be mistaken for a real connector account in any
listing a real deployment might show.

Every method below is pure/deterministic given its input and touches no
network, no filesystem, and no table -- `connector_accounts.py`'s own
router is the only thing that persists state (`sync_cursors`/`sync_runs`
rows), exactly as `worker.py` is `ActionAdapter.execute`'s only caller in
Phase 5's identical precedent.
"""

from __future__ import annotations

from hashlib import sha256

from .connectors import (
    AdapterAuthorizationError,
    ConnectorAccountContext,
    ConnectorAuthorization,
    PermissionState,
    SyncOutcome,
)

_REQUIRED_SCOPES: frozenset[str] = frozenset({"contents:read", "metadata:read"})
_ITEMS_PER_BACKFILL = 3


def _fake_external_account_id(credential: str) -> str:
    """Deterministic given `credential` -- so re-authorizing with the same
    fake credential always resolves to the same account identity (letting
    `uq_connector_accounts_workspace_provider_external_id` reject a genuine
    duplicate connection the same way a real provider's stable account id
    would), while a different credential always resolves to a different one.
    """
    return f"sandbox-{sha256(credential.encode()).hexdigest()[:12]}"


class SandboxGithubAdapter:
    """`required_scopes` mirrors the read-only scope set design doc
    Decision 2 names for the real GitHub connector (`contents:read`,
    `metadata:read`), so this fake exercises the same granted-scopes shape
    a real GitHub adapter will report.
    """

    provider = "sandbox"
    required_scopes: frozenset[str] = _REQUIRED_SCOPES

    def authorize(self, credential: str) -> ConnectorAuthorization:
        """Rejects an empty credential or one containing the literal
        substring `"invalid"` -- the one deliberately-triggerable failure
        mode this fake exposes, so a test can exercise `AdapterAuthorization
        Error` handling without a real provider to reject a real token.
        """
        if not credential or "invalid" in credential:
            raise AdapterAuthorizationError("sandbox credential rejected")
        return ConnectorAuthorization(
            external_account_id=_fake_external_account_id(credential),
            display_name=f"Sandbox GitHub ({_fake_external_account_id(credential)})",
            granted_scopes=_REQUIRED_SCOPES,
        )

    def backfill(self, account: ConnectorAccountContext, resource_type: str) -> SyncOutcome:
        return SyncOutcome(
            resource_type=resource_type,
            items_processed=_ITEMS_PER_BACKFILL,
            status="succeeded",
            next_cursor="1",
        )

    def incremental_sync(
        self, account: ConnectorAccountContext, resource_type: str, cursor: str | None
    ) -> SyncOutcome:
        """No `cursor` behaves like a fresh backfill (`ConnectorAdapter.
        incremental_sync`'s own contract). Otherwise advances by exactly
        one fake item per call, so a test can assert cursor progression
        deterministically (`"1"` -> `"2"` -> `"3"`, ...).
        """
        if cursor is None:
            return self.backfill(account, resource_type)
        try:
            next_value = int(cursor) + 1
        except ValueError:
            next_value = 1
        return SyncOutcome(
            resource_type=resource_type,
            items_processed=1,
            status="succeeded",
            next_cursor=str(next_value),
        )

    def handle_webhook(
        self, account: ConnectorAccountContext, payload: bytes, headers: object
    ) -> SyncOutcome:
        return SyncOutcome(
            resource_type="repository",
            items_processed=1 if payload else 0,
            status="succeeded",
            next_cursor=None,
        )

    def refresh_permissions(self, account: ConnectorAccountContext) -> PermissionState:
        """Returns `permission_lost` when the account's own credential
        (re-supplied by the caller at refresh time, per the same shape
        `authorize` receives) contains the literal substring
        `"lose-access"` -- the one deliberately-triggerable state
        transition this fake exposes for testing the permission-loss path
        without a real provider to actually revoke access.
        """
        if "lose-access" in account.credential:
            return "permission_lost"
        return "active"

    def disconnect(self, account: ConnectorAccountContext) -> None:
        """No-op -- this fake has no provider-side session to revoke."""
        return None
