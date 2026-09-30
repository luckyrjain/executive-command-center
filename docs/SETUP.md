# Setup and Usage

This guide gets Executive Command Center running locally with PostgreSQL, FastAPI, React, and a development-only authenticated session.

## Prerequisites

- Docker with Compose
- Python 3.14
- `uv`
- Node.js 22
- `pnpm` 10.12.4
- [Ollama](https://ollama.com), running (`ollama serve`), for AI enrichment features (optional, off by default -- see step 3)

## Fast path

Run everything below in one command:

```bash
git clone https://github.com/luckyrjain/executive-command-center.git
cd executive-command-center
./scripts/quickstart.sh
```

`quickstart.sh` creates `.env` with a generated session secret (if `.env`
does not already exist), starts PostgreSQL, installs backend and frontend
dependencies, runs migrations, and creates a local development session. It
prints the two commands you still need to run yourself, one per terminal,
to start the backend and frontend dev servers, plus the one-time bootstrap
URL to open once both are running.

Re-run `./scripts/quickstart.sh` any time to get a fresh bootstrap URL or
pick up new dependencies/migrations; it is safe to run repeatedly. If it
fails partway through, or you want to understand or control each step, work
through the manual walkthrough below -- it performs the same steps
individually.

## Manual step-by-step

### 1. Configure the repository

```bash
git clone https://github.com/luckyrjain/executive-command-center.git
cd executive-command-center
cp .env.example .env
chmod 600 .env
```

`.env` holds `ECC_SESSION_SECRET` once you set it below -- restricting it to
your own user avoids leaving that secret world-readable on a shared host.

Generate a session secret and place it in `.env`:

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(48))'
```

Load the environment:

```bash
set -a
source .env
set +a
```

### 2. Start PostgreSQL and migrate

```bash
docker compose up -d postgres
uv sync --frozen --all-groups --python 3.14
uv run alembic -c backend/alembic.ini upgrade head
```

### 3. Create a local authenticated session

Phase 1 does not include a production login screen. Create or reuse the development workspace and user with:

```bash
uv run python scripts/bootstrap_dev.py
```

The bootstrap utility runs only when `ECC_ENV=development` and refuses non-local database hosts by default. Running it again reuses the existing local identity, revokes previous active sessions, and prints a fresh one-time URL that expires after 15 minutes.

It also checks, non-fatally, that every Ollama model the `model_definitions` catalog requires (Phase 4 AI Runtime) is pulled locally. To run that check on its own:

```bash
uv run python scripts/check_ollama_models.py
```

It reports any missing model with its exact `ollama pull` command. AI enrichment (meeting prep summaries, attention explanations, personal insights) is opt-in and off by default (`ECC_MEETING_PREP_AI_ENRICHMENT_ENABLED` and friends, `backend/ecc/config.py`) -- the deterministic core works with no Ollama install at all.

Optional embeddings and both AI-enrichment paths are explicitly disabled in `.env.example`. Phase 10 Gmail OAuth also remains inert until all `ECC_GMAIL_OAUTH_*` values are configured and the user is in the internal allowlist. Never place real secrets in `.env.example` or commit `.env`.

Gmail connector ownership and personal-data isolation are controlled by three settings (`backend/ecc/config.py`). `ECC_GMAIL_REVOKE_SCOPE` defaults to `global`: a Google grant is revoked only when no non-disconnected connector row in any workspace still uses that Google account; set it to `none` only once Google revocation is proven per-token. `ECC_PERSONAL_DATA_ISOLATION` (default `false`) is the rollout flag for making Gmail-derived rows private to the mailbox owner and non-shareable. `ECC_GMAIL_REQUIRE_IDENTITY_MATCH` (default `false`) is the rollout flag for making the Gmail OAuth callback refuse a Google account whose email differs from the connecting user's own. Settings are cached per process, so changing any of them requires a backend restart.

Personal-data isolation rollout notes (security remediation Spec A):

- Migration `0084_backfill_log_previous_state` adds a nullable JSONB column to the ops-only `personal_visibility_backfill_log` table: a metadata-only `ADD COLUMN`, instant, with no table rewrite. The backfill stores its per-row snapshot there, for the restore's exact check. The log holds ids, owners, versions, timestamps and a SHA-256 digest of member-written attention text (`override_reason`), never content or member-written text itself.
- Migration `0083_email_derived_target_idx` builds an index on `recommendations` without `CONCURRENTLY`. The migration ends with `ANALYZE recommendations`, so the new statistics are ready before member removal relies on them; no manual ANALYZE is needed. It runs as one transaction, so the index build's SHARE lock is held until it commits: writes to `recommendations` (recommendation generation, publish, confirm, dismiss) are blocked for the full-table index build plus the ANALYZE (about 6 s at 2.3M rows). Run it in a maintenance window on large deployments.
- The rule that makes tasks, commitments and risks created from email recommendations non-shareable and non-blocking for member removal (plan task FX3) recognises rows confirmed while `ECC_PERSONAL_DATA_ISOLATION` was off. Those rows stay workspace-visible until the personal-visibility backfill (`scripts/backfill_personal_visibility.py`) makes them private. The backfill handles these rows too (plan task FX2), so run it right after enabling the flag (below). With the flag off (the default), FX3 changes nothing.
- Backfill (rollout step R5, `scripts/backfill_personal_visibility.py`). Order:
  1. Enable `ECC_PERSONAL_DATA_ISOLATION` on the application and restart every process.
  2. Dry run and review its report: `PYTHONPATH=backend ECC_DATABASE_URL=<url> uv run python scripts/backfill_personal_visibility.py --dry-run`.
  3. Before the real run, review the dry run's `owner_inactive` rows and tell each confirming member which task, commitment or risk will leave their view. The query below maps the dry run's ids to what you need. The per-row remedy further down works only AFTER the real run (it hands a row the backfill already re-owned back to the confirming member); it cannot be applied in advance. Moving a row from an active confirming member to an inactive recommendation owner (review item A2) is signed off (2026-09-30): keep current behaviour.

     ```sql
     -- one query per table (<table>: tasks, commitments or risks), with that table's
     -- owner_inactive ids from the dry-run CSV; compare the row count with the CSV
     SELECT '<table>' AS table_name, r.workspace_id, r.id AS row_id,
            COALESCE(to_jsonb(r) ->> 'title', to_jsonb(r) ->> 'summary', to_jsonb(r) ->> 'description') AS title,
            ca.email AS confirming_member_email, ra.email AS recommendation_owner_email
     FROM <table> r
     JOIN recommendations rec ON rec.workspace_id = r.workspace_id
      AND rec.recommendation_type = 'email_action_detected'
      AND rec.execution_result ->> 'operation' = 'create'
      AND rec.execution_result ->> 'target_id' = r.id::text
     JOIN users cu ON cu.id = r.created_by JOIN accounts ca ON ca.id = cu.account_id
     JOIN users ru ON ru.id = rec.owner_id JOIN accounts ra ON ra.id = ru.account_id
     WHERE r.id = ANY('{<id>,<id>}'::uuid[]);  -- ids are globally unique
     ```
  4. Real run (it refuses to start without the flag): `PYTHONPATH=backend ECC_DATABASE_URL=<url> ECC_PERSONAL_DATA_ISOLATION=true uv run python scripts/backfill_personal_visibility.py`. Re-run it after any later re-enable of the flag. Every run prints its run id first.
  5. Rebuild the knowledge projections with the flag, one workspace at a time (below).

  Duration and locking: one short transaction per batch of 500 rows, holding the workspace's shared membership lock (for tasks, commitments and risks also the attention-regenerate lock, so `POST /attention/regenerate` waits for the batch), the batch's grant locks and then its row locks (a grant revoke's own order, so a revoke racing a batch waits instead of deadlocking), with `lock_timeout` 5 s and `statement_timeout` 120 s. Measured durations: 457.7 s (7.6 min) for 720k recommendations and 520k derived rows, and 5 min 50 s for 300k recommendations and 200k derived rows with three earlier runs in the log under heavy machine load. Plan for roughly 11 to 20 minutes per million recommendations (about 6 minutes per million recommendations plus derived rows on an idle machine), and run it in a quiet period. App writes to a row in the current batch wait for that batch. A batch that fails with a deadlock, lock timeout or statement timeout is rolled back and retried from the same place up to four times (0.5 to 4 s backoff); after that the run stops with exit 2, and a re-run continues where it stopped.

- Backfill reasons and exit codes (the single reference; the CSV `reason` column). Exit codes: backfill and `--dry-run` 0 = nothing reported, 1 = rows reported, 2 = error or refusal (flag guard, unknown workspace, a table without a rule, or a database error after the retries; committed batches stay committed and are logged under the printed run id); `--restore` 0 = every logged row in scope restored, 1 = rows reported, 2 = error or refusal. The reasons marked "every run" keep being reported while they hold, so exit 1 whose rows you have all reviewed and accepted is the steady state; compare each run's list with the previous one.

  | Reason | Meaning | Operator action | Reported |
  | --- | --- | --- | --- |
  | `evidence_owner_ambiguous` / `evidence_owner_unknown` / `evidence_source_ref_unrecognised` | Gmail evidence whose mailbox owner cannot be resolved; left as written, grants revoked | Re-own and make private manually, or accept | every run |
  | `alias_owner_inactive` | A Gmail-derived alias would move to a member who is not active | None (removal re-owned it); for a suspended member, re-run after reactivation | every run |
  | `owner_not_mailbox_owner` | An email recommendation / AI run (and its steps), or a task/commitment/risk created from such a recommendation, is owned by someone other than the mailbox owner; made private with its current owner | Fix the owner via SQL, or accept. A row whose cited evidence or thread belongs to another owner stays reported even after the fix: accepting does not clear it | every run |
  | `connector_owner_unknown` | Sync run/cursor whose connector owner could not be read (not reachable today) | Fix the owner via SQL | every run |
  | `derived_owner_changed` | A task/commitment/risk created from an email recommendation is owned by someone the rule would not assign: re-owned since the confirm, or since a run applied the rule (for example the `owner_inactive` remedy below). Made private with its current owner; that owner sticks | Confirm the owner should keep it, or change the owner via SQL: with the flag on the app refuses transfers of email-derived rows | every run |
  | `owner_inactive` | A task/commitment/risk re-owned by a backfill run to its recommendation's owner, who is now not an active member (removed or suspended before or after that run; see below) | See below | every run while it holds (rebuilt from the log, so a failed run loses nothing) |
  | `derived_source_missing` | A derived row whose source recommendation could not be read (not reachable through the app); only grants revoked | Investigate | every run |
  | `changed_since_backfill:rule` / `:owner` / `:visibility` / `:content` / `:attention` (restore) | The row no longer holds what the run left. `rule`: the backfill's rules no longer yield that value (reclassified, no longer resolvable). `owner` / `visibility`: changed since (transfer, re-share). `content`: a task, commitment or risk was edited since (its `version` or `updated_at` differs from the log's snapshot). `attention`: an attention item's member fields (`dismissed_at`, `deferred_until`, `override_reason`) differ from the snapshot -- usually a member dismissed, deferred or restored it since, but also a regenerate that re-scored the item after a content change of its entity (which clears a dismissal), or an item replaced since | Expected; restore manually (SQL below) only after checking the change should be undone | per restore |
  | `changed_since_backfill:no_snapshot` (restore) | A log row written before migration 0084 on a table that needs its snapshot (tasks, commitments, risks, email attention items): it cannot be verified, so it is not restored | Restore by hand (SQL below) after checking the row | per restore |
  | `previous_owner_inactive` (restore) | The logged previous owner is not an active member | Expected; pick an owner manually if needed | per restore |
  | `no_longer_personal` (restore) | The row is no longer in the personal data set | Expected | per restore |
  | `deleted` (restore) | The row no longer exists (listed even under `--workspace-id`) | Expected | per restore |

- `owner_inactive` means: a backfill run re-owned a task, commitment or risk to its email recommendation's owner, who is now not an active member (removed or suspended before or after that run); typically another member had confirmed the recommendation while the flag was off. The backfill makes that row private to that member, like the rest of that member's personal data. The confirming member loses it: it disappears from their lists, search and attention, they can no longer edit it, grants on it are revoked, and with the flag on the app refuses to transfer it back (email-derived rows are share-refused). A suspended member sees it again on reactivation. The one safe per-row remedy, if the confirming member must keep the row, is an owner change in SQL that is also recorded as an ownership transfer (the same `ownership_transfers` row the app writes for a transfer; there is no dedicated audit event type, so also note it in the ops log). Run it in one transaction, taking the same locks as the tool (membership, then attention-regenerate), with `<table>` one of `tasks`, `commitments`, `risks`:

  ```sql
  BEGIN;
  SELECT pg_advisory_xact_lock_shared(hashtextextended('membership-mutation:<workspace_id>', 0));
  SELECT pg_advisory_xact_lock(hashtextextended('attention-regenerate:<workspace_id>', 0));
  INSERT INTO ownership_transfers (id, workspace_id, resource_type, resource_id, from_account_id,
      to_account_id, status, initiated_by, created_at, completed_at)
  SELECT gen_random_uuid(), r.workspace_id, '<table>', r.id, fu.account_id, tu.account_id,
      'completed', '<operator users.id in that workspace>', now(), now()
  FROM <table> r JOIN users fu ON fu.id = r.owner_id JOIN users tu ON tu.id = r.created_by
  WHERE r.workspace_id = '<workspace_id>' AND r.id = '<row_id>';
  UPDATE <table> SET owner_id = created_by, version = version + 1
  WHERE workspace_id = '<workspace_id>' AND id = '<row_id>';
  -- its attention items follow the row, keeping a dismissal across the version bump
  -- (<entity_type>: task / commitment / risk):
  UPDATE attention_items SET owner_id = r.owner_id, visibility = r.visibility,
      source_entity_version = CASE WHEN (attention_items.dismissed_entity_version = attention_items.source_entity_version
             AND attention_items.source_entity_version = r.version - 1)
          THEN r.version ELSE attention_items.source_entity_version END,
      dismissed_entity_version = CASE WHEN (attention_items.dismissed_entity_version = attention_items.source_entity_version
             AND attention_items.source_entity_version = r.version - 1)
          THEN r.version ELSE attention_items.dismissed_entity_version END
  FROM <table> r WHERE r.workspace_id = '<workspace_id>' AND r.id = '<row_id>'
    AND attention_items.workspace_id = r.workspace_id
    AND attention_items.entity_type = '<entity_type>' AND attention_items.entity_id = r.id;
  COMMIT;
  ```

  The row stays private. Later runs keep the new owner and report the row `derived_owner_changed`, and a `--restore` leaves it alone (`changed_since_backfill:owner`).

- Restore (rollback): turn the flag off on the application and restart it, then restore EVERY backfill run id, newest first (`SELECT run_id, min(at) FROM personal_visibility_backfill_log GROUP BY 1 ORDER BY 2 DESC;`, then `PYTHONPATH=backend ECC_DATABASE_URL=<url> uv run python scripts/backfill_personal_visibility.py --restore <run_id>` for each). Restore refuses (exit 2) while `ECC_PERSONAL_DATA_ISOLATION` is enabled in its own environment or in the application's settings (a `.env` file included), unless given `--allow-with-isolation` (it then warns: the application keeps writing these rows private while the restore republishes the old ones); the refusal names where the flag came from. It is compare-and-set, exact and clock-free: a row is restored only while it still holds what the run left -- the owner and visibility the rules give, and (migration 0084's `previous_state` snapshot) for tasks, commitments and risks `version` equal to the snapshot's plus the backfill's own bump and `updated_at` unchanged, and for attention items (their own, or a task's) the member fields `override_reason` (compared by digest), `dismissed_at`, `deferred_until` unchanged. Timestamps are written in UTC and compared parsed, so the operator's `PGTZ` does not matter. The backfill and the restore carry an item's dismissal across the `version` bump they make, so a later regenerate keeps it. Rows changed since stay as they are and are reported with the cause. Log rows written before migration 0084 have no snapshot: on tasks, commitments, risks and email attention items they are refused (`changed_since_backfill:no_snapshot`); other tables get the owner/visibility check only. A restored row keeps its pre-backfill `updated_at` (the backfill never changes it). Each page of the log is one transaction, retried on a deadlock, lock timeout or statement timeout like a backfill batch. Exit 1 with `previous_owner_inactive`, `no_longer_personal`, `deleted` or `changed_since_backfill:*` rows is the expected outcome. To restore a reported row by hand: first read its log row and compare `previous_state` with the row now. **Warning: restoring publishes whatever was written since the backfill** -- the row's content and its attention items' text (`override_reason`) become visible to the previous owner and, for `workspace`, to everyone; review the row, and clear `override_reason` first where needed. Then, in one transaction taking the workspace's membership lock and then the attention-regenerate lock, like the tool does (`<entity_type>` is task / commitment / risk; skip the attention statement for other tables):

  ```sql
  SELECT table_name, row_id, previous_owner_id, previous_visibility, previous_state
  FROM personal_visibility_backfill_log WHERE run_id = '<run_id>' AND row_id = '<row_id>';
  BEGIN;
  SELECT pg_advisory_xact_lock_shared(hashtextextended('membership-mutation:<workspace_id>', 0));
  SELECT pg_advisory_xact_lock(hashtextextended('attention-regenerate:<workspace_id>', 0));
  -- tables WITHOUT a version column:
  UPDATE <table> SET owner_id = '<previous_owner_id>', visibility = '<previous_visibility>'
  WHERE workspace_id = '<workspace_id>' AND id = '<row_id>';
  -- OR, for versioned tables (tasks, commitments, risks, recommendations,
  -- connector_accounts, entity_aliases), bump the version too:
  UPDATE <table> SET owner_id = '<previous_owner_id>', visibility = '<previous_visibility>',
      version = version + 1
  WHERE workspace_id = '<workspace_id>' AND id = '<row_id>';
  -- its attention items follow the row, keeping a dismissal across the version bump
  -- (<entity_type>: task / commitment / risk):
  UPDATE attention_items SET owner_id = r.owner_id, visibility = r.visibility,
      source_entity_version = CASE WHEN (attention_items.dismissed_entity_version = attention_items.source_entity_version
             AND attention_items.source_entity_version = r.version - 1)
          THEN r.version ELSE attention_items.source_entity_version END,
      dismissed_entity_version = CASE WHEN (attention_items.dismissed_entity_version = attention_items.source_entity_version
             AND attention_items.source_entity_version = r.version - 1)
          THEN r.version ELSE attention_items.dismissed_entity_version END
  FROM <table> r WHERE r.workspace_id = '<workspace_id>' AND r.id = '<row_id>'
    AND attention_items.workspace_id = r.workspace_id
    AND attention_items.entity_type = '<entity_type>' AND attention_items.entity_id = r.id;
  COMMIT;
  ```

  The attention items of a restored task, commitment or risk follow it. Revoked grants are NOT restored. To list the grants a run revoked (the backfill revokes in the same transaction that writes the log row, so `revoked_at` equals the log's `at`):

  ```sql
  SELECT g.id, g.resource_type, g.resource_id, g.grantee_account_id, g.actions
  FROM personal_visibility_backfill_log l
  JOIN resource_grants g ON g.resource_type = l.table_name AND g.resource_id = l.row_id
   AND g.revoked_at = l.at
  WHERE l.run_id = '<run_id>' AND l.grants_revoked > 0;
  ```

- Rebuild (`scripts/rebuild_knowledge_projections.py`): after a backfill, run it with the flag, one workspace at a time: `PYTHONPATH=backend ECC_DATABASE_URL=<url> ECC_PERSONAL_DATA_ISOLATION=true uv run python scripts/rebuild_knowledge_projections.py --workspace-id <UUID>`. It prints the effective database (host and name) and whether the flag is on, and where it came from (environment, `.env` file, or default). With the flag off it refuses (exit 2) while any `pkos_evidence` row in scope (the `--workspace-id` workspace, else every workspace) is `private`, listing the workspaces with counts; nothing else triggers the refusal (not the backfill log, not narrowed `shared_explicitly` evidence, which a flag-off application already puts in shared search text). The check is one read at the start of the rebuild's own transaction, before any write, with a 120 s statement timeout; unscoped, it scans `pkos_evidence` once. After a full rollback, the evidence the restores put back is `workspace` again. Evidence stays `private`, and keeps the refusal on, where a restore reported the row instead of restoring it (`previous_owner_inactive`, `changed_since_backfill:*`) and where it was written while the flag was on (for example Gmail evidence synced as `private`; `--restore` never touches it). The refusal lists those workspaces with counts; `SELECT id, source_type, owner_id FROM pkos_evidence WHERE workspace_id = '<ws>' AND visibility = 'private'` lists the rows, and the restores' CSV lists the reported ones. Then either re-enable the flag, or pass `--allow-without-isolation` knowing that claims backed by that evidence become shared search text (the flag-off application's own writers would put them there on their next write anyway). See also `docs/runbooks/PHASE-2-DEPLOYMENT.md`.

Start the backend, then open the printed URL. The URL carries the one-time code in its fragment so it is not sent in HTTP access logs. The backend rotates the code into an opaque `HttpOnly`, `SameSite=Lax` session cookie with a seven-day absolute lifetime, sets the readable CSRF cookie, and redirects to the frontend.

For an isolated remote development database only, explicitly set:

```bash
export ECC_BOOTSTRAP_ALLOW_REMOTE_DATABASE=1
```

Never enable this override for staging or production data.

### 4. Start the backend

```bash
uv run uvicorn ecc.main:app --app-dir backend --reload --host 127.0.0.1 --port 8000
```

Verify:

```bash
curl http://localhost:8000/health/live
curl http://localhost:8000/health/ready
```

API documentation is available at `http://localhost:8000/docs`.

