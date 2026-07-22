# Phase 2 Deployment Runbook (Delta from Phase 1)

**Scope:** what changes operationally when deploying Phase 2 (the knowledge
platform: entities, claims, relationships, timeline, resolution/merge,
lexical retrieval) on top of an existing Phase 1 deployment.

This document only records the delta. Everything in
`docs/runbooks/PHASE-1-DEPLOYMENT.md` — environment variables, deploy steps,
migration commands, smoke check, rollback, backup/restore, and change
ownership — still applies unchanged. Read that document first; this one
assumes it.

## What's new

- **Migrations `0010`-`0014`** (`backend/migrations/versions/`): PKOS
  reconciliation (`0010_phase2_pkos_reconciliation.py`), knowledge
  entities/aliases/claims (`0011_phase2_knowledge_entities.py`), timeline
  projection (`0012_phase2_timeline.py`), resolution candidates and entity
  operations (`0013_phase2_resolution.py`), and retrieval documents
  (`0014_phase2_retrieval.py`). These extend `pkos_nodes`/`pkos_edges`/
  `pkos_evidence` rather than adding parallel tables (per
  `phase-002/DATA-MODEL.md`'s reconciliation decision) and add
  `timeline_entries`, `resolution_candidates`, `entity_operations`, and
  `retrieval_documents`. Applied the same way as any Phase 1 migration —
  `uv run alembic -c backend/alembic.ini upgrade head` picks these up with
  no separate step.
- **No new environment variables.** Phase 2 introduces no new required or
  recommended settings; `backend/ecc/config.py`'s `Settings` and
  `validate_production_settings` are unchanged. The full table in
  `PHASE-1-DEPLOYMENT.md` remains complete and current.
- **No new services.** Phase 2 has no embeddings/vector search component —
  Slice 7 (optional embeddings and hybrid fusion) is explicitly out of
  scope pending an RFC-005 amendment and ADR (see
  `docs/phases/phase-002/IMPLEMENTATION-STATUS.md`'s Prerequisites). Lexical
  retrieval (`GET /knowledge/retrieve`) runs entirely against
  `retrieval_documents`' `pg_trgm`/generated-`tsvector` columns inside the
  existing PostgreSQL instance — no new infrastructure to provision.
- **New frontend workspace tab** ("Knowledge") wired into the existing
  single-page app build; no new build steps, no new `VITE_*` variables, no
  change to `frontend/Dockerfile` or `frontend/nginx.conf.template`.
- **New rebuild CLI** for the two Phase 2 projection tables:

  ```bash
  # Rebuild timeline_entries and retrieval_documents for every workspace,
  # deterministically, from the authoritative tables they're derived from
  # (audit_events, pkos_nodes, knowledge_claims). Safe to re-run any time —
  # both projections are declared rebuildable in phase-002/DATA-MODEL.md.
  uv run python scripts/rebuild_knowledge_projections.py

  # Or scope it to one workspace:
  uv run python scripts/rebuild_knowledge_projections.py --workspace-id <UUID>
  ```

  There is no scheduled job that runs this automatically — the projection
  writers (`queue_timeline_entry`, `queue_retrieval_document`) keep both
  tables current on every entity/claim/relationship mutation. This CLI
  exists for recovery (e.g. after a restore that predates a mutation, or to
  regenerate after a manual data fix) and for the backup/restore isolation
  checks in `scripts/verify_restore.sh`, not as a routine deployment step.

## Optional: enabling embeddings and hybrid retrieval (Task 7)

Off by default in every deployment, including the shipped production image. Two separate opt-ins, both required:

1. **Build a custom backend image with the `embeddings` extra**, since `torch` (a `sentence-transformers` dependency) has no musl/Alpine wheels and `backend/Dockerfile`'s production image is deliberately Alpine (see ADR-0011):

   ```bash
   # Either switch the base image to a glibc distro (e.g. python:3.14.6-slim)
   # for this custom build, or otherwise ensure a glibc runtime, then:
   uv sync --frozen --extra embeddings
   ```

2. **Set `ECC_EMBEDDINGS_ENABLED=true`** on the running container (`Settings.embeddings_enabled`, `backend/ecc/config.py`) — without this, even a build that has the extra installed keeps `queue_embedding`/`GET /knowledge/retrieve?mode=hybrid` degrading to lexical-only, matching the default-off design at the runtime-config level.

The first real request after enabling pays a multi-second model-load cost (and, until cached, a Hugging Face Hub download for `sentence-transformers/all-MiniLM-L6-v2`) — expected, not a fault.

## Deploy

Follow `PHASE-1-DEPLOYMENT.md`'s "Deploy" section unchanged. The migration
step (`uv run alembic -c backend/alembic.ini upgrade head`) now also applies
`0010`-`0014` when deploying a ref that includes Phase 2; no separate
command is needed.

## Rollback

The same limitations documented in `PHASE-1-DEPLOYMENT.md`'s "Rollback"
section apply to `0010`-`0014`: each defines a `downgrade()`, but running
one against a database that has already taken production writes to
`timeline_entries`, `resolution_candidates`, `entity_operations`,
`retrieval_documents`, or the reconciled `pkos_*` columns is data-lossy, not
a safe default. Restore-from-backup remains the safe rollback path for any
Phase 2 migration that has taken production writes.

## Backup and restore

Unchanged mechanically (`scripts/backup.sh`, `scripts/restore.sh`,
`scripts/verify_restore.sh`) — Phase 2 tables are ordinary
`workspace_id`-scoped tables covered by the same `pg_dump --format=custom`
backup and the same generic workspace-isolation check as every Phase 1
table. `scripts/seed_phase1_acceptance.py`'s `_WORKSPACE_ID_TABLES` list has
been extended to include the new Phase 2 tables so the isolation check
continues to cover them.
