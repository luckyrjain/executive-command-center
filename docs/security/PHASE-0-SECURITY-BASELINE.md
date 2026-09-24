---
id: PHASE-0-SECURITY-BASELINE
title: Phase 0 Security Baseline
status: Approved
version: 1.1.0
owner: Lucky Jain
---

# Phase 0 Security Baseline

## Scope

This document defines the minimum security controls required before Phase 0 can exit. It applies to local development, CI and the single-machine Docker Compose deployment.

## Identity and session controls

- one local owner account per installation
- Argon2id password hashing
- opaque server-side sessions stored in PostgreSQL
- `HttpOnly` and `SameSite=Lax` cookies
- `Secure` cookies outside localhost HTTP development
- CSRF validation on state-changing browser requests
- 12-hour idle timeout and 7-day absolute lifetime
- rotation after login and privilege-sensitive operations
- immediate server-side revocation on logout
- authorization checks at API, application and repository boundaries

## Workspace isolation

Every persisted domain, event, PKOS and configuration record MUST include `workspace_id`. User-owned records MUST include `owner_id`. Repository queries MUST require workspace context and tests MUST prove that cross-workspace reads and writes fail.

### Workspace isolation exceptions

- **Background job pickup.** Background jobs that pick up due work across every workspace, such as the automation scheduler (`list_schedule_triggers`) and the workflow worker (`claim_next_run`), then act only inside each claimed row's own workspace. They predate this section and are not a tenant data read; see the [Enterprise Tenancy Contract](../phases/phase-009/TENANCY-CONTRACT.md).
- **Provider-revocation safety check.** `ecc.platform.connector_security.revoke_is_safe` (scope `global`) runs one existence check on `connector_accounts (provider, external_account_id)` across all workspaces before an OAuth grant is revoked at the provider. It returns a boolean only, never identifiers or row data, and is used for nothing else; a revoke it blocks is counted only as `result="skipped_unsafe"` on `ecc_connector_revoke_total` (provider and call-site labels). It is indexed by `ix_connector_accounts_provider_external_id` (migration `0082_revoke_idx_backfill_log`) and covered by `tests/test_platform_connector_security_postgres.py` and `tests/test_connector_revoke_index_migration_postgres.py`. See the [Enterprise Tenancy Contract](../phases/phase-009/TENANCY-CONTRACT.md#exceptions).
- **`personal_visibility_backfill_log`.** This ops-only table (same migration) has no `workspace_id`. It is reserved for the `scripts/backfill_personal_visibility.py` ops command (security remediation S1.8(b), not yet landed), which records and restores the visibility and owner of rows it made private. It is not a domain record, is never exposed through the API and is not an authorization resource.

## Secrets

- `.env` and local secret files are ignored by Git
- committed examples contain placeholders only
- connector tokens are stored separately from authentication data
- secrets never appear in logs, traces, fixtures, screenshots or test snapshots
- Gitleaks runs on pull requests and the full reachable Git history

## Supply-chain controls

CI MUST run:

- Gitleaks for secret detection
- Trivy for filesystem and container vulnerability scanning
- Syft for SBOM generation
- pip-audit for Python dependencies
- `pnpm audit` for JavaScript dependencies

Verified committed secrets and critical vulnerabilities fail CI unless there is a documented, owner-approved and time-bound exception.

## Application controls

- request-body and query validation through typed schemas
- explicit output schemas for public APIs
- no wildcard CORS configuration
- local development origins are allowlisted explicitly
- state-changing APIs require authenticated workspace context
- error responses do not expose stack traces or secrets
- structured logs exclude credentials, full source content, PII and prompt context

## Database controls

- application database credentials use least privilege
- migrations run through a dedicated controlled command
- PostgreSQL is not exposed beyond localhost by default
- backup files inherit local filesystem permissions and are excluded from Git

## Container controls

- immutable image version tags
- non-root application containers where supported
- only required ports exposed
- health checks for application and PostgreSQL
- no Docker socket mounting

## Required tests

- password hashing and verification
- login failure behavior
- cookie attribute checks
- CSRF rejection and acceptance
- session rotation, expiration and logout revocation
- workspace-isolation tests at API and repository layers
- secret-scanner test fixture proving CI detection
- dependency and container scanning in CI

## Exit evidence

The Phase 0 exit review MUST include links to passing CI runs, generated SBOM artifacts, vulnerability scan summaries and the security test report.

## Changelog

- 1.1.0: Added named workspace-isolation exceptions for the provider-revocation safety check and the `personal_visibility_backfill_log` ops table, and noted existing background job pickup (security remediation S1.12).
- 1.0.0: Approved baseline.
