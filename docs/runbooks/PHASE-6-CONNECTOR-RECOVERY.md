---
id: PHASE-6-CONNECTOR-RECOVERY
title: Phase 6 Connector Recovery Runbook
status: Active
version: 1.0.0
owner: Lucky Jain
updated: 2026-08-06
---

# Phase 6 Connector Recovery Runbook

This runbook covers the implemented GitHub, GitLab, Jira, Datadog, and sandbox connector lifecycle. Production recovery remains blocked by [`../operations/PRODUCTION-READINESS.md#blockers`](../operations/PRODUCTION-READINESS.md#blockers) PR-004 and PR-006.

## Detection and containment

Treat `permission_lost`, `rate_limited`, `error`, a failed/partial sync run, an unexpectedly old last-success time, or a provider-side credential alert as an incident signal. Stop repeated retries. If continued access is unsafe, disable the connector and separately revoke the credential in the provider console. Preserve non-secret request/sync-run identifiers and do not edit cursors, credentials, or projection rows.

## Supported operator actions

1. Inspect `GET /api/v1/engineering/connectors` and `GET /api/v1/engineering/sync-runs?connector_account_id=<id>` as an authorized workspace member.
2. If a sync is not already running and the account remains active, initiate `POST /api/v1/engineering/connectors/<id>/sync` with CSRF protection, an `Idempotency-Key`, and the intended sync request. Sync is manual; no scheduler or push subscription exists.
3. If credentials are revoked, permissions are insufficient, or continued fetch is unsafe, initiate `POST /api/v1/engineering/connectors/<id>/disable`. The account becomes `disconnected` and existing projections are retained with disconnected freshness.
4. Verify provider access in the provider's own security settings. Disable attempts provider revocation best effort; local success alone does not prove the remote credential was revoked.
5. Verify no further sync can start and the provider credential is revoked or explicitly recorded as still live. A disconnected Phase 6 account cannot be reactivated or have its credential replaced through the current API; creating the same provider/account again conflicts with the retained row.

## Partial sync and provider failure

Keep the last successful cursor and projections. Review the recorded sync-run status and sanitized error, correct provider authorization or availability, then start a new manual sync. The adapter owns deduplication/upsert behavior; operators must not advance cursors or rewrite projection rows manually.

Credential-key rotation and automated re-encryption are **Unsupported — production blocker**. Contain by keeping the configured key stable, restricting deployment, disabling affected connectors when possible, and revoking credentials through the provider console. Do not attempt reauthorization or credential replacement through Phase 6 because no supported path exists. See PR-004. A complete provider-revocation and partial-sync recovery exercise is also **Unsupported — production blocker** until PR-006 closes.

## Escalation

Escalate to the repository owner when a provider credential cannot be confirmed revoked, a sync remains `running`, a partial retry cannot make progress, or reconnection is required. Keep the connector disabled. Recovery that needs cursor repair, credential replacement, or row deletion is unsupported until PR-006 closes.

## Evidence to retain

Record connector ID/provider, sync-run ID, timestamps, safe error code, whether remote revocation was independently verified, whether stale projections were marked disconnected, and the reviewer. Never record tokens, OAuth codes, message content, or encrypted credential values.
