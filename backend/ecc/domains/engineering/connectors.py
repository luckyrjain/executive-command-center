"""Connector-independent adapter contract and in-process registry (design
doc Decision 1: `docs/superpowers/specs/2026-07-27-phase-6-engineering-
workspace-design.md`, `docs/phases/phase-006/CONNECTOR-CONTRACT.md`).

Mirrors `ecc.domains.automation.adapters.ActionAdapter`/`AdapterRegistry`'s
shape deliberately -- structural typing (`typing.Protocol`, not an ABC),
`@runtime_checkable` so `ConnectorRegistry.register` can `isinstance()`
-check a candidate at registration time, and one shared production
`registry` instance a later real GitHub/GitLab/Jira adapter registers into
alongside this task's own `sandbox.github` (`sandbox_adapter.py`). This is
a **second, distinct registry**, not a reuse of Phase 5's -- a connector
owns read sync and account lifecycle; a Phase 5 `ActionAdapter` owns one
write action. Phase 6's own write actions (a later task) are registered
into *that* registry, per `docs/phases/PHASE-REVIEW.md` finding F-03 --
this module has nothing to do with writes.

`CONNECTOR-CONTRACT.md`'s seven lifecycle operations -- authorize,
validate, backfill, incremental sync, webhook ingestion where available,
permission refresh, disconnect -- map onto this Protocol's methods below.
"Validate" is folded into `authorize`'s own return value (an adapter that
cannot validate a credential simply raises during `authorize`, rather than
a separate round-trip) since no adapter in this task's scope has a
distinct post-authorization revalidation step from a fresh credential.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol, runtime_checkable
from uuid import UUID

PermissionState = Literal["active", "permission_lost", "deleted"]
SyncStatus = Literal["succeeded", "failed", "partial"]

# Phase 8 Task 3 (`authz.py`) added a NOT NULL `owner_id` to every synced
# projection table (`repositories`/`changes`/`reviews`/`engineering_work_
# items`/`datadog_*`/`delivery_metric_snapshots`) -- rows an adapter
# upserts from `ConnectorAccountContext` alone, with no acting user in
# scope (unlike `connector_accounts`/`sync_runs`/`sync_cursors`, created
# directly inside a request handler that has `auth.user_id`). Migration
# `0063_phase8_authz_visibility.py` backfilled every pre-existing row in
# these same tables to "the workspace's oldest-created user" for the
# identical reason; new rows an adapter inserts reuse that exact subquery
# so ownership stays consistent with the backfill rather than inventing a
# second rule. `visibility='workspace'` (this module's INSERTs always pair
# this with the literal) makes the choice low-stakes in practice -- every
# active workspace member can already read/write these rows via role
# permissions regardless of which user happens to hold `owner_id`.
#
# Byte-identical to `ecc.platform.authz.WORKSPACE_ORIGINAL_OWNER_SQL` (a
# second copy, not a shared import) -- this module is deliberately a leaf
# with no `ecc.*` imports (see its own module docstring); pulling in
# `authz.py`'s FastAPI/session dependency chain for one string constant
# was judged worse than the duplication. Both trace back to the identical
# migration formula, so there is no divergence risk.
WORKSPACE_ORIGINAL_OWNER_SQL = (
    "(SELECT u.id FROM users AS u WHERE u.workspace_id = :workspace_id "
    "ORDER BY u.created_at ASC LIMIT 1)"
)


@dataclass(frozen=True, slots=True)
class ConnectorAuthorization:
    """What `ConnectorAdapter.authorize` returns on success -- never the
    credential itself (the caller already holds it). `external_account_id`
    and `display_name` populate `connector_accounts` directly;
    `granted_scopes` is what `GET /engineering/connectors` shows an
    operator, per design doc Decision 2 ("an operator can see exactly what
    a connection can do").
    """

    external_account_id: str
    display_name: str
    granted_scopes: frozenset[str]
    # Phase 10 Gmail Connector Task 1 (design doc Decision 1): populated
    # only by `OAuth2ConnectorAdapter.handle_oauth_callback` -- the OAuth
    # grant (access/refresh token, adapter-serialized) it just exchanged a
    # code for, which no caller has seen before this call returns. Every
    # PAT-based `ConnectorAdapter.authorize()` leaves this `None`; the
    # caller there already holds the original credential string it passed
    # in, so there is nothing new for `authorize` to hand back.
    credential: str | None = None


@dataclass(frozen=True, slots=True)
class ConnectorAccountContext:
    """Passed to every post-authorization adapter method. `credential` is
    the already-decrypted plaintext (`ecc.domains.engineering.crypto.
    decrypt_credential`'s output) -- held only for the duration of one
    call, never persisted by this module.
    """

    workspace_id: UUID
    connector_account_id: UUID
    external_account_id: str
    credential: str


@dataclass(frozen=True, slots=True)
class SyncOutcome:
    """Result of a backfill/incremental/webhook sync call.
    `items_processed` and `status` are written to `sync_runs`;
    `next_cursor` (when not `None`) is written to `sync_cursors` for the
    given `resource_type`, resuming from that position on the next call --
    `CONNECTOR-CONTRACT.md`: "Sync is incremental, idempotent, resumable."
    """

    resource_type: str
    items_processed: int
    status: SyncStatus
    next_cursor: str | None
    error_summary: str | None = None


class AdapterAuthorizationError(Exception):
    """Raised by `ConnectorAdapter.authorize` when the supplied credential
    is invalid, expired, or lacks a scope this adapter requires -- a
    caller-facing rejection, not a transient/retryable failure.
    """


@runtime_checkable
class ConnectorAdapter(Protocol):
    """Structural contract every registered connector adapter satisfies.
    `provider` is the stable slug stored in `connector_accounts.provider`
    (the `ck_connector_accounts_provider` CHECK constraint's closed set:
    `github`, `gitlab`, `jira`, `sandbox`, `datadog`, `gmail`) and doubles
    as this registry's lookup key.
    """

    provider: str
    required_scopes: frozenset[str]

    def authorize(self, credential: str) -> ConnectorAuthorization:
        """Validate `credential` against the provider (or, for a sandbox
        adapter, an in-memory fake) and return the account identity/scopes
        it resolves to. Raises `AdapterAuthorizationError` on rejection --
        never partially registers an account.

        Every adapter through Phase 6 is PAT-based: `credential` is an
        already-obtained token string, pasted by the caller. An adapter
        needing true OAuth2 (no such thing as a personal access token for
        its provider) additionally implements `OAuth2ConnectorAdapter`
        below *instead of* relying on this method -- `authorize` itself is
        then simply never called for that adapter's provider.
        """
        ...

    def backfill(
        self,
        account: ConnectorAccountContext,
        resource_type: str,
        since: datetime | None = None,
    ) -> SyncOutcome:
        """Full historical sync for one resource type, from no prior
        cursor. `CONNECTOR-CONTRACT.md`: "Backfill resumes without
        duplicate projections" -- an adapter implementing real pagination
        must itself be resumable if interrupted mid-backfill; this task's
        sandbox adapter is small enough to complete in one call.

        `since` (Phase 10 Gmail Connector Task 1, design doc Decision 1):
        an optional lower bound on how far back this call should sync.
        Every adapter through Phase 6 accepts and ignores this parameter
        (full backfill regardless, matching their existing one-shot-at-
        connect-time behavior -- `isinstance`-based Protocol conformance
        does not check method signatures, only attribute presence, so
        widening this signature does not itself require touching any
        existing adapter) -- `GmailAdapter` is the first to act on it,
        re-invoked with a narrower or wider window on an explicit "expand
        history" request rather than only once at connect time.
        """
        ...

    def incremental_sync(
        self, account: ConnectorAccountContext, resource_type: str, cursor: str | None
    ) -> SyncOutcome:
        """Resumes from `cursor` (the `sync_cursors.cursor_value` most
        recently persisted for this account/resource_type), or behaves
        like a fresh backfill if `cursor` is `None`.
        """
        ...

    def handle_webhook(
        self, account: ConnectorAccountContext, payload: bytes, headers: Mapping[str, str]
    ) -> SyncOutcome:
        """Provider push notification ingestion. `CONNECTOR-CONTRACT.md`:
        "deduplicates webhook/poll overlap" -- an adapter's own
        implementation is responsible for that dedupe (e.g. against the
        same `content_hash`/cursor an incremental poll would also observe),
        not this module.
        """
        ...

    def refresh_permissions(self, account: ConnectorAccountContext) -> PermissionState:
        """Re-checks whether the credential still has access (distinct from
        whether it is merely *valid* -- a token can remain valid while
        losing repository/project access). `CONNECTOR-CONTRACT.md`:
        "Provider deletion, access loss and rename are distinct states."
        """
        ...

    def disconnect(self, account: ConnectorAccountContext) -> None:
        """Best-effort provider-side credential revocation.
        `CONNECTOR-CONTRACT.md`: "Disconnect revokes credentials when
        possible and stops future sync" -- stopping future sync is this
        module's caller's responsibility (`connector_accounts.status`
        transition to `disconnected`), not this method's; this method only
        performs the provider-side revocation call, and must not raise for
        "provider does not support revocation" (that is an expected,
        non-error outcome for some providers/PAT-based auth, only a real
        revocation-attempt failure should raise).
        """
        ...


@runtime_checkable
class OAuth2ConnectorAdapter(Protocol):
    """Phase 10 Gmail Connector Task 1 (design doc Decision 1): the first
    true OAuth2-authorization-code-grant shape any adapter in this
    registry has used. A **separate** Protocol from `ConnectorAdapter`
    above, not two more required methods added to it -- `@runtime_
    checkable` Protocol `isinstance()` checks require every declared
    member to be *present* on the concrete object (verified directly:
    `isinstance` returns `False` for an object missing even one declared
    method, regardless of whether that method has a real body in the
    Protocol itself). Adding these two methods to `ConnectorAdapter`
    directly would have broken `ConnectorRegistry.register`'s own
    `isinstance` check for every existing adapter (`github`/`gitlab`/
    `jira`/`sandbox`/`datadog`), none of which define them. An adapter
    needing this flow (only `GmailAdapter` in this activation) implements
    both `ConnectorAdapter` and this Protocol on the same class --
    structural typing means no explicit multiple inheritance is needed,
    only both method sets actually existing on the object.

    `gmail_oauth.py`, not `connector_accounts.py`, is the one caller of
    this pair -- `POST /api/v1/engineering/connectors` (PAT-only) has no
    reason to know this Protocol exists.
    """

    def get_authorization_url(self, state: str) -> str:
        """Returns the provider's own consent-screen URL the caller
        redirects a browser to; `state` is an opaque, caller-generated
        anti-CSRF token this adapter must embed in the URL unchanged and
        `handle_oauth_callback` must verify unchanged.

        Design doc Decision 3's internal-allowlist rejection ("before any
        redirect URL is even generated") does not necessarily happen
        *inside* this method -- this method's own fixed signature carries
        no account/email argument to check an allowlist against (the
        provider account is not known pre-redirect at all for a 3-legged
        OAuth2 flow). `GmailAdapter`, this Protocol's sole implementer,
        performs that check in its caller (`gmail_oauth.py`'s router, via
        the Gmail-specific `is_account_allowed` method) immediately before
        calling this method -- see that adapter's own module docstring for
        why. This method itself may still raise `AdapterAuthorizationError`
        for its own reason (e.g. an unconfigured OAuth client).
        """
        ...

    def handle_oauth_callback(self, code: str, state: str) -> ConnectorAuthorization:
        """Exchanges `code` (the provider's own authorization code,
        returned to the caller's redirect URI) for the account identity/
        scopes this connection resolves to, mirroring `ConnectorAdapter.
        authorize`'s own return shape exactly. Raises `AdapterAuthorization
        Error` on a rejected/expired code.

        `state` is accepted for symmetry with `get_authorization_url` and
        so an implementer *could* re-verify it here, but is not required
        to: the caller must never persist a connector account from a
        callback whose `state` it cannot verify, and nothing prevents that
        verification from happening entirely on the caller's own side
        (`GmailAdapter`, this Protocol's sole implementer, does exactly
        that -- see its own module docstring for why a per-request adapter
        instance has no way to independently re-derive what a *separate*,
        earlier `get_authorization_url` call issued). Whichever side
        verifies it, the caller must never persist a connector account
        without that verification having happened first.
        """
        ...


class AdapterAlreadyRegistered(ValueError):
    """Raised by `ConnectorRegistry.register` when `provider` is already
    taken -- a registration-time programming error, not a runtime
    condition any caller input can trigger.
    """


class ConnectorRegistry:
    """A small in-process `dict[str, ConnectorAdapter]`-backed registry
    `ecc.domains.engineering.connector_accounts` resolves a connector
    account's `provider` against. Deliberately an instance, not a single
    mutate-and-restore global -- mirrors `ecc.domains.automation.adapters.
    AdapterRegistry`'s identical reasoning: each test builds its own
    registry with whatever fake adapter it needs, and the shared
    production `registry` below is one particular instance real product
    code registers into.
    """

    def __init__(self) -> None:
        self._by_provider: dict[str, ConnectorAdapter] = {}

    def register(self, adapter: ConnectorAdapter) -> None:
        if not isinstance(adapter, ConnectorAdapter):
            raise TypeError(
                f"object registered for provider={adapter.provider!r} does not satisfy "
                "the ConnectorAdapter protocol (missing a required attribute or method)"
            )
        if adapter.provider in self._by_provider:
            raise AdapterAlreadyRegistered(f"provider '{adapter.provider}' is already registered")
        self._by_provider[adapter.provider] = adapter

    def get(self, provider: str) -> ConnectorAdapter | None:
        return self._by_provider.get(provider)

    def providers(self) -> tuple[str, ...]:
        return tuple(sorted(self._by_provider))

    def __contains__(self, provider: object) -> bool:
        return provider in self._by_provider

    def __len__(self) -> int:
        return len(self._by_provider)


# Shared production registry. Task 1 registered only `sandbox.github` (see
# `sandbox_adapter.py`) -- no real adapter existed yet. Task 2 added the
# real `github` adapter (`github_adapter.py`); Task 3 added `gitlab`
# (`gitlab_adapter.py`); Task 4 added `jira` (`jira_adapter.py`) -- work
# items only, since Jira is not a source-control provider. A later
# follow-up task adds `datadog` (`datadog_adapter.py`) -- monitors, service
# definitions and dashboards, against the identical contract. `sandbox`
# remains registered for tests/simulation, matching Phase 5's identical
# precedent of keeping its own fake adapters registered alongside real
# ones.
from .datadog_adapter import DatadogAdapter  # noqa: E402
from .github_adapter import GitHubAdapter  # noqa: E402
from .gitlab_adapter import GitLabAdapter  # noqa: E402
from .jira_adapter import JiraAdapter  # noqa: E402
from .sandbox_adapter import SandboxGithubAdapter  # noqa: E402

registry = ConnectorRegistry()
registry.register(SandboxGithubAdapter())
registry.register(GitHubAdapter())
registry.register(GitLabAdapter())
registry.register(JiraAdapter())
registry.register(DatadogAdapter())
