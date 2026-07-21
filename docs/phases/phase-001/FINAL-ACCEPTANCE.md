# Phase 1 Final Acceptance

## Purpose

This document records the executable evidence required to close PHASE-001.
All Phase 1 product slices, including the executive frontend and the
production-hardening work of `docs/superpowers/plans/2026-07-16-phase-1-completion.md`
(Tasks 1-11, each independently reviewed with zero Critical or Important
findings — `.superpowers/sdd/progress.md`), are implemented on
`feature/phase-1-production-hardening`. This document does not by itself
close PHASE-001: the seven-day daily-use validation
(`docs/runbooks/PHASE-1-DAILY-USE.md`) and human change review remain open
— see "Product validation outside the merge gate" below.

> **Citation note:** this document and `docs/runbooks/PHASE-1-RELEASE-GATE.md`
> cite `.superpowers/sdd/progress.md`, `task-*-review.md`, `task-12-report.md`,
> and `task-ci-secret-fix-report.md` throughout as the backing evidence for
> individual line items. That directory does not exist anywhere in this
> repository (tracked or untracked) — only the unrelated `docs/superpowers/`
> (plans/specs) does. Treat every such citation as an unverifiable dead
> pointer: it does not mean the underlying claim is false, only that this
> document's stated evidence for it cannot currently be inspected. Re-derive
> or re-run the actual check before relying on a citation-only claim.

## Automated gates

| Gate | Evidence | Budget |
|---|---|---|
| Dashboard performance | repeatable PostgreSQL benchmark in the standard test suite | p95 < 2,000 ms |
| Search performance | `tests/test_search_performance_postgres.py` | p95 < 500 ms local; < 800 ms CI |
| Priority ranking | `tests/test_risks_attention_postgres.py` with 10,000 entities | < 500 ms |
| Accessibility | ten named Playwright scenarios (`frontend/e2e/scenarios/`) plus `assertNoSeriousAccessibilityViolations`, orchestrated by `frontend/e2e/run.mjs` (Task 6) | WCAG 2.2 AA core flows, zero serious/critical axe violations |
| Backup integrity | custom-format PostgreSQL archive and SHA-256 (Task 9) | checksum valid |
| Restore integrity | clean PostgreSQL 18 restore with the full Task 9 invariant set (see below) | migration head, row counts, constraints and every invariant below match |
| Security | HIGH+CRITICAL Trivy filesystem and container-image scanning, `pnpm audit --audit-level=high`, gitleaks secret scanning and SBOM generation in standard CI (Task 11); `pip-audit` in the backend CI job | zero HIGH or CRITICAL findings required; **status unverified as of this branch's current commit, not "not currently met" as previously recorded here.** An earlier scan against an older commit on this branch found real findings (`pip-audit` clean; Trivy filesystem HIGH findings in `react-router`; `pnpm audit` findings including `react-router`/`vite`; Trivy image findings in both base images, mostly OS-package). Since that scan ran, this branch has separately bumped `react-router` to `7.18.1` and `vite`/other frontend deps (commit `c4ca876`, above the `>=7.15.0` fix threshold the earlier scan itself cited for the `react-router` finding) and switched the backend base image from `python:3.14.6-slim` to `python:3.14.6-alpine` with an explicit `apk upgrade` step specifically to clear OS-package CVEs (commit `8ebb32d`). Neither change has been re-scanned, so the actual current HIGH/CRITICAL finding count is unknown — do not treat either the old failing numbers or an assumption that the bumps fully resolved it as current evidence. Re-run the `containers`/`security`/`frontend` CI jobs against this branch's current HEAD to get a real answer before treating this gate as open or closed. (The `.superpowers/sdd/task-12-report.md` this row previously cited for detail does not exist anywhere in this repository — there is no `.superpowers/` directory at all, tracked or untracked — so that citation was already dead regardless of the staleness above.) |
| Review closure | specification and code review | Critical 0, High 0, Medium 0 |

The normative machine-readable budgets and evidence paths are stored in `config/phase1-acceptance.json` and validated by `scripts/check_phase1_acceptance.py`, including result-aware validation of recorded backup/restore, performance, and container-scan artifacts (Task 11) — not just evidence-file existence.

## Backup and restore evidence

The acceptance workflow (`scripts/seed_phase1_acceptance.py`, `scripts/backup.sh`, `scripts/restore.sh`, `scripts/verify_restore.sh`, `scripts/phase1_evidence.py` — Task 9):

1. migrates a clean PostgreSQL 18 source database and seeds every one of the 21 Phase 1 tables under two genuinely isolated workspaces with idempotent, deterministic fixtures;
2. creates a custom-format logical archive with the PostgreSQL 18 client and verifies its SHA-256 checksum;
3. restores into a separate clean database;
4. compares the Alembic migration head, row counts for every public table, and the number of public constraints;
5. compares representative-record checksums (full-row `md5`, order-independent) for every seeded table between source and target;
6. verifies append-only audit protection — `audit_events` rows are checksum-identical between source and restored target;
7. verifies PKOS mapped-column checksums (`pkos_nodes`, `pkos_edges`, `pkos_evidence`);
8. verifies workspace isolation — both seeded workspaces are represented in every workspace-scoped table, discovered generically via `information_schema`, not hardcoded;
9. verifies lifecycle-field survival (`archived_at`, `pre_archive_status`) across all seven lifecycle-bearing tables;
10. verifies search index/query readiness by running the real `ecc.search` full-text predicates against the restored database and confirming all 12 seeded marker rows are found;
11. verifies application readiness — boots the real backend against the restored database and polls `/health/ready`;
12. enforces the 600-second recovery time objective, measured against real elapsed time (`$SECONDS`);
13. generates a timestamped JSON+Markdown evidence report (`scripts/phase1_evidence.py`) containing only table names, row counts, checksums, revisions, and booleans — never seeded content.

The scripts remain usable outside CI. `PG_CLIENT_IMAGE=postgres:18.0` provides a portable matching client; without it, locally installed PostgreSQL tools are used. Task 12 re-ran this drill live — see `.superpowers/sdd/task-12-report.md`.

## Accessibility and product evidence

The Playwright acceptance suite (ten named scenario files under `frontend/e2e/scenarios/`, Task 6) covers the Today dashboard, Morning Brief refresh and stale state, recommendation publication and confirmation, Search, Audit, keyboard navigation, named controls, status and error announcements, visible focus, deterministic AI-disabled behavior, and a dedicated `assertNoSeriousAccessibilityViolations` axe scan against every scenario including the persistent `WorkspaceNavigation` chrome.

Workspace-isolation, mutation audit coverage, lifecycle behavior, optimistic concurrency, idempotency, deterministic ranking, search performance, recommendation confirmation, and AI-disabled behavior remain enforced by the standard PostgreSQL and frontend suites.

## Product validation outside the merge gate

The one-week daily-use validation is a product outcome rather than a
deterministic CI gate. It is tracked in `docs/runbooks/PHASE-1-DAILY-USE.md`
and, as of this update, has 0 of the 7 required days recorded. It should be
recorded separately after sustained real use; it does not weaken or bypass
any automated Phase 1 acceptance requirement, and no automated gate above
can substitute for it. PHASE-001 closure additionally requires an explicit
human change-review sign-off, which has also not yet occurred. Neither of
these two items is satisfied by this document or by any part of Task 12.
