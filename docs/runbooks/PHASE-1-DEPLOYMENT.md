# Phase 1 Deployment Runbook

**Scope:** Executive Command Center Phase 1 (FastAPI backend, React frontend, PostgreSQL 18).
**Owner:** Lucky Jain (repository owner) — owns `ECC_SESSION_SECRET` and all production secret values named below. No other rotation owner is currently designated; a real production deployment MUST name one before secrets are issued.

Phase 0's technology boundary (`README.md`) defers cloud infrastructure and
Kubernetes; Phase 1 has no live hosted environment. The commands below are
the exact, runnable commands for building, migrating, running, smoke
checking, rolling back and recovering the application — usable today
against any host that can run Docker and reach a PostgreSQL 18 instance,
local or otherwise. They are not placeholders.

## Environment variables

| Variable | Required | Purpose | Production requirement |
| --- | --- | --- | --- |
| `ECC_ENV` | yes | Deployment classification (`development`, `staging`, `production`, ...). | Must be a recognized non-`development` value; `validate_production_settings` (`backend/ecc/config.py`) rejects an unrecognized or blank value outside development. |
| `ECC_DATABASE_URL` | yes | SQLAlchemy/psycopg connection string, e.g. `postgresql+psycopg://ecc:ecc@127.0.0.1:5432/ecc`. | Must point at the real production PostgreSQL 18 instance; never the default local value. |
| `ECC_SESSION_SECRET` | yes | Session/CSRF signing secret. | Must be at least 32 characters, cryptographically random, and not one of the recognized development placeholder strings — `validate_production_settings` rejects both a too-short value and a known placeholder outside development. Rotation owner: see above. |
| `ECC_CORS_ORIGINS` | yes | Comma-separated allowed browser origins. | Must be non-empty, must not contain a wildcard (`*`), and every origin must use `https://` outside development. |
| `POSTGRES_DB` / `POSTGRES_USER` / `POSTGRES_PASSWORD` | yes (for the `postgres` container/service) | Database provisioning credentials. | Must not be the `ecc`/`ecc`/`ecc` development defaults in any shared or production environment. |
| `ECC_METRICS_TOKEN` | recommended | Shared-secret token gating `GET /metrics`. | Optional, but strongly recommended in production: `/metrics` is a `GET` route and is intentionally outside `mutation_rate_limit_middleware`'s scope (it's meant to be scrape-friendly), and each scrape runs a live DB query. If unset, the endpoint stays open to anyone who can reach it -- **only acceptable if the port/route is firewalled off from the public internet** (e.g. restricted to an internal scrape network). If set, Prometheus/scrapers must send `Authorization: Bearer <token>`. |
| `ECC_TRUSTED_PROXY_COUNT` | conditionally required | Number of trusted reverse proxies/load balancers in front of `ecc-backend`. | Defaults to `0` (trust only the raw socket peer), which is correct for the direct `docker run -p 8000:8000` exposure shown above. This app does not terminate TLS itself, so any deployment that actually satisfies `ECC_CORS_ORIGINS`'s `https://`-only requirement puts a TLS-terminating reverse proxy or load balancer in front of it -- at that point **this must be set to the exact hop count** (usually `1`), or `mutation_rate_limit_middleware`'s per-IP ceiling collapses every distinct client into one shared bucket (they all arrive from the proxy's address) instead of limiting individual clients. See `backend/ecc/http_security.py`'s `_client_host`. |
| `VITE_API_BASE_URL` | yes (frontend build AND frontend run) | Backend origin the built frontend calls. | Must be the real backend origin reachable from the browser. Needed at **two** points with the same value: baked into the static JS bundle at `docker build` time (`frontend/Dockerfile`'s `build` stage `ARG`/`ENV`), AND passed again at `docker run` time so `frontend/nginx.conf.template` can render it into the production container's `Content-Security-Policy` `connect-src` directive (via the base nginx image's `envsubst`-on-templates entrypoint). Omitting it at run time does not fail the container start, but the CSP falls back to `connect-src 'self'` only — the browser will block every API call to a non-same-origin backend. |

All five backend variables are validated together by `validate_production_settings`,
called unconditionally at import time in `backend/ecc/main.py` — the
application refuses to start under an insecure production configuration
rather than starting and failing later (Task 7; `tests/test_production_security.py`).

## Deploy

Run from the repository root. Set `REF` to the git ref being deployed (a commit SHA or tag) before running the commands below.

```bash
REF="<commit-sha-or-tag>"
git fetch origin
git checkout "${REF}"

# 1. Build images at the deployed ref.
docker build -f backend/Dockerfile -t "ecc-backend:${REF}" .
docker build -f frontend/Dockerfile --target production \
  --build-arg VITE_API_BASE_URL="https://<production-backend-origin>" \
  -t "ecc-frontend:${REF}" .

# 2. Apply migrations against the target database before traffic is routed
#    to the new backend image. Requires ECC_DATABASE_URL exported.
uv run alembic -c backend/alembic.ini upgrade head

# 3. Start (or replace) the backend and frontend containers with the
#    environment variables above. Example using `docker run` directly
#    (a real environment MAY instead use docker-compose.yml or a
#    scheduler; the images and startup command are identical either way):
docker run -d --name ecc-backend --restart unless-stopped \
  -p 8000:8000 \
  -e ECC_ENV -e ECC_DATABASE_URL -e ECC_SESSION_SECRET -e ECC_CORS_ORIGINS \
  "ecc-backend:${REF}"

docker run -d --name ecc-frontend --restart unless-stopped \
  -p 80:80 \
  -e VITE_API_BASE_URL="https://<production-backend-origin>" \
  "ecc-frontend:${REF}"
```

`VITE_API_BASE_URL` must be passed here with the **same value** used in the
`docker build --build-arg` step above -- the build-time value is baked into
the JS bundle; this run-time value is rendered into the CSP `connect-src`
directive by `frontend/nginx.conf.template` at container start. A mismatch
(or omitting this flag) leaves the JS bundle calling a backend origin the
CSP doesn't allow, and the browser silently blocks every request.

`docker-compose.yml` remains the local-development entry point (`docker
compose up -d postgres` plus `pnpm --filter @ecc/frontend dev` per
`README.md`); it pins the frontend to the `dev` Vite-server target and does
not build the `production` nginx stage above, by design (Task 7).

## Migration

```bash
uv run alembic -c backend/alembic.ini current   # confirm the pre-deploy head
uv run alembic -c backend/alembic.ini upgrade head
uv run alembic -c backend/alembic.ini current   # confirm the new head
```

## Post-deployment smoke check

Run immediately after step 3 above, against the deployed backend origin:

```bash
BASE_URL="https://<production-backend-origin>"
curl -fsS "${BASE_URL}/health/live"    # {"status":"ok"}
curl -fsS "${BASE_URL}/health/ready"   # {"status":"ready"}; non-2xx if the database is unreachable
curl -fsS "${BASE_URL}/version"        # {"service":"ecc-backend","version":"..."}
```

All three endpoints are exercised by `tests/test_health.py` and
`tests/test_observability.py` (Task 8) and by `scripts/verify_restore.sh`'s
application-readiness check (Task 9), but no CI/CD pipeline runs this exact
curl sequence automatically against a live deployment today — see
`docs/runbooks/PHASE-1-RELEASE-GATE.md`'s "Post-deployment smoke checks are
automated" item, left open for this reason.

## Rollback

**Application rollback** (no schema change involved, or the new migration
is backward-compatible with the previous application version):

```bash
docker stop ecc-backend && docker rm ecc-backend
docker run -d --name ecc-backend --restart unless-stopped \
  -p 8000:8000 \
  -e ECC_ENV -e ECC_DATABASE_URL -e ECC_SESSION_SECRET -e ECC_CORS_ORIGINS \
  "ecc-backend:<PREVIOUS_REF>"
# repeat the equivalent docker run for ecc-frontend:<PREVIOUS_REF>
```

**Database migration rollback limitations — explicit.** Every Phase 1
migration under `backend/migrations/versions/` defines a `downgrade()`, and
`uv run alembic -c backend/alembic.ini downgrade -1` is mechanically
available. It is NOT a safe default rollback action for a real deployment:

- `downgrade()` functions `DROP TABLE` / `DROP INDEX` the objects their
  `upgrade()` created (e.g. `0002_phase1_task_foundation.py` drops
  `audit_events` and `idempotency_records`; `0004_phase1_notes.py` and
  `0007_phase1_search_indexes.py` drop generated search-document columns
  and their indexes). Any row written to those objects since the upgrade
  ran is unrecoverably deleted by the downgrade — this is real data loss,
  not a reversible schema tweak.
- No migration downgrade attempts to preserve or migrate data out of the
  structures it removes.
- Because of this, **the safe rollback path for any migration that has
  already taken production writes is restore-from-backup** (see Recovery
  below), not `alembic downgrade`. `alembic downgrade` is only appropriate
  immediately after `upgrade head` fails validation and before any new row
  has been written under the new schema.
- If the previous application version cannot run against the new schema
  and a backup restore is not yet warranted, the safest interim action is
  to keep the database at the new migration head and roll back only the
  application image to a version compatible with it, or take the
  application out of service until a compatible combination is restored.

## Backup

```bash
# Defaults to $ECC_DATABASE_URL / $DATABASE_URL, writes to .local/backups/,
# and emits the archive path on stdout plus a .sha256 checksum beside it.
SCHEMA_VERSION="$(uv run alembic -c backend/alembic.ini current | awk '{print $1}')" \
  ./scripts/backup.sh
```

**Format:** PostgreSQL custom-format logical archive (`pg_dump --format=custom
--no-owner --no-privileges`), SHA-256 checksum generated alongside it
(`scripts/backup.sh`).

**Retention policy:** retain the most recent 7 daily backups and the most
recent backup from each of the preceding 4 weeks (7 daily + 4 weekly,
matching the seven-day daily-use validation window this document's
companion runbook tracks); delete older archives and their `.sha256`
sidecars. No automated retention/pruning job exists yet — this is a
documented manual policy pending a scheduled job, consistent with Phase 1
having no live hosted environment yet to schedule a job against.

**Recovery point objective (RPO):** the most recent successful backup
(inherited from `docs/operations/PHASE-0-BACKUP-RESTORE.md`; Phase 1 has
not changed this — there is no continuous replication or WAL archiving in
Phase 1 scope).

**Recovery time objective (RTO):** 600 seconds (`config/phase1-acceptance.json`
`backup_restore.rto_seconds`), measured end-to-end by
`scripts/verify_restore.sh`'s `$SECONDS` check. Actually measured: 7
seconds wall-clock for a full backup+restore+verify cycle against real
PostgreSQL 18 in this environment on 2026-07-20 (see
`.superpowers/sdd/task-12-report.md`'s full-proof section for the live
re-run), and 24 seconds in Task 9's original drill
(`.superpowers/sdd/task-9-report.md`) — both well inside budget.

## Restore

```bash
# 1. Create a clean target database.
createdb -h <host> -U ecc ecc_restore

# 2. Restore the archive (verifies the .sha256 checksum first, aborts on mismatch).
RESTORE_DATABASE_URL="postgresql://ecc:ecc@<host>:5432/ecc_restore" \
  ./scripts/restore.sh "<path-to-archive>.dump"

# 3. Run the full restore verification (migration head, row counts,
#    constraints, representative-record checksums, workspace isolation,
#    audit append-only protection, PKOS mapped-column checksums,
#    lifecycle-field survival, search readiness, application readiness,
#    RTO budget — see Task 9).
RESTORE_DATABASE_URL="postgresql://ecc:ecc@<host>:5432/ecc_restore" \
  ./scripts/verify_restore.sh "<path-to-archive>.dump"

# 4. Generate a timestamped evidence report.
uv run python scripts/phase1_evidence.py \
  --source-url "postgresql://ecc:ecc@<host>:5432/ecc" \
  --target-url "postgresql://ecc:ecc@<host>:5432/ecc_restore" \
  --archive "<path-to-archive>.dump" \
  --elapsed-seconds "<measured-seconds>" \
  --output-json .local/evidence/phase1-recovery.json \
  --output-md .local/evidence/phase1-recovery.md
```

## Change ownership and review

Every deployment to a shared or production environment MUST be preceded by
a reviewed pull request (per `docs/CONTRIBUTING.md` and this branch's own
history — every task in `docs/superpowers/plans/2026-07-16-phase-1-completion.md`
was independently reviewed before being considered complete; see
`.superpowers/sdd/progress.md`). This runbook does not itself constitute
that review, and no deployment against this document satisfies Phase 1's
outstanding human change-review exit gate on its own.
