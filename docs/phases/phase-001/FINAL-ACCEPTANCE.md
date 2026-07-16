# Phase 1 Final Acceptance

## Purpose

This document records the executable evidence required to close PHASE-001. All Phase 1 product slices are merged into `main`; PR #13 adds the final automated acceptance and recovery gate.

## Automated gates

| Gate | Evidence | Budget |
|---|---|---|
| Dashboard performance | repeatable PostgreSQL benchmark in the standard test suite | p95 < 2,000 ms |
| Search performance | `tests/test_search_performance_postgres.py` | p95 < 500 ms local; < 800 ms CI |
| Priority ranking | `tests/test_risks_attention_postgres.py` with 10,000 entities | < 500 ms |
| Accessibility | `frontend/e2e/run.mjs` keyboard and semantic checks | WCAG 2.2 AA core flows |
| Backup integrity | custom-format PostgreSQL archive and SHA-256 | checksum valid |
| Restore integrity | clean PostgreSQL 18 restore | migration head, table row counts and constraints match |
| Security | dependency and filesystem scanning in standard CI | zero critical vulnerabilities |
| Review closure | specification and code review | Critical 0, High 0, Medium 0 |

The normative machine-readable budgets and evidence paths are stored in `config/phase1-acceptance.json` and validated by `scripts/check_phase1_acceptance.py`.

## Backup and restore evidence

The acceptance workflow:

1. migrates a clean PostgreSQL 18 source database;
2. creates a custom-format logical archive with the PostgreSQL 18 client;
3. produces and verifies a SHA-256 checksum;
4. restores into a separate clean database;
5. compares the Alembic migration head;
6. compares row counts for every public table;
7. compares the number of public constraints;
8. enforces the ten-minute development recovery target.

The scripts remain usable outside CI. `PG_CLIENT_IMAGE=postgres:18.0` provides a portable matching client; without it, locally installed PostgreSQL tools are used.

## Accessibility and product evidence

The Playwright acceptance suite covers the Today dashboard, Morning Brief refresh and stale state, recommendation publication and confirmation, Search, Audit, keyboard navigation, named controls, status and error announcements, visible focus, and deterministic AI-disabled behavior.

Workspace-isolation, mutation audit coverage, lifecycle behavior, optimistic concurrency, idempotency, deterministic ranking, search performance, recommendation confirmation, and AI-disabled behavior remain enforced by the standard PostgreSQL and frontend suites.

## Product validation outside the merge gate

The one-week daily-use validation is a product outcome rather than a deterministic CI gate. It should be recorded separately after sustained use; it does not weaken or bypass any automated Phase 1 acceptance requirement.
