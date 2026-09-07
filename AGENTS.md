# AGENTS.md

## Codebase map

FastAPI/Python backend (`backend/ecc/`) + pnpm/TypeScript/React frontend (`frontend/src/`), Postgres +
pgvector only (no Neo4j/Qdrant/Redis, despite `docs/architecture/chapter-08-data-platform.md`'s target
design — see that chapter's Implementation Status section and `docs/specifications/SCR-0001-*.md`).

- **Backend entry point**: `backend/ecc/main.py` — the FastAPI composition root. Every domain router is
  mounted here (`app.include_router(...)`); this is the file to read to see the whole request pipeline
  (middleware order, router registration order) and the one file every new domain endpoint mechanically
  touches.
- **Backend domains** (`backend/ecc/domains/<name>/`, one directory per business domain — routers, models,
  and hand-written SQL live together per file, there is no separate repository/service layer):
  `ai_runtime`, `attention`, `automation`, `calendar`, `collaboration`, `communication`, `engineering`,
  `governance`, `identity`, `knowledge`, `personal`, `planning`, `platform` (despite the name, a real
  business domain — cross-domain read endpoints: `dashboard_briefs.py`, `audit_queries.py` — not infra;
  see the next bullet for the other, unrelated `platform`), `scheduling`.
- **Frontend features** (`frontend/src/features/<name>/`): `attention`, `automation`, `collaboration`,
  `commitments`, `engineering`, `knowledge`, `notes`, `personal`, `risks`, `schedule`, `tasks`. This
  vocabulary does **not** map 1:1 onto the backend domain list above — e.g. backend `governance` splits
  across frontend `features/governance/` and `features/risks/`. `MorningBrief.tsx`/`Sections.tsx` live
  under `frontend/src/dashboard/` and `SearchAuditPanel.tsx` under `features/search-audit/` — check there
  if a `features/<backend-domain-name>/` search comes up empty. Backend `communication`, `scheduling`, and
  `domains/platform` have no directly-named frontend feature folder.
- **Auth/authz seam**: `backend/ecc/auth.py` (`AuthDep`, the one FastAPI dependency every router imports)
  and `backend/ecc/platform/authz.py` (`authorize()`/`require_role_action()`, the one decision engine most
  domains call rather than reimplementing checks — `domains/personal/*` is the deliberate exception, see
  that package's own `workspace_id`+`owner_id` scoping instead). **`backend/ecc/platform/`** (`authz.py`,
  `authz_grants.py`, `audit_outbox.py`, `idempotency.py`, `notifications.py`, `events/`) is shared infra
  living beside `domains/`, not inside it — unrelated to the business-domain `domains/platform/` two
  bullets up despite the identical name; a "platform" search should check both. Other cross-cutting
  infra (`auth.py`, `config.py`, `observability.py`, `logging.py`) lives directly at `backend/ecc/*`,
  outside either `platform/`.
- **Tests**: backend tests live centrally under `tests/` at the repo root (not colocated with
  `backend/ecc/domains/`), named `test_<domain>_<feature>_postgres.py`, and run against a real Postgres.
  Frontend tests are colocated next to the component they test (`Foo.tsx` + `Foo.test.tsx`).
- **Architecture docs**: `docs/architecture/chapter-*.md` are Draft RFC-004 chapters describing the target
  architecture; several central claims (an Application Gateway, a Memory Engine, polyglot Neo4j/Qdrant/Redis
  persistence) were never built — each affected chapter has an "Implementation Status" section near the top
  stating what's real. Trust the Implementation Status sections and the code over the rest of the chapter
  when they conflict.

## Agent skills

### Issue tracker

Issues/PRs tracked via GitHub Issues (`gh` CLI); external PRs are not treated as a triage request surface. See `docs/agents/issue-tracker.md`.

### Triage labels

Default label vocabulary — label string equals role name (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`). See `docs/agents/triage-labels.md`.

### Domain docs

Single-context layout — one `CONTEXT.md` (not yet created) + `docs/adr/` at the repo root. See `docs/agents/domain.md`.
