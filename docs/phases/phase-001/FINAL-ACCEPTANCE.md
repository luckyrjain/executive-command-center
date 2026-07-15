# Phase 1 Final Acceptance

## Purpose

This document records the executable evidence required to close PHASE-001. It does not waive incomplete product slices. A gate can pass only after all Phase 1 entities and surfaces are merged into `main`.

## Automated gates

| Gate | Evidence | Budget |
|---|---|---|
| Dashboard performance | repeatable local benchmark | p95 < 2,000 ms |
| Search performance | local and CI benchmark | p95 < 500 ms local; < 800 ms CI |
| Priority ranking | deterministic 10,000-entity benchmark | < 500 ms |
| Accessibility | Playwright keyboard and semantic smoke checks | WCAG 2.2 AA core flows |
| Backup integrity | custom-format PostgreSQL archive and SHA-256 | checksum valid |
| Restore integrity | clean PostgreSQL 18 restore | migration head, table row counts and constraints match |
| Security | dependency and filesystem scanning | zero critical vulnerabilities |
| Review closure | specification and code review | Critical 0, High 0, Medium 0 |

The normative machine-readable budgets are stored in `config/phase1-acceptance.json` and validated by `scripts/check_phase1_acceptance.py`.

## Backup and restore evidence

The acceptance workflow must:

1. migrate a clean PostgreSQL 18 source database;
2. create a custom-format logical archive;
3. produce and verify a SHA-256 checksum;
4. restore into a separate clean database;
5. compare the Alembic migration head;
6. compare row counts for every public table;
7. compare the number of public constraints;
8. complete within the ten-minute development recovery target.

## Accessibility evidence

Core user flows must be operable using the keyboard and expose semantic headings, named controls, status/error announcements and visible focus. Accessibility acceptance expands as each Phase 1 frontend surface lands.

## Remaining final evidence

The following cannot be closed until the dependent Phase 1 slices are implemented:

- dashboard, search and ranking performance benchmarks;
- workspace-isolation coverage for every Phase 1 table;
- audit coverage for every mutation;
- AI-disabled dashboard, brief and recommendation acceptance;
- complete Playwright coverage for notes, calendar, meetings, risks, search, audit and recommendations;
- one-week daily-use validation.