### 5. Start the frontend

In another terminal:

```bash
corepack enable
corepack prepare pnpm@10.12.4 --activate
pnpm install --frozen-lockfile
pnpm --filter @ecc/frontend dev
```

Open the one-time bootstrap URL printed by `scripts/bootstrap_dev.py`. After the secure cookie exchange, the backend redirects to `http://localhost:5173`.

## What is available

- Today dashboard
- Morning Brief
- recommendations and confirmation
- global Search
- immutable Audit history
- Phase 1 task, commitment, note, calendar, meeting, risk, and attention APIs
- Phase 8 collaboration workspace: workspace switcher, members/invitations, sharing review, delegation inbox, and shared activity feed (`frontend/src/features/collaboration/`) -- reachable once you have a real account (see "Registration and login" below); the one-time bootstrap flow above remains the fastest way to get a local session for everyday development
- Phase 10 Tasks 1–2: internal-allowlist Gmail OAuth plus manual 30-day metadata backfill/incremental sync and person-entity linking; message bodies, attention/recommendation/AI integration, consent-revocation cascade, and Gmail frontend remain unimplemented

## Tests and quality gates

`make check` and `make test` (see `Makefile`) cover ruff/mypy/pytest/frontend
lint+typecheck+test -- the fast, no-extra-tooling subset of what CI runs on
every PR. The remaining CI gates, run individually below, need either a
package not in the default install (`pip-audit`, Playwright's browser) or
external CLI tools most local setups won't have -- so they're not folded
into `make check`/`make test`.

Backend:

```bash
make docs-check
uv run ruff check backend tests
uv run ruff format --check backend tests
uv run mypy backend
uv run pytest
uv run pip-audit
python scripts/check_phase3_prohibited_signals.py
```

Frontend:

```bash
pnpm --filter @ecc/frontend typecheck
pnpm --filter @ecc/frontend test -- --run
pnpm --filter @ecc/frontend build
pnpm audit --audit-level=high
pnpm --filter @ecc/frontend exec playwright install --with-deps chromium
pnpm --filter @ecc/frontend test:e2e
```

CI additionally runs an `embeddings-benchmark` job (`uv sync --extra
embeddings`, then `tests/test_knowledge_embeddings_postgres.py` and
`tests/test_knowledge_retrieval_benchmark_postgres.py` -- the `embeddings`
extra pulls in `sentence-transformers`/`torch`, ~1-2GB, so it's isolated
from the default `backend` job rather than folded into `uv run pytest`
above), a `containers` job (`docker build` of both `backend/Dockerfile` and
`frontend/Dockerfile`, a boot smoke test of each, then a Trivy **image**
scan), and a separate `security` job (a Gitleaks secret scan, an SBOM
export via `anchore/sbom-action`, and a Trivy **filesystem** scan of the
repository -- not the same scan as the `containers` job's). These need
Docker and the `trivy`/`gitleaks` CLIs respectively; see
`.github/workflows/ci.yml` for the exact invocations if you need to
reproduce one locally -- they're intentionally not Makefile targets since
most contributors won't have that tooling installed by default.

## Docker Compose

To build the whole stack:

```bash
docker compose up --build
```

The services listen on:

- frontend: `http://localhost:5173`
- backend: `http://localhost:8000`
- PostgreSQL: `localhost:5432`

Migrations and the development identity still need to be created explicitly. The local-process workflow above is recommended during active development.

## Reset local data

```bash
docker compose down -v
docker compose up -d postgres
uv run alembic -c backend/alembic.ini upgrade head
uv run python scripts/bootstrap_dev.py
```

## Troubleshooting

### `ECC_SESSION_SECRET` validation error

Use a value with at least 32 characters and reload `.env` into the shell.

### Bootstrap refuses the environment or database

Confirm `ECC_ENV=development` and that `ECC_DATABASE_URL` points to `localhost`, `127.0.0.1`, or `::1`. Use the remote-development override only for an isolated non-production database.

### Bootstrap code is invalid or expired

Run `scripts/bootstrap_dev.py` again and open the newly printed URL within 15 minutes. Generating a new code revokes the previous active session.

### `401 Authentication required`

Run `scripts/bootstrap_dev.py` again and complete the one-time browser exchange. Use `localhost` consistently in browser URLs.

### `403 CSRF_TOKEN_REQUIRED` or `CSRF_TOKEN_INVALID`

Complete the bootstrap exchange again. The CSRF cookie is tied to the generated session and current session secret.

### Database connection failure

```bash
docker compose ps
docker compose logs postgres
```

Confirm `ECC_DATABASE_URL` matches the Compose credentials.

### Frontend cannot reach the backend

Check `http://localhost:8000/health/ready`, confirm `VITE_API_BASE_URL`, and restart Vite after environment changes.

## Current limitations

- Production registration and login are real and implemented (Phase 8 account/membership/session framework) rather than absent, but the one-time bootstrap flow above is still the fastest way to get a local development session and is what these instructions default to.
- The bootstrap utility and `/dev/bootstrap` exchange are development-only.
- GitHub, GitLab, Jira, Datadog, and the Phase 10 Gmail metadata connector have implemented development paths; there is no Google Calendar connector, connector scheduler, or push sync.
- Gmail access is internal-allowlist only. Tasks 3–8 are open, including body fetch, attention/recommendation integration, consent-revocation cascade, and executive UX.
- Production provisioning, account recovery/MFA, key rotation, real connector recovery, personal-data recovery, and automated post-deploy smoke remain blockers; see [Production Readiness](operations/PRODUCTION-READINESS.md).
- AI enrichment is optional and disabled by default; deterministic features remain available.
