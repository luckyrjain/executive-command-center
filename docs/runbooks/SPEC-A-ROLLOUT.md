---
id: SPEC-A-ROLLOUT
title: Security Remediation Spec A Rollout Runbook
status: Active
version: 1.4.0
owner: Lucky Jain
created: 2026-10-01
updated: 2026-10-01
depends_on:
  - PHASE-10-GMAIL-RECOVERY
  - SPEC-A-ALERTS
---

# Security Remediation Spec A rollout runbook (R1–R7)

This is the operator sequence for turning on connector ownership and personal-data isolation (security remediation Spec A, version 5.2.1; see `docs/phases/phase-010/IMPLEMENTATION-STATUS.md` for where the spec lives). Run it once per environment, in order. Each step has its own gate. Do not start a step until the previous one's gate holds.

It is a separate runbook rather than a section of [`PHASE-10-GMAIL-RECOVERY.md`](PHASE-10-GMAIL-RECOVERY.md) for two reasons. That runbook covers ongoing Gmail operations; this one is a one-time, ordered rollout that also touches member removal, sharing, search, meeting prep and the knowledge projections. And this one closes when the flags are removed (plan task T20), while the Gmail runbook stays. Detailed procedures that already exist are linked, not copied: the backfill, its reasons, the restore and the rebuild are in [`docs/SETUP.md`](../SETUP.md) and [`PHASE-2-DEPLOYMENT.md`](PHASE-2-DEPLOYMENT.md), and the remediation command documents itself (`scripts/remediate_connector_ownership.py`).

## Where things stand (2026-10-01)

