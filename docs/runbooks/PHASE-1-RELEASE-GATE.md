# Phase 1 Production Release Gate

**Status:** In progress  
**Scope:** Executive Command Center Phase 1  
**Baseline:** PR #12 merge commit `399b3ea7b0743590ff8996452e709813b6c10fcf`

## Required release checks

### Application correctness

- [ ] Backend Ruff, formatting, mypy, Alembic and PostgreSQL tests pass.
- [ ] Frontend typecheck, unit tests, production build and Chromium acceptance pass.
- [ ] All lifecycle mutations preserve optimistic version checks, idempotency, CSRF and workspace isolation.
- [ ] Search, Audit, Today, Morning Brief, Recommendations and Work Actions pass acceptance coverage.

### Security and production configuration

- [ ] Production configuration rejects insecure defaults.
- [ ] Security headers are emitted by the frontend and backend entry points.
- [ ] Request size and rate limits are defined for authenticated and mutation routes.
- [ ] Session cookies remain secure, HTTP-only and same-site constrained in production.
- [ ] Dependency, secret, container and SBOM scans pass.

### Observability

- [ ] Structured logs include request ID, correlation ID, workspace ID, route, status and duration.
- [ ] Health, readiness and version endpoints are documented and exercised.
- [ ] Metrics cover request count, latency, errors, database failures and outbox backlog.
- [ ] Sensitive note bodies, evidence payloads, session values and CSRF tokens never enter logs.

### Backup and recovery

- [ ] PostgreSQL backup command and retention policy are documented.
- [ ] Restore is verified into an isolated database.
- [ ] Alembic head, row counts and representative workspace data are validated after restore.
- [ ] Recovery point objective and recovery time objective are recorded.
- [ ] A restore drill produces a timestamped evidence report.

### Accessibility and UX

- [ ] Keyboard navigation covers all interactive surfaces.
- [ ] Focus visibility, labels, landmarks, status and alert regions are validated.
- [ ] Automated accessibility checks report no serious or critical violations.
- [ ] Loading, empty, stale, degraded, conflict and error states remain recoverable.

### Operations

- [ ] Deployment and rollback procedures are documented.
- [ ] Database migration rollback limitations are explicit.
- [ ] Environment variables and secret ownership are documented.
- [ ] Post-deployment smoke checks are automated.
- [ ] Critical, High and Medium review findings are zero before merge.

## Exit criteria

Phase 1 is releasable only when every required check above is backed by an automated test, CI result, or timestamped operational evidence. Any exception must name an owner, expiry date and rollback plan.
