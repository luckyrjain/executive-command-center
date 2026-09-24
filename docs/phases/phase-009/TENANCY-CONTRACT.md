---
id: PHASE-009-TENANCY
title: Enterprise Tenancy Contract
status: Draft
version: 0.2.0
owner: Lucky Jain
---

# Enterprise Tenancy Contract

Tenant context is derived from authenticated identity and membership, never arbitrary request payloads. All storage queries, signed cursors, caches, indexes, jobs, connector credentials, AI context and observability labels are tenant scoped. Cross-tenant identifiers return 404. Background jobs that pick up due work across every workspace, such as the automation scheduler (`list_schedule_triggers`) and the workflow worker (`claim_next_run`), act only inside each claimed row's own workspace and are not tenant reads in this sense. Any other query that reads connector or tenant data across workspaces needs a named entry under [Exceptions](#exceptions).

No global model, benchmark or analytics dataset may contain tenant content without explicit separate consent. Administrative support access uses just-in-time approval, least privilege, time limits, reason, notification and immutable review. Tenant export/deletion is isolated and verifiable.

## Exceptions

- **Provider-revocation safety check.** Before revoking an OAuth grant at the provider, `ecc.platform.connector_security.revoke_is_safe` (scope `global`) asks whether any live `connector_accounts` row, in any workspace, still uses the same `(provider, external_account_id)`. The query (`_LIVE_ROW_EXISTS_SQL`) returns a single boolean and no identifiers or row data: no account ids, workspace ids or owners leave it. The only trace of its answer is that a revoke skipped because of it is counted as `result="skipped_unsafe"` on `ecc_connector_revoke_total`, labelled by provider and call site only. It exists only to decide whether revoking would cut off another tenant's live connection. It is served by the index `ix_connector_accounts_provider_external_id` (migration `0082_revoke_idx_backfill_log`) and covered by `tests/test_platform_connector_security_postgres.py` and `tests/test_connector_revoke_index_migration_postgres.py`. Any other use of this query, or any widening of what it returns, needs its own named exception here.

## Changelog

- 0.2.0: Added the provider-revocation safety check as a named cross-tenant exception and noted existing cross-workspace background job pickup (security remediation S1.12).
- 0.1.0: Initial draft.