- On `main`: every Spec A code task except the frontend (T17) and flag removal (T20), plus fix wave FX1–FX6 (PRs #282–#312). The three settings default to their safe values, so deploying `main` changes nothing that is flag-gated.
- **Unflagged changes are live as soon as `main` is deployed.** Callers and the frontend see them before any flag is on:
  - new error codes: 409 `CONNECTOR_OWNED_BY_ANOTHER_MEMBER`, 403 `MEMBERSHIP_INACTIVE`, 500 `CONNECTOR_ACCOUNT_PERSIST_FAILED`, 404/403 on engineering reactivation, 422 on `POST /recommendations` for `email_action_detected`, 403 `EMAIL_CONSENT_NOT_ACTIVE` on Gmail sync and email confirm;
  - search filtered by visibility;
  - meeting packs that store only workspace-visible sources;
  - revokes guarded and truthfully counted.
  
  See [`../phases/phase-010/API-SCHEMAS.md`](../phases/phase-010/API-SCHEMAS.md#security-remediation-spec-a). Until T17 ships, the Gmail panel shows some of these codes raw.
- Sign-offs: only **A2** is signed (see [Sign-off checklist](#sign-off-checklist)). R5 cannot start until the rest are.

## Flags and processes

| Setting | Default | Turned on at | Gates |
|---|---|---|---|
| `ECC_GMAIL_REVOKE_SCOPE` | `global` | R2, set to `none` only if D2 proves per-token revocation | `revoke_is_safe`: under `global` a Google grant is revoked only when no non-disconnected row in any workspace uses that Google account |
| `ECC_PERSONAL_DATA_ISOLATION` | `false` | R5 | Private writes for Gmail rows and Gmail-derived content; share/transfer/delegation refusal; removal not blocked by personal rows; non-owner sync/disable → 404; evidence readers filter visibility; backfill and rebuild guards |
| `ECC_GMAIL_REQUIRE_IDENTITY_MATCH` | `false` | R6 | Gmail callback refuses a Google account whose email differs from the member's ECC email (403 `GMAIL_ACCOUNT_IDENTITY_MISMATCH`) |

An invalid value fails at startup.

**Every flag change needs a restart of every process.** `get_settings()` is `@lru_cache`d per process (`backend/ecc/config.py`), so a process that is not restarted keeps the old value. With isolation, that means it keeps writing new Gmail rows `workspace`-visible. Restart all of these:

- every API worker and replica (`uvicorn ecc.main:app`);
- `scripts/run_automation_worker.py`;
- nothing else: ops scripts (`backfill_personal_visibility.py`, `rebuild_knowledge_projections.py`, the audit and remediation commands) are one-off processes that read their own environment and `.env` when they start. The backfill and the rebuild check the flag in **their own** environment, so pass it on their command line as shown in SETUP.md.

Prometheus counters reset on every restart (see [Monitoring](#monitoring)).

**`docker-compose.yml` passes no Spec A setting.** The `backend` service sets only `ECC_ENV`, `ECC_DATABASE_URL`, `ECC_SESSION_SECRET`, `ECC_CORS_ORIGINS` and the Postgres variables. The image copies only `backend/` (no `.env`), and the container is `read_only`. The stock compose stack therefore always runs with the defaults. It also has no `ECC_GMAIL_OAUTH_*` or encryption keys, so Gmail cannot run under it at all. This runbook does not add a passthrough: compose is the local full-stack path, and a production deployment must supply these values from its own configuration.

To change a flag under compose, add it to the `backend` service's `environment:`, for example in a `docker-compose.override.yml`:

```yaml
services:
  backend:
    environment:
      ECC_PERSONAL_DATA_ISOLATION: "true"
```

Then run `docker compose up -d backend`. `docker compose restart` does **not** re-read `environment:`; the container must be recreated. Confirm the value from the process itself, not from the file. The backfill and rebuild print where they found the flag, and the backfill's real run refuses (exit 2) without it.

## R1: deploy (flags off)

1. Deploy `main`. The `migrate` step (`alembic upgrade head`) applies the three Spec A migrations:
   - `0082_revoke_idx_backfill_log`: a small index on `connector_accounts` plus the ops-only `personal_visibility_backfill_log` table.
   - `0083_email_derived_target_idx`: an index and extended statistics on `recommendations`, then `ANALYZE`. It is built without `CONCURRENTLY` inside the migration's transaction, so writes to `recommendations` (generate, publish, confirm, dismiss) are blocked for the build. That took about 6 s at 2.3M rows. **It sets no `lock_timeout`.** If a long write transaction holds `recommendations`, the migration queues behind it, and every later write queues behind the migration.
   - `0084_backfill_log_previous_state`: a metadata-only `ADD COLUMN`, instant.
   
   **Run this deploy in a maintenance window** on any deployment with a large `recommendations` table, and check `pg_stat_activity` for long transactions first. To bound the wait instead of queueing, run the migration with a lock timeout, for example `PGOPTIONS="-c lock_timeout=10s" alembic -c backend/alembic.ini upgrade head`. On a timeout the migration rolls back, so retry it. 0083 and 0084 must be applied before R5 in any case.
2. Gate: `alembic current` shows `0084_backfill_log_previous_state`; `/health/ready` is 200; the three flags are at their defaults.
3. Load the alert rules ([`../observability/SPEC-A-ALERTS.md`](../observability/SPEC-A-ALERTS.md)) and confirm every API process is scraped.

## R2: D2 revoke-scope test

The D2 question is whether Google's `/revoke` on one refresh token kills only that token (**per-token**) or the user's whole grant to this OAuth client (**grant-wide**). No code waits on it: the default `global` is safe either way, at the cost of leaving users extra Google grants. Under `global`, a replaced or duplicate grant is not revoked while a live row uses that Google account. An adapter rejection whose Google email is unknown is never revoked either.

1. With a real test Google account and the real OAuth client (never a member's mailbox), follow the record template below. Record the result in `docs/phases/phase-010/D2-GOOGLE-REVOKE-SCOPE.md`.
2. **Per-token:** set `ECC_GMAIL_REVOKE_SCOPE=none` and restart every process. This restores revoking replaced and duplicate grants. **Grant-wide:** keep `global`.
3. Canary check after switching to `none` (plan note N18). After the next `callback_duplicate` or `reconnect_replaced` revoke with `result="ok"`, wait until the surviving connector's access token has expired (about 1 h), then manually sync it.
   - Watch `EccGmailRefreshInvalidGrantAboveBaseline`, and the sync run's status and `error_summary`, for a 401 or `invalid_grant`. The canary counts only token refreshes, not Gmail API 401s.
   - Because sync is manual, detection waits for the first such sync, so do it deliberately rather than waiting for a user.
   - If it fails, set the scope back to `global`, restart, and record it.
4. Gate: D2 recorded; the scope decision recorded in the change record.

### D2 record template

Copy into `docs/phases/phase-010/D2-GOOGLE-REVOKE-SCOPE.md` (status `Closed` once decided). Never record tokens, OAuth codes or email addresses: refer to the test account by a label.

```markdown
---
id: PHASE-010-D2-GOOGLE-REVOKE-SCOPE
title: D2 Google Revoke Scope Record
status: Open
version: 1.0.0
owner: <operator>
updated: <YYYY-MM-DD>
---

# D2: Google revoke scope

- Date / operator / reviewer:
- OAuth client (project id label, not the secret):
- Test Google account label (not the address):
- ECC build (commit) and environment:

## Procedure
1. Connect the test account in workspace W1 (connector row R1); sync once.
2. Connect the same Google account again so a second refresh token exists
   (second workspace W2, row R2, or a fresh consent replacing R1's token).
3. Confirm both rows can refresh: manual sync each after its access token expired.
4. Revoke ONE token only (disconnect R2 with ECC_GMAIL_REVOKE_SCOPE=none in a
   test environment, or call Google's /revoke with R2's token directly).
   Note the revoke result (ecc_connector_revoke_total / Google's HTTP status).
5. Wait for R1's access token to expire (~1 h), then manual-sync R1.
6. Check Google Account > Security > Third-party access for the app.

## Result
- R1 refresh after R2's revoke: success / invalid_grant
- Third-party access entry after step 4: present / gone
- Conclusion: PER-TOKEN (R1 kept working) / GRANT-WIDE (R1 lost access)

## Decision
- ECC_GMAIL_REVOKE_SCOPE: none (per-token) / global (grant-wide)
- Applied on (date), all processes restarted: yes/no
- Canary watched until (date); invalid_grant above baseline: no / yes (action)
```

## R3: ownership audit

Run the read-only audit in each environment, as a whole-database run (no `--workspace-id`), using a read-only role where possible:

```bash
PYTHONPATH=backend ECC_DATABASE_URL=<url> uv run python scripts/audit_connector_ownership.py --out audit.csv
```

Exit 0 means nothing was found; exit 1 means rows were found (checks A–E, CSV ids only); exit 2 means an error. Keep the CSV for R4. Gate: CSV archived with the change record.

## R4: sign-offs and remediation

1. Complete the [Sign-off checklist](#sign-off-checklist). R5 needs every item in it.
2. Remediate checks **A, B, D and E** (C is cleared by the R5 backfill). Use `scripts/remediate_connector_ownership.py`, following the runbook in its docstring:
   - one workspace at a time, `--dry-run` first, then per-row confirmation;
   - A/E rows, and mismatched B/D connectors, are disconnected **without purge** and revoked if safe (`site="remediation"`);
   - B/D rows that are not mismatched are recorded as `connector_ownership.review_recorded` audit events.
   
   The application endpoints cannot do this: the engineering disable refuses Gmail with 409, the email-domain disable purges, and an E owner was removed.
3. Copy the command's stderr revoke counts into the change record (that process is not scraped). For every `revoke=error` or `credential_unavailable` row, have the mailbox owner remove the app's access at Google.
4. Re-run the audit. Gate: A and E rows show `row_status=disconnected`, every B/D row is remediated or recorded, and all sign-offs are ticked.

## R5: enable isolation and backfill

### Prerequisites (all must hold)

- [ ] R4 gate met; migrations 0083/0084 applied (R1); alert rules live.
- [ ] **Database backup.** Take a full backup immediately before R5 (`make backup`, `docs/operations/PHASE-0-BACKUP-RESTORE.md`), verify it (`make verify-restore BACKUP=<file>`), and record its name in the change record. The backfill log and the snapshot tables below make the R5 changes reversible row by row; the backup covers anything they do not (the grants the backfill revokes, a mistaken manual statement).
- [ ] **`<fx1_deploy_time>`** in every statement below is the moment the FX1 build (PR #308) went live, written with an explicit UTC offset, for example `'2026-09-29T14:00:00+00:00'`. The block also sets `SET LOCAL TimeZone = 'UTC'`, so the recorded `cleanup_ts` prints in UTC; a literal without an offset would otherwise be read in the session's time zone.
- [ ] **Snapshot before the FX1 clean-up.** The two statements below change rows that no log records, so first copy what they will change into snapshot tables (ids, owners, visibility, status only; no content). The snapshot block below, the pack archive (if you archive rather than refresh) and the AI-run narrowing form **one transaction** in one `psql` session: the block opens it with `BEGIN` and the narrowing ends it with `COMMIT`. If you refresh packs through the API instead, do that after the `COMMIT`; the snapshot still lists which packs to refresh.

  ```sql
  \set ON_ERROR_STOP on
  BEGIN;
  SET LOCAL TimeZone = 'UTC';
  SET LOCAL lock_timeout = '5s';
  -- Lock the AI runs and their steps BEFORE reading their grants: creating a
  -- grant locks its resource row (authz_grants._load_resource_for_update), so
  -- no new grant on these rows can commit until this transaction ends.
  SELECT id FROM ai_runs
  WHERE task_type = 'meeting.prep_summary' AND created_at < '<fx1_deploy_time>'
  ORDER BY id FOR UPDATE;
  SELECT st.id FROM ai_run_steps st JOIN ai_runs r
    ON r.id = st.run_id AND r.workspace_id = st.workspace_id
  WHERE r.task_type = 'meeting.prep_summary' AND r.created_at < '<fx1_deploy_time>'
  ORDER BY st.id FOR UPDATE OF st;
  CREATE TABLE spec_a_r5_meeting_packs_snapshot AS
    SELECT id, workspace_id, meeting_id, status, version, updated_at, updated_by
    FROM meeting_packs
    WHERE status IN ('fresh', 'stale') AND generated_at < '<fx1_deploy_time>';
  CREATE TABLE spec_a_r5_ai_runs_snapshot AS
    SELECT id, workspace_id, owner_id, visibility
    FROM ai_runs
    WHERE task_type = 'meeting.prep_summary' AND created_at < '<fx1_deploy_time>';
  CREATE TABLE spec_a_r5_ai_run_steps_snapshot AS
    SELECT s.id, s.workspace_id, s.owner_id, s.visibility
    FROM ai_run_steps s JOIN spec_a_r5_ai_runs_snapshot r
      ON r.id = s.run_id AND r.workspace_id = s.workspace_id;
  CREATE TABLE spec_a_r5_ai_run_grants_snapshot AS
    SELECT g.id FROM resource_grants g
    WHERE g.revoked_at IS NULL
      AND ((g.resource_type = 'ai_runs' AND g.resource_id IN (SELECT id FROM spec_a_r5_ai_runs_snapshot))
        OR (g.resource_type = 'ai_run_steps' AND g.resource_id IN (SELECT id FROM spec_a_r5_ai_run_steps_snapshot)));
  SELECT now() AS cleanup_ts;  -- record this value: every grant revoked below gets exactly it
  ```

  Keep the snapshot tables until R7 closes, then drop them. Restrict access to them like the backfill log.
- [ ] **FX1 packs.** Archive or refresh every active meeting pack generated before the FX1 deploy (PR #308). Such packs may hold other members' private rows and private text in their AI enrichment, and are served verbatim (as `stale`) until refreshed. They are the rows in `spec_a_r5_meeting_packs_snapshot`. Either refresh each one (`POST /api/v1/meetings/<meeting_id>/prep/refresh` as a member who can read the meeting), or archive them all; members then regenerate with `POST .../prep`:

  ```sql
  UPDATE meeting_packs p SET status = 'archived', updated_at = now(), version = p.version + 1
  FROM spec_a_r5_meeting_packs_snapshot s
  WHERE p.id = s.id AND p.workspace_id = s.workspace_id AND p.status IN ('fresh', 'stale');
  ```

  Superseded packs (`refreshed`, `archived`) are kept as history but no endpoint serves them. Undoing an archive (setting `status` back from the snapshot) is possible only for a meeting that has no newer active pack, because a meeting may have at most one `fresh`/`stale` pack; it would also re-serve the old content, so do it only as part of a full rollback.
- [ ] **FX1 AI runs: narrow them to their actor.** Every `meeting.prep_summary` `ai_runs` row (with its `ai_run_steps`) created before the FX1 deploy is served by `GET /api/v1/ai/runs/{id}` to any workspace member, and refreshing a pack does not touch it. Make them private to the member who ran them, and revoke grants on them, in the same transaction as the snapshot above:

  ```sql
  UPDATE ai_runs r SET owner_id = r.actor_id, visibility = 'private'
  FROM spec_a_r5_ai_runs_snapshot s
  WHERE r.id = s.id AND r.workspace_id = s.workspace_id;
  UPDATE ai_run_steps st SET owner_id = r.owner_id, visibility = 'private'
  FROM spec_a_r5_ai_run_steps_snapshot s, ai_runs r
  WHERE st.id = s.id AND st.workspace_id = s.workspace_id
    AND r.id = st.run_id AND r.workspace_id = st.workspace_id;
  UPDATE resource_grants g SET revoked_at = now()
  FROM spec_a_r5_ai_run_grants_snapshot s WHERE g.id = s.id;
  -- Re-check: no live grant may remain on a narrowed run or step. Aborts the
  -- whole transaction (ON_ERROR_STOP) if one does.
  DO $$
  BEGIN
    IF EXISTS (
      SELECT 1 FROM resource_grants g
      WHERE g.revoked_at IS NULL
        AND ((g.resource_type = 'ai_runs' AND g.resource_id IN (SELECT id FROM spec_a_r5_ai_runs_snapshot))
          OR (g.resource_type = 'ai_run_steps' AND g.resource_id IN (SELECT id FROM spec_a_r5_ai_run_steps_snapshot)))
    ) THEN
      RAISE EXCEPTION 'live grant on a narrowed meeting.prep_summary run or step: rolled back, re-run the block';
    END IF;
  END $$;
  COMMIT;
  ```

  **Why both a lock and a re-check.** Locking the targeted rows first is the primary guard: every grant-creating endpoint locks the resource row before inserting a grant (`authz_grants._load_resource_for_update`), so between the lock and `COMMIT` no grant on these rows can be created, and the grant snapshot is complete. The re-check before `COMMIT` is the backstop for any writer that does not take that row lock (none is known): instead of silently leaving a live grant behind, the transaction fails and rolls back as a whole, nothing changes, and you re-run the block. Run it in a quiet period: the `lock_timeout` makes it give up (and roll back) rather than queue writes behind it, and a deadlock error (Postgres aborts one side) is also safe to retry. If the block fails partway, the `CREATE TABLE` statements roll back with it.

  `now()` is the transaction's start time, so every grant revoked here has the same `revoked_at` (the `cleanup_ts` you recorded). That value, together with `spec_a_r5_ai_run_grants_snapshot`, identifies exactly these grants. This runbook gives no purge procedure: other rows may reference these runs, and narrowing removes the exposure. If a purge is later required, plan it separately, after R7.

  With the snapshot, the narrowing is reversible (full rollback only, since it re-exposes the runs):

  ```sql
  BEGIN;
  UPDATE ai_runs r SET owner_id = s.owner_id, visibility = s.visibility
  FROM spec_a_r5_ai_runs_snapshot s WHERE r.id = s.id AND r.workspace_id = s.workspace_id;
  UPDATE ai_run_steps st SET owner_id = s.owner_id, visibility = s.visibility
  FROM spec_a_r5_ai_run_steps_snapshot s WHERE st.id = s.id AND st.workspace_id = s.workspace_id;
  UPDATE resource_grants g SET revoked_at = NULL
  FROM spec_a_r5_ai_run_grants_snapshot s WHERE g.id = s.id AND g.revoked_at = '<cleanup_ts>';
  COMMIT;
  ```

- [ ] **Known gap acknowledged (FX1 #308 M1, open).** A row whose visibility is narrowed after a pack was generated stays in that stored pack. The pack is served (as `stale`) until someone refreshes it. This is the same class of leak as the pre-FX1 packs, but it is created after R5, by a grant narrowing, a delegation, or a backfill making a row private. Until it is fixed, refresh or archive the packs of meetings whose rows the backfill changed (its CSV lists them), and treat any narrowing as needing a pack refresh.

### Order

1. **Enable `ECC_PERSONAL_DATA_ISOLATION=true` and restart every process** ([Flags and processes](#flags-and-processes)).
   - From this moment new Gmail rows are written private.
   - Rows written while the flag was off are still `workspace`-visible until the backfill reaches them. Start step 2 immediately.
   - In this window, sharing those rows is already refused and removal is already unblocked.
2. **Backfill**, exactly as [`docs/SETUP.md`](../SETUP.md) ("Personal-data isolation rollout notes" → "Backfill") describes:
   - `--dry-run` and review it, including telling confirming members about `owner_inactive` rows (A2, signed);
   - the real run with the flag on its command line;
   - exit 1 with reviewed rows is the steady state (reasons table in SETUP.md).
   
   Note each run id it prints in the change record.
3. **Rebuild the knowledge projections one workspace at a time**, with the flag on the command line (`scripts/rebuild_knowledge_projections.py --workspace-id <UUID>`; SETUP.md "Rebuild" and [`PHASE-2-DEPLOYMENT.md`](PHASE-2-DEPLOYMENT.md)). Without the flag it refuses while private evidence exists. The rebuild prints the database and the flag's source: check both.
4. Refresh or archive the packs of meetings touched by the backfill (M1 above).

### Verify

- Re-run the R3 audit. Check C is cleared; A/E rows stay disconnected.
- Re-run the backfill `--dry-run`. It reports only rows you have already reviewed and accepted.
- As a second member (not the mailbox owner), check that another member's Gmail connector is absent from `GET /api/v1/engineering/connectors`, and that its sync runs and email recommendations return 404.
- Watch [Monitoring](#monitoring) for a day.

### Rollback (asymmetric)

Turning the flag off alone is **not** a rollback. Rows stay private, the share block and the removal predicates switch off, removal returns 409 again, and an admin could grant or transfer the private rows. To roll back:

1. Flag off and restart every process.
2. `--restore` **every** backfill run id, newest first. It refuses while the flag is on.
3. Rebuild per workspace.

Revoked grants are not restored (the SQL that lists them is in SETUP.md). Rows changed since the backfill are reported `changed_since_backfill:*` and left as they are.

What the restore does **not** undo:

- **Rows the application wrote `private` while the flag was on.** New Gmail connectors, runs, cursors, email recommendations, AI runs, Gmail evidence and confirmed email-derived tasks, commitments and risks were written private from the start. They are not in the backfill log, so they stay private after flag off and restore. With the flag off, the share block no longer protects them: an admin can grant or transfer them. If they must become workspace-visible again, that is a separate, deliberate SQL change; if they must stay protected, keep the flag on instead of rolling back.
- **Member removals done while the flag was on.** Their side effects stay: the removed members' Gmail connectors stay disconnected (and their Google grants revoked where that was safe), and re-owned Gmail-only person nodes and aliases keep their new owner. Their retained Gmail-derived rows stay private to the removed member.
- **The FX1 clean-up.** Archived packs and narrowed AI runs stay as they are unless you restore them from the R5 snapshot tables (above).

## R6: identity binding

1. Deploy T17 (frontend codes) and this documentation (T19). T08 is already on `main`.
2. Set `ECC_GMAIL_REQUIRE_IDENTITY_MATCH=true` and restart every process.
3. Verify: a connect whose Google email differs from the member's ECC email returns 403 `GMAIL_ACCOUNT_IDENTITY_MISMATCH`, and no row is written. On the process that served the request, `/metrics` showed `ecc_connector_enrollment_refused_total{provider="gmail",reason="identity_mismatch"} 0.0` before the test and now shows `1.0`; `EccGmailIdentityMismatchRefused` fires within a scrape and evaluation interval and stays firing for a day (its `increase()` window). A matching connect succeeds.
4. Rollback: flag off and restart. Nothing else changed.

## R7: two clean weeks, then T20

The clean window starts when R6's restart completes and needs **14 consecutive days** with all of the following:

- no `EccConnectorRevokeFailed` / `EccConnectorRevokeErrorRatioHigh` left unresolved: each has had its manual Google-side revoke and is recorded;
- no `EccGmailRefreshInvalidGrantAboveBaseline` that turned out to be caused by an ECC revoke. Confirm each one first (alert file, "What an alert means"): one whose mailbox owner confirms they removed the app's access at Google themselves, with no matching ECC revoke, is recorded as benign and does not reset the window;
- no flag toggled and no `--restore` run;
- no personal-data exposure incident, and no new backfill-reported row left unreviewed after a re-run;
- no open Critical/High finding against the Spec A code.

Any breach resets the window to day 0. **Treat every `EccConnectorRevokeFailed` as real by default**: do the Google-side check and reset the window. Several real `error` paths have no disconnect, removal or purge audit event (`callback_failure` has only `connector_account.enrollment_refused` or nothing; `adapter_callback`, `callback_duplicate` and `reconnect_replaced` have none), so do not require one. Declare a false positive only when **none** of the log lines in the alert file's "Revoke-error evidence" table (`connector_revoke_failed`, `gmail_revoke_failed`, `gmail_revoke_post_failed`, `connector_revoke_safety_check_failed`, `removal_revoke_credential_unavailable`, `remediation_revoke_credential_unavailable`) exists for that provider and site on any API process from 60 minutes before the alert to its end. The counters are pre-initialised at 0, so `increase()` does not fire without a counted event; an alert with no log line points at a monitoring cause, such as a relabelling that renamed series. Record such an alert, with the empty log search, as a false positive and do not reset the window. When it closes, record the dates and the alert history in the change record, then start T20 (remove the three flags). Spec B may start after T20.

## Monitoring

Alert rules, how to read each counter, and first actions are in [`../observability/SPEC-A-ALERTS.md`](../observability/SPEC-A-ALERTS.md). Points specific to the rollout:

- The counters are process-local and reset on every restart, and every R-step restarts. Every bounded label set is pre-initialised at 0 at startup (#355), so `increase()` sees the first event after a restart. It can still miss an event that happens before a restarted process's first scrape and brings the counter back to the old process's value (see "Remaining gaps" in the alert file): after each R-step restart, also check the audit log for `connector_account.enrollment_refused` and `connector_account.disabled` events and the Gmail `connector_revoke_failed` log lines. Never read the counters as raw values.
- Ops-script counts (remediation, backfill) are not scraped; they live in the change record.
- A revoke-safety check that fails at `site="adapter_callback"` (a DB error) counts as `skipped_unsafe`, not `error`. Watch that series too.
- Every Gmail callback refusal also revokes at `site="callback_failure"`. Net `ecc_connector_enrollment_refused_total` out of that trend (the recording rule does this).

## Known limitations (accepted, not blockers)

- **5 s statement timeouts behind a Gmail token refresh (FX5).**
  - A manual sync holds the owner's Gmail connector row locked while it refreshes an expired access token (up to 10 s). Other consent-checked work for that owner waits on it and can hit the 5 s statement timeout, returning a generic 500 with nothing written: another Gmail write, an on-demand thread body fetch (an N-message thread can take about N×5 s), or confirming an `email_action_detected` recommendation.
  - Sync phase 3 can also time out behind a concurrent sync's refresh. That sync returns 500, and its run stays `running` until the stale-run reaper closes it.
  - All are safe to retry. Moving the refresh out of the locked section is Spec B.
- **Removal and role change during a token refresh.** Sync phase 1 holds the workspace's shared membership lock across the same refresh. A concurrent member removal or role change gives up after 3 s with a retryable `409 MEMBERSHIP_CHANGE_BUSY` (ADR-0014).
- **`adapter_callback` revoke-safety DB failures count as `skipped_unsafe`** (fail closed), not `error`. See Monitoring.
- **0083 has no `lock_timeout`.** See R1 for the maintenance window and the `PGOPTIONS` workaround.
- **Unsalted `override_reason` digest (minor).** The backfill log's `previous_state` stores member-written attention `override_reason` text as an unsalted SHA-256 digest, never the text. A short, guessable reason could be confirmed by hashing candidates. The log is ops-only (no API, not an authz resource). Restrict read access to it, and purge it after R7 once no restore is needed.
- **Under `global` scope, extra Google grants stay.** Replaced or duplicate grants are not revoked while a live row uses the account, and a rejection whose Google email is unknown is never revoked (accepted, T08 #306 Q1). Users can remove them at Google. D2 may allow `none`.
- **FX1 #308 M1:** narrowed rows stay in stored meeting packs until refresh (R5 prerequisites).
- **The consent cascade does not purge `email.detect_action` `ai_runs`/`ai_run_steps`.** They stay private to the owner. Decide under DS2 (FX5 #309 Q1).
- **Member-removal count for recommendations.** At about 2M owned email recommendations, the count can exceed the 5 s timeout (6.9 s measured). It is a table-wide scan; a partial index is a follow-up.
- **Rows that stay readable.** Gmail evidence whose mailbox owner is ambiguous stays as written (reported every run), and the accepted DS3 residuals are listed in [`PRIVACY-CONSENT-CONTRACT.md`](../phases/phase-010/PRIVACY-CONSENT-CONTRACT.md#personal-data-isolation-security-remediation-spec-a).

## Sign-off checklist

G-SIGN. Record who signed, when, and where (link or change-record id). Only A2 is signed. Every other item is **open**, and R5 cannot start until all are ticked.

- [x] **A2** (backfill re-owns a derived row to an inactive recommendation owner, reported `owner_inactive`): keep current behaviour. Signed off by the user, 2026-09-30.
- [ ] **DS3** (privacy): Gmail-derived content private to the mailbox owner; person entities and aliases stay workspace knowledge. Includes acknowledging the accepted residuals: admin audit snapshots (N28), dashboard `recently_changed` ids (N27), and claims citing private evidence staying visible (N29(1)).
- [ ] **DS1** (product): no Gmail aliases; a work email that differs from the Gmail address gets 403 with guidance.
- [ ] **DS2** (privacy + product): retain, private, a removed member's Gmail-derived data (no purge on removal), including the open cascade question on email `ai_runs`.
- [ ] **Q-A1** (security): no admin break-glass for another member's Gmail; use removal.
- [ ] **C15-a** (security): any member may reactivate a workspace engineering connector with their own credentials.
- [ ] **C15-c** (product): per-workspace uniqueness of `(provider, external_account_id)` kept.
- [ ] **C15-d** (privacy): a removed member's email consent stays granted (connector disconnected, so no sync).
- [ ] **C15-f** (privacy): S2 remediation notifies the row owner only.
- [ ] **I5** (security): identity binding proves an email *match*, not mailbox ownership (no email verification).

Also open and not a sign-off: the owner-sharing question. FX3 also refuses owners who try to share their **own** email-derived rows; confirm this, or allow owner sharing.

## Changelog

| Version | Date | Summary | Author |
|---|---|---|---|
| 1.4.0 | 2026-10-01 | Counters are pre-initialised at 0 (#355) and the alert rules are plain `increase()` (alert file 1.4.0): R6 verification expects 0 then 1 and an alert that stays firing for a day; the R7 false-positive text and Monitoring note drop the new-series term and keep the first-scrape gap | Lucky Jain |
| 1.3.0 | 2026-10-01 | PR review: R7 treats revoke errors as real by default and needs an empty search across every revoke-error log line before calling one a false positive; the canary criterion confirms a user-side removal before recording it benign; R5 SQL uses UTC (`SET LOCAL TimeZone`, explicit offset in `<fx1_deploy_time>`) | Lucky Jain |
| 1.2.0 | 2026-10-01 | Review fixes: the R5 clean-up locks the targeted AI runs and steps before snapshotting their grants and re-checks for live grants before `COMMIT`; R6 alert now resolves after about 5 minutes; R7 requires confirming a new-series alert against log lines and audit events before resetting the clean window (false-positive causes include a fresh TSDB or replaced Prometheus server) | Lucky Jain |
| 1.1.0 | 2026-10-01 | Review fixes: database backup and snapshot tables required before R5; the FX1 AI-run clean-up is narrowing only (purge dropped), reversible through the snapshot, with one shared `revoked_at`; rollback lists what restore does not undo (rows written private while the flag was on, removal side effects); R6 verification and monitoring reflect counters that appear at 1 | Lucky Jain |
| 1.0.0 | 2026-10-01 | First rollout runbook for Spec A R1–R7: flags and restarts, compose gap, R5 order and FX1 prerequisites, D2 record template, clean window, known limitations, G-SIGN checklist | Lucky Jain |
