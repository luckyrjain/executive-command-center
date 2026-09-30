---
id: PHASE-2-DEPLOYMENT
title: Phase 2 Deployment Runbook
status: Active
version: 1.4.0
owner: Lucky Jain
---

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
- **Migrations `0015`-`0021`, shipped after this section was first written, also belong to Phase 2** and are picked up by the same `upgrade head` call: `0015_phase2_embeddings.py` (adds `pgvector`/the `vector` extension and embedding columns — see "Embeddings deployment" below, the reason this environment specifically needs the `pgvector/pgvector:pg18` image, not plain `postgres:18`), `0016_phase2_require_evidence.py`, `0017_phase2_resolution_defer.py`, `0018_phase2_split_operation.py`, `0019_phase2_mutable_versioning.py`, `0020_phase2_drop_dead_entity_id.py`, and `0021_phase2_drop_observed_at.py` (schema-correction migrations refining the `0010`-`0014` tables above, not new tables of their own). This list was previously undercounted at `0010`-`0014` only.
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
  # both projections are declared rebuildable in phase-002/DATA-MODEL.md —
  # as long as the isolation flag matches the application's: when the
  # application runs with ECC_PERSONAL_DATA_ISOLATION on, add
  # ECC_PERSONAL_DATA_ISOLATION=true here too. With the flag off the command
  # refuses (exit 2) while `private` evidence exists in scope (see
  # "Personal-data isolation" below). Each invocation is one transaction, so
  # on large deployments prefer one workspace at a time.
  PYTHONPATH=backend ECC_DATABASE_URL=<url> \
      uv run python scripts/rebuild_knowledge_projections.py

  # Or scope it to one workspace:
  PYTHONPATH=backend ECC_DATABASE_URL=<url> \
      uv run python scripts/rebuild_knowledge_projections.py --workspace-id <UUID>
  ```

  It prints the effective database (host and name) and whether
  `ECC_PERSONAL_DATA_ISOLATION` is on and where it came from (environment,
  `.env` file, or default) on stderr. Exit codes: 0 rebuilt; 2 refused by the
  isolation guard, invalid arguments, or unknown workspace; 1 unexpected
  error.

  There is no scheduled job that runs this automatically — the projection
  writers (`queue_timeline_entry`, `queue_retrieval_document`) keep both
  tables current on every entity/claim/relationship mutation. This CLI
  exists for recovery (e.g. after a restore that predates a mutation, or to
  regenerate after a manual data fix) and for the backup/restore isolation
  checks in `scripts/verify_restore.sh`, not as a routine deployment step.

  **Personal-data isolation (security remediation Spec A).** With the flag
  off, `retrieval._build_body` copies every claim into shared search text,
  including claims backed by evidence that is `private` only because the
  flag was on (Gmail evidence written with the flag on, or evidence the
  personal-visibility backfill made private). So with the flag off the
  rebuild refuses (exit 2) while any `pkos_evidence` row in scope (the
  `--workspace-id` workspace, else every workspace) is `private`, and lists
  the workspaces with counts (top 10 plus the total). Nothing else triggers
  it: not the backfill log, and not narrowed (`shared_explicitly`) evidence,
  which a flag-off application already puts in shared search text. The
  check is one read at the start of the rebuild's own transaction (before
  any write) with a 120 s statement timeout; unscoped, it scans
  `pkos_evidence` once. `--allow-without-isolation` overrides it with a
  warning.

  Order at rollout step R5 (details, durations, every backfill reason and
  exit code: `docs/SETUP.md`, "Personal-data isolation rollout notes"):

  1. Enable `ECC_PERSONAL_DATA_ISOLATION` on the application and restart
     every process.
  2. Dry-run the backfill and review its report:

     ```bash
     PYTHONPATH=backend ECC_DATABASE_URL=<url> \
         uv run python scripts/backfill_personal_visibility.py --dry-run
     ```

  3. Review the dry run's `owner_inactive` rows: for each, tell the
     confirming member (the row's `created_by`) which task, commitment or
     risk will leave their view (`docs/SETUP.md` has the query mapping the
     ids to titles and email addresses). The per-row remedy in
     `docs/SETUP.md` works only AFTER the real run; it cannot be applied in
     advance. Moving a row from an active confirming member to an inactive
     recommendation owner (review item A2) is signed off (2026-09-30):
     keep current behaviour.
  4. Run the backfill (it refuses to run without the flag). Measured: 457.7 s
     for 720k recommendations and 520k derived rows; 5 min 50 s for 300k
     recommendations and 200k derived rows with three earlier runs, under
     heavy machine load. Plan for roughly 11 to 20 minutes per million
     recommendations (about 6 minutes per million recommendations plus
     derived rows on an idle machine). Batches that hit a deadlock, lock timeout
     (`lock_timeout` 5 s) or statement timeout (120 s) are retried up to
     four times; after that the run stops with exit 2 and a re-run
     continues.

     ```bash
     PYTHONPATH=backend ECC_DATABASE_URL=<url> ECC_PERSONAL_DATA_ISOLATION=true \
         uv run python scripts/backfill_personal_visibility.py
     ```

  5. Rebuild, one workspace at a time, with the flag:

     ```bash
     PYTHONPATH=backend ECC_DATABASE_URL=<url> ECC_PERSONAL_DATA_ISOLATION=true \
         uv run python scripts/rebuild_knowledge_projections.py --workspace-id <UUID>
     ```

  Rollback: turn the flag off on the application (and restart), then
  restore EVERY backfill run id, newest first
  (`SELECT run_id, min(at) FROM personal_visibility_backfill_log GROUP BY 1
  ORDER BY 2 DESC;`, then
  `PYTHONPATH=backend ECC_DATABASE_URL=<url> uv run python scripts/backfill_personal_visibility.py --restore <run_id>`
  for each; it refuses while the flag is on in its environment or in the
  application's settings, `.env` included, unless given
  `--allow-with-isolation`). The restore is an exact compare-and-set on
  migration 0084's snapshot (rows edited or acted on since are reported
  `changed_since_backfill:<cause>`), and each log page is retried like a
  backfill batch. Revoked grants are not restored; `docs/SETUP.md`
  has the SQL that lists them. Then rebuild with the flag off: the evidence
  the restores put back is `workspace` again. Evidence stays `private`, and
  keeps the guard on, where a restore reported the row instead of restoring
  it (`previous_owner_inactive`, `changed_since_backfill:*`) and where it was
  written while the flag was on (restore never touches it). The guard's
  refusal lists those workspaces with counts; `SELECT id, source_type,
  owner_id FROM pkos_evidence WHERE workspace_id = '<ws>' AND visibility =
  'private'` lists the rows. Then re-enable the flag, or pass
  `--allow-without-isolation` knowing that claims backed by that evidence
  become shared search text.

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
