---
id: PHASE-006-CONNECTOR
title: Engineering Connector Contract
status: Approved for Implementation
version: 0.5.0
owner: Lucky Jain
---

# Engineering Connector Contract

Contracts moved from Draft after resolving `docs/phases/PHASE-REVIEW.md:137`'s "provider scopes and retention" and "connector release set" approval-gate items in `docs/superpowers/specs/2026-07-27-phase-6-engineering-workspace-design.md` (Decisions 1-2). This document is the normative statement of those resolutions; the design doc records the reasoning behind them.

Connectors implement authorize, backfill, incremental sync, webhook ingestion where available, permission refresh and disconnect. Tokens use least privilege and encrypted secret storage. Sync persists cursor only after durable projection, deduplicates webhook/poll overlap and handles rate limits with bounded backoff.

Provider deletion, access loss and rename are distinct states. Disconnect revokes credentials when possible and stops future sync; locally retained records follow configured retention. Connector payloads are untrusted and cannot issue runtime instructions.

## Connector release set (resolved)

GitHub ships first, as the reference adapter against the `ConnectorAdapter` contract below; GitLab and Jira are explicitly sequenced next against the identical contract, not descoped -- `docs/superpowers/plans/2026-07-27-phase-6-engineering-workspace.md` names the task order (Task 2 GitHub, Task 3 GitLab, Task 4 Jira). A provider that cannot pass this contract by its scheduled task is explicitly descoped at that point in `docs/phases/phase-006/IMPLEMENTATION-STATUS.md`, not silently dropped.

## Provider scopes (resolved)

Least privilege, read-only by default; a write scope is requested only when a specific Phase 5-gated write action needs it, never bundled into the default read connection.

| Provider | Read scopes | Write scopes (write actions only) |
|---|---|---|
| GitHub | `repo` (classic OAuth token/PAT scope; see Task 2 status below for the fine-grained-PAT limitation) | `repo` (already read+write; no separate write scope to request) |
| GitLab | `read_api`, `read_repository` | `api` |
| Jira | `read:jira-work` | `write:jira-work` |

**GitHub's scope vocabulary correction (Task 2).** The row above originally
named GitHub App/fine-grained-PAT permission identifiers (`contents:read`,
`metadata:read`, `pull_requests:read`, `issues:read`) as if they were the
provider's OAuth scopes. They are not: `github_adapter.GitHubAdapter`
authorizes against classic PATs/OAuth tokens via `GET /user`'s
`X-OAuth-Scopes` response header, and that header only ever contains
classic OAuth scope names (`repo`, `read:org`, etc.), never the
fine-grained vocabulary. `repo` is the single classic scope that grants
read (and write) access to both public and private repository contents --
the only scope this task's repository sync needs.

Every scope actually granted at authorization time is recorded on the `connector_accounts` row and returned by `GET /engineering/connectors` -- never the credential itself.

## Contract shape (resolved)

A connector adapter implements: `authorize`, `backfill`, `incremental_sync`, `handle_webhook`, `refresh_permissions`, `disconnect` (`ecc.domains.engineering.connectors.ConnectorAdapter`). "Validate" (named separately above) is deliberately folded into `authorize`'s own return value rather than a distinct seventh method -- an adapter that cannot validate a credential raises during `authorize` itself; no adapter in Task 1's scope has a distinct post-authorization revalidation step from a fresh credential that `authorize` could not already perform. `refresh_permissions` exists on the contract and is exercised at the adapter-unit level, but has no HTTP caller in Task 1 -- no endpoint in `API-SCHEMAS.md` names a dedicated permission-refresh route; a later task (a scheduled reconciliation job) is the intended caller.

## Retention (resolved)

No raw provider payload is retained beyond what a normalized projection needs -- every projection row stores a `content_hash` for dedupe/change detection instead of a raw payload blob. Credentials are encrypted at rest (Fernet, RFC-005 v1.4.0) with a key distinct from the session secret; the decrypted value is held only for the duration of one authorized call and never logged, returned by an API response, or written into an audit/outbox payload.

Disconnect and delete are two distinct, separately confirmed operations. Disconnect revokes credentials at the provider when supported, stops future sync immediately, and retains previously-synced projections with a `disconnected` freshness state by default (visible, not deleted). Deleting retained projections after disconnect is a separate, explicit operator action, not implied by disconnect.

## Accepted limitation (Task 1)

Disconnect's provider-side credential revocation is best-effort: if the adapter's `disconnect()` call itself fails (or the stored credential cannot be decrypted, e.g. after an encryption-key rotation without re-encryption), the connector is still marked `disconnected` and future sync still stops -- a revocation failure must never block a caller from severing the connection. The provider-side credential may remain live until revoked through the provider's own console in that case; this is disclosed here rather than silently assumed away.

## Task 2 status

