---
id: PHASE-008-IMPLEMENTATION-STATUS
title: Phase 8 Implementation Status
status: Task 1 (account/membership/session framework) complete
version: 0.2.0
owner: Lucky Jain
updated: 2026-08-01
---

# Phase 8 Implementation Status

Design pass and Task 1 delivered per `docs/superpowers/plans/2026-08-01-phase-8-multi-user.md`.

| Slice | Status |
|---|---|
| User identity, membership and invitations | Partial -- account/membership/session framework done (Task 1); invitations not started (Task 2) |
| Roles, grants and authorization engine | Not started |
| Resource visibility and sharing | Not started |
| Delegation and acceptance | Not started |
| Notifications and shared activity | Not started |
| Removal, transfer and retention | Not started |
| Multi-identity browser acceptance | Not started |

## Task 1 evidence

**Complete.** `docs/superpowers/plans/2026-08-01-phase-8-multi-user.md`'s Task 1: the `accounts`/`workspace_memberships` schema (migration `0061_phase8_accounts_memberships.py`) and `ecc.domains.identity.accounts` (`POST /accounts`, `POST /auth/login`, `POST /auth/select-workspace`, `GET|POST /workspaces`, `GET|PATCH /workspaces/{id}`). Full detail in `docs/phases/phase-008/DATA-MODEL.md`'s and `docs/phases/phase-008/API-SCHEMAS.md`'s own "Task 1 status" sections.

**Central design choice, verified in practice, not just asserted.** `users` keeps its exact pre-existing role as the composite `(workspace_id, id)` FK anchor every `owner_id`/`created_by`/`updated_by` column already points to; only a new `account_id` column was added. After running the real migration against local Postgres, all ~14 pre-existing composite FKs to `users.(workspace_id, id)` were directly inspected via `\d` and confirmed completely unaffected -- this migration does not touch them.

**Two deliberate, disclosed scope-narrowings for this task**, not silent gaps: (1) `POST /accounts` is genuine open self-registration -- no invitation gate exists yet, since Task 2 hasn't shipped the `invitations` table; (2) pre-auth endpoints (`POST /accounts`, `POST /auth/login`) skip `Idempotency-Key` support entirely -- `idempotency_records` is keyed on `(workspace_id, actor_id)`, which structurally does not exist yet at those call sites, and `accounts.email`'s own `UNIQUE` constraint already makes retrying account creation safe.

**Test-suite migration.** ~75 pre-existing `tests/*_postgres.py` files seeded their own fixture identity via an inline three-column `INSERT INTO users (id, workspace_id, email, password_hash, created_at)` that no longer matches the new schema. Rather than hand-edit each file, an AST-based transform script located every matching call (tolerant of `text`/aliased-`text` and whitespace variation) and replaced it with a call to a new shared `tests/identity_fixtures.py:create_identity` helper (which performs the new three-table `accounts`/`users`/`workspace_memberships` insert sequence), applied end-of-file-backwards using exact `lineno`/`col_offset` character-span splicing. Verified via dry-run diff before applying, then spot-verified against real Postgres (`test_workspace_isolation.py`, `test_personal_insight_tools_postgres.py`, and several others -- all passing, confirming the transform is correct and introduces no regression). `identity_fixtures.py` also exports `add_membership`, for tests that need one account holding active memberships in more than one workspace.

**New tests.** `tests/test_identity_accounts_postgres.py` (13 tests): account creation + duplicate-email rejection (case-insensitive) + short-password rejection; single-active-membership login auto-authenticates; wrong-password/no-active-membership/disabled-account login rejections; two-active-memberships login returns a `pending_login_token` and completes via `select-workspace`; `select-workspace` rejects a forged/expired pending token and a workspace the account has no membership in (cross-account isolation -- knowing a real `workspace_id` is not enough); the authenticated-session switch mode requires CSRF; `GET /workspaces` excludes suspended memberships; `POST /workspaces` makes the caller owner; `PATCH /workspaces/{id}` is role-gated (403 for a viewer); `GET /workspaces/{id}` 404s for a different workspace id.

**Verified.** `ruff check`/`ruff format --check` clean across `backend/`, `scripts/` and `tests/`. `mypy backend` clean (165 source files -- `tests/` is out of mypy's CI scope). Migration `upgrade head` / `downgrade -1` / `upgrade head` round-trip verified against real local Postgres 16, schema and backfilled data (28 accounts/users/memberships from existing seed data) confirmed correct by direct `\d`/`SELECT` inspection. `tests/test_identity_accounts_postgres.py` itself could not run in this sandbox's Python 3.11 venv -- it imports `ecc.main.app`, which transitively hits the same pre-existing, unrelated Python-3.11-only `NameError` (a self-referencing return-type annotation without `from __future__ import annotations`, safe under the real Python-3.14-with-PEP-649 CI environment) already documented for other `TestClient`-based files in this repository; several transformed non-`TestClient` test files (`test_workspace_isolation.py`, `test_personal_insight_tools_postgres.py`, `test_automation_triggers_postgres.py`, `test_ai_runtime_tools_postgres.py`, `test_knowledge_rebuild_performance_postgres.py`) ran for real against Postgres and passed, confirming the schema/fixture migration itself is correct; the new file's own dynamic verification happens in CI.
