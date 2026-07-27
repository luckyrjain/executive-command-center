---
id: PHASE-006-CONNECTOR
title: Engineering Connector Contract
status: Approved for Implementation
version: 0.2.0
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
| GitHub | `contents:read`, `metadata:read`, `pull_requests:read`, `issues:read` | `contents:write`, `pull_requests:write` |
| GitLab | `read_api`, `read_repository` | `api` |
| Jira | `read:jira-work` | `write:jira-work` |

Every scope actually granted at authorization time is recorded on the `connector_accounts` row and returned by `GET /engineering/connectors` -- never the credential itself.

## Contract shape (resolved)

A connector adapter implements: `authorize`, `backfill`, `incremental_sync`, `handle_webhook`, `refresh_permissions`, `disconnect` (`ecc.domains.engineering.connectors.ConnectorAdapter`). "Validate" (named separately above) is deliberately folded into `authorize`'s own return value rather than a distinct seventh method -- an adapter that cannot validate a credential raises during `authorize` itself; no adapter in Task 1's scope has a distinct post-authorization revalidation step from a fresh credential that `authorize` could not already perform. `refresh_permissions` exists on the contract and is exercised at the adapter-unit level, but has no HTTP caller in Task 1 -- no endpoint in `API-SCHEMAS.md` names a dedicated permission-refresh route; a later task (a scheduled reconciliation job) is the intended caller.

## Retention (resolved)

No raw provider payload is retained beyond what a normalized projection needs -- every projection row stores a `content_hash` for dedupe/change detection instead of a raw payload blob. Credentials are encrypted at rest (Fernet, RFC-005 v1.4.0) with a key distinct from the session secret; the decrypted value is held only for the duration of one authorized call and never logged, returned by an API response, or written into an audit/outbox payload.

Disconnect and delete are two distinct, separately confirmed operations. Disconnect revokes credentials at the provider when supported, stops future sync immediately, and retains previously-synced projections with a `disconnected` freshness state by default (visible, not deleted). Deleting retained projections after disconnect is a separate, explicit operator action, not implied by disconnect.

## Accepted limitation (Task 1)

Disconnect's provider-side credential revocation is best-effort: if the adapter's `disconnect()` call itself fails (or the stored credential cannot be decrypted, e.g. after an encryption-key rotation without re-encryption), the connector is still marked `disconnected` and future sync still stops -- a revocation failure must never block a caller from severing the connection. The provider-side credential may remain live until revoked through the provider's own console in that case; this is disclosed here rather than silently assumed away.
