# Phase 1 Production Release Gate

**Status:** In progress — 25 of 27 engineering checks below are evidenced by Tasks 1-11 (`.superpowers/sdd/progress.md`, `.superpowers/sdd/task-1-review.md` through `task-11-review.md`) and Task 12's local full-proof re-run (`.superpowers/sdd/task-12-report.md`). 2 checks remain explicitly open, both genuine, currently-failing problems rather than merely undocumented gaps: (1) dependency/container scans, which Task 12 discovered actually CAN run locally (network access is available here, contrary to `task-11-review.md`'s assumption) — and found real HIGH/CRITICAL findings in frontend dependencies and both container base images; (2) post-deployment smoke checks are documented and manually exercised but not yet wired into an automated pipeline (Phase 1 has no hosted environment yet). A third finding from Task 12's re-run — the full backend `pytest` suite failing 11 tests under CI's actual configured secret, a test-isolation defect in `tests/test_production_security.py`'s `restore_main_module` fixture — was fixed and independently re-reviewed in commit `87e12b2` (snapshot-and-restore of the real prior environment state instead of a hardcoded literal; before/after and no-regression evidence in `.superpowers/sdd/task-ci-secret-fix-report.md`); that checklist item below is now checked. See each remaining open item's note below and `.superpowers/sdd/task-12-report.md`'s Concerns section for full detail. This document governs engineering release readiness only — it does NOT close Phase 1: the seven-day daily-use gate (`docs/runbooks/PHASE-1-DAILY-USE.md`, 0 of 7 days recorded) and human change review remain separately open regardless of this checklist's state.
**Scope:** Executive Command Center Phase 1
**Baseline:** `feature/phase-1-production-hardening`, commit `7072b1c` (Task 12's documentation-synchronization commit `0edfa37`, the standalone CI-secret fix `87e12b2`, and a follow-up runbook correction).

## Required release checks

### Application correctness

- [x] Backend Ruff, formatting, mypy, Alembic and PostgreSQL tests pass. (Task 12 re-run: Ruff, format, mypy and `alembic upgrade head` all pass cleanly. Task 12 discovered the full `pytest` suite deterministically failed 11 tests with CSRF 403s when `ECC_SESSION_SECRET` was set to the value CI's `ci.yml` actually configures — root cause: `tests/test_production_security.py`'s `restore_main_module` fixture teardown hardcoded `os.environ["ECC_SESSION_SECRET"]` to a literal instead of restoring the prior value, permanently reloading `ecc.main` with a different secret than the rest of the process started with. This directly contradicted `task-9-review.md`'s "resolved as a non-issue" conclusion, which was reached without testing against CI's actual configured secret value. Fixed in commit `87e12b2` (snapshot-and-restore of the true pre-test environment state, including "was absent," rather than a hardcoded literal) and independently re-reviewed: `ECC_SESSION_SECRET=ci-secret-value-that-is-at-least-32-characters uv run pytest -q` now passes with only the pre-existing, disclosed ranking-test flake (Task 10), matching the default-secret run — no regression to the file's CORS/dev-bootstrap/body-size tests. See `.superpowers/sdd/task-ci-secret-fix-report.md`.)
- [x] Frontend typecheck, unit tests, production build and Chromium acceptance pass. (Task 6: `frontend/e2e/scenarios/*`, `task-6-review.md`; re-run locally in Task 12)
- [x] All lifecycle mutations preserve optimistic version checks, idempotency, CSRF and workspace isolation. (Tasks 1-5, 9: `task-1-review.md` through `task-5-review.md`; a CSRF cache-pollution concern was investigated in `task-9-review.md` and initially judged a non-defect, but that conclusion was itself found incomplete by Task 12 — the actual test-isolation defect and its fix are recorded in the item above)
- [x] Search, Audit, Today, Morning Brief, Recommendations and Work Actions pass acceptance coverage. (Tasks 5-6: `task-5-review.md`, `task-6-review.md` — ten named Playwright scenarios)

### Security and production configuration

- [x] Production configuration rejects insecure defaults. (Task 7: `backend/ecc/config.py` `validate_production_settings`, `task-7-review.md`)
- [x] Security headers are emitted by the frontend and backend entry points. (Task 7: `backend/ecc/http_security.py`, `frontend/nginx.conf.template`, `task-7-review.md`)
- [x] Request size and rate limits are defined for authenticated and mutation routes. (Task 7: non-buffering 413 body-size limit and bounded 429 rate limiting, `task-7-review.md`)
- [x] Session cookies remain secure, HTTP-only and same-site constrained in production. (Task 7: the only cookie-issuing code path, `backend/ecc/dev_bootstrap.py`, is gated to `ECC_ENV=development` only — no cookie is ever issued outside development — `task-7-review.md`)
- [ ] Dependency, secret, container and SBOM scans pass. (Task 12: contrary to `task-11-review.md`'s assumption that live CVE-database scans could only run inside GitHub Actions, this environment has outbound network access and Docker, so Task 12 actually ran them locally against real CVE data — and they currently FAIL, not merely "unverifiable": `pip-audit` is clean; Trivy filesystem scan (`--severity HIGH,CRITICAL`) finds 6 real HIGH findings in `pnpm-lock.yaml`'s `react-router` (fixed in >=7.15.0); `pnpm audit --audit-level=high` finds 19 findings (1 critical, 9 high) including `react-router` and `vite`; Trivy image scans find 22 HIGH/CRITICAL findings in the backend image and 37 in the frontend image, mostly base-OS-image (`python:3.14.6-slim`, `nginx:1.27-alpine`) packages rather than application code. gitleaks and the SBOM step were not independently re-run locally. See `.superpowers/sdd/task-12-report.md`'s Concerns section for full findings and exact commands. This is a genuine, currently-failing gate requiring a dependency/base-image update pass before this branch's real CI run would go green — not a documentation gap.)

### Observability

- [x] Structured logs include request ID, correlation ID, workspace ID, route, status and duration. (Task 8: `backend/ecc/observability.py`, `task-8-review.md`)
- [x] Health, readiness and version endpoints are documented and exercised. (Task 8: `/health/live`, `/health/ready`, `/version` in `backend/ecc/main.py`; `tests/test_health.py`, `tests/test_observability.py`; documented in `README.md` and `docs/runbooks/PHASE-1-DEPLOYMENT.md`)
- [x] Metrics cover request count, latency, errors, database failures and outbox backlog. (Task 8: `task-8-review.md` — DB-failure path fixed and re-verified)
- [x] Sensitive note bodies, evidence payloads, session values and CSRF tokens never enter logs. (Task 8: negative-space test against real secret markers, `task-8-review.md`)

### Backup and recovery

- [x] PostgreSQL backup command and retention policy are documented. (Task 9: `scripts/backup.sh`; retention policy newly documented in Task 12's `docs/runbooks/PHASE-1-DEPLOYMENT.md`)
- [x] Restore is verified into an isolated database. (Task 9: `scripts/restore.sh`, `scripts/verify_restore.sh`, `task-9-review.md`; re-run live in Task 12)
- [x] Alembic head, row counts and representative workspace data are validated after restore. (Task 9: `task-9-review.md`)
- [x] Recovery point objective and recovery time objective are recorded. (Task 9: RTO 600s budget, measured well within budget both in `task-9-report.md` and Task 12's re-run; RPO recorded in `docs/runbooks/PHASE-1-DEPLOYMENT.md`)
- [x] A restore drill produces a timestamped evidence report. (Task 9: `scripts/phase1_evidence.py`, `task-9-review.md`)

### Accessibility and UX

- [x] Keyboard navigation covers all interactive surfaces. (Task 6: `task-6-review.md`)
- [x] Focus visibility, labels, landmarks, status and alert regions are validated. (Task 6: `task-6-review.md`, nav accessibility gap closed in round 2)
- [x] Automated accessibility checks report no serious or critical violations. (Task 6: `assertNoSeriousAccessibilityViolations` across ten scenarios, `task-6-review.md`; re-run in Task 12)
- [x] Loading, empty, stale, degraded, conflict and error states remain recoverable. (Task 6: `task-6-review.md`)

### Operations

- [x] Deployment and rollback procedures are documented. (Task 12: `docs/runbooks/PHASE-1-DEPLOYMENT.md`)
- [x] Database migration rollback limitations are explicit. (Task 12: `docs/runbooks/PHASE-1-DEPLOYMENT.md`'s "Rollback" section)
- [x] Environment variables and secret ownership are documented. (Task 12: `docs/runbooks/PHASE-1-DEPLOYMENT.md`'s "Environment variables" section)
- [ ] Post-deployment smoke checks are automated. (Task 12: exact smoke-check commands are documented in `docs/runbooks/PHASE-1-DEPLOYMENT.md` and were manually run in Task 12's full-proof section, but no CI/CD pipeline exists yet to run them automatically against a live deployment — Phase 1 has no hosted environment. Remains open.)
- [x] Critical, High and Medium review findings are zero before merge. (Tasks 1-11: every task review in `.superpowers/sdd/task-1-review.md` through `task-11-review.md` records zero Critical and zero Important/High findings at closure — only disclosed Minor notes carried forward — per `.superpowers/sdd/progress.md`)

## Exit criteria

Phase 1 is releasable only when every required check above is backed by an automated test, CI result, or timestamped operational evidence. Any exception must name an owner, expiry date and rollback plan. Even when every check above is closed, Phase 1 overall completion additionally requires the seven-day daily-use validation (`docs/runbooks/PHASE-1-DAILY-USE.md`) and explicit human change-review sign-off — neither is satisfied by this checklist and neither may be marked complete by any automated process.