`github_adapter.GitHubAdapter` implements the contract above for repositories only: `authorize` (`GET /user`, checks granted scopes from `X-OAuth-Scopes` against the required `repo` scope for a classic token/PAT, rejecting an under-scoped credential), bounded-retry rate-limit handling (`403`/`429` plus `X-RateLimit-Remaining`/`Retry-After`/`X-RateLimit-Reset`, one bounded wait, `partial` sync status beyond it -- a still-rate-limited retry also degrades to `partial` rather than an opaque failure), and `disconnect` as a documented no-op (a personal access token has no revocation API a connector can call on the user's behalf). Work items/changes/reviews/deployments and webhook ingestion's receiving endpoint are not yet implemented -- see `github_adapter.py`'s own module docstring and `docs/phases/phase-006/IMPLEMENTATION-STATUS.md`'s Task 2 evidence.

## Accepted limitation (Task 2): fine-grained PAT scopes are unverifiable

GitHub does not emit `X-OAuth-Scopes` at all for a fine-grained personal access token (only classic OAuth tokens/PATs get that header) -- there is no header-based signal on `GET /user` (or anywhere else) exposing a fine-grained PAT's actual repository/permission grants for this adapter to check. `authorize` distinguishes "header absent" (fine-grained PAT) from "header present but empty" (a classic token authorized with zero scopes, which still fails the scope check normally): for the former, the credential is accepted with an honestly-empty `granted_scopes` rather than a fabricated full-scope grant -- refusing every fine-grained PAT outright would make GitHub's own currently-recommended token type unusable through this connector. An operator connecting a fine-grained PAT sees an empty scope list on `GET /engineering/connectors` and cannot rely on this endpoint's scope check to catch an under-permissioned fine-grained PAT in advance; a downstream sync call simply failing/going `permission_lost` remains the actual signal for that case, identical in kind to how `refresh_permissions` already detects access loss after the fact rather than up front.

## Accepted limitation (Task 2): `refresh_permissions` re-validates, does not diff access

`GitHubAdapter.refresh_permissions` re-checks only whether the credential itself is still valid (`GET /user` succeeding), not whether it has lost access to a *specific* previously-synced repository while remaining otherwise valid -- GitHub has no single endpoint reporting "which of this token's previously-visible repositories can it still see," and this task's scope (repository sync only) stores no per-repo re-check list `refresh_permissions` could iterate. A token that is still valid but has lost access to one org/repo is not distinguished from a fully-permissioned one by this method; that case instead surfaces through the ordinary sync path, where a previously-visible repository simply stops appearing in `GET /user/repos`'s results. Task 2 does not yet mark a no-longer-listed `repositories` row `permission_lost` on that basis -- disclosed here as deferred, not silently assumed handled, matching this section's identical "disclose rather than silently absent" precedent above.

## Accepted limitation (Task 2): the stale-running-sync reap bound is adapter-specific, not contract-level

`sync_connector_endpoint`'s `_STALE_RUNNING_SYNC_THRESHOLD` (`connector_accounts.py`) reaps a `running` `sync_runs` row older than 10 minutes before starting a new sync for the same account -- recovery for a crashed request, not a normal code path. That bound is justified entirely by each real adapter's own bounded call shape (`_MAX_PAGES_PER_CALL`, `_RATE_LIMIT_MAX_WAIT_SECONDS` -- `github_adapter.py` and, as of Task 3, `gitlab_adapter.py`'s identical values), not by any duration guarantee the `ConnectorAdapter` Protocol itself makes. A future adapter (Jira, Task 4, or a later GitHub/GitLab change removing today's page cap) whose legitimate single call can genuinely exceed this bound would have its still-in-flight row reaped and a second, genuinely concurrent call dispatched against the same account -- reintroducing the lost-cursor-update race `uq_sync_runs_running_per_account` (migration 0046) exists to prevent. This constant must be revisited (or made adapter-declared) before registering an adapter without an equivalent per-call duration bound of its own. `sync_connector_endpoint`'s phase 3 UPDATEs are guarded (`AND status = 'running'`) so a late-arriving outcome for an already-reaped run can never silently overwrite the reaper's recorded status -- the narrower, always-safe half of this problem -- though that guard does not by itself prevent the concurrent-call overlap.

## Task 3 status

`gitlab_adapter.GitLabAdapter` implements the contract above for repositories only, against the identical `ConnectorAdapter` contract `github_adapter.GitHubAdapter` (Task 2) implements: `authorize` (`GET /personal_access_tokens/self` for scopes/validity/revocation state, `GET /user` for identity), bounded-retry rate-limit handling (`429` plus `Retry-After`, one bounded wait, `partial` sync status beyond it -- a still-rate-limited retry also degrades to `partial`), and `disconnect` as a genuine (not no-op) best-effort revocation attempt via `DELETE /personal_access_tokens/self`. Work items/changes/reviews/deployments and webhook ingestion's receiving endpoint are not yet implemented -- see `gitlab_adapter.py`'s own module docstring and `docs/phases/phase-006/IMPLEMENTATION-STATUS.md`'s Task 3 evidence.

**GitLab's scope verification has no analogue of GitHub's fine-grained-PAT gap.** `GET /personal_access_tokens/self` returns every GitLab personal access token's actual granted `scopes` in its JSON response body -- unlike GitHub's `X-OAuth-Scopes` header, this signal is never simply absent for a token type this connector supports. `authorize` always checks `granted_scopes` against `_REQUIRED_SCOPES` (`read_api`, `read_repository`) and rejects an under-scoped, inactive, or revoked token; the "Accepted limitation (Task 2): fine-grained PAT scopes are unverifiable" section above is GitHub-specific and does not apply to this adapter.

**GitLab's `disconnect()` attempts real revocation, expected to fail at this connector's own default scope.** GitLab exposes `DELETE /personal_access_tokens/self`, a genuine self-revocation endpoint classic GitHub PATs have no equivalent of -- `gitlab_adapter.py` calls it rather than treating revocation as universally unsupported. This connector's own default read-only scopes (`read_api`, `read_repository`) do not include the `api`/`self_rotate` scope GitLab requires for this call in practice, so the attempt is realistically expected to return `403` for a connection authorized at this connector's own default scope. It is attempted anyway (never silently skipped) and any failure is still absorbed by `disable_connector_endpoint`'s existing best-effort try/except (`CONNECTOR-CONTRACT.md`'s "Accepted limitation (Task 1)" above) -- disconnecting a workspace's GitLab connection always succeeds even though the provider-side token itself is expected to remain live until revoked through GitLab's own settings UI, identical in effect to GitHub's hard no-op despite the different mechanism.
