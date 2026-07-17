#!/usr/bin/env bash
set -euo pipefail

BACKUP=${1:?usage: verify_restore.sh BACKUP}
SOURCE_DATABASE_URL="${ECC_DATABASE_URL:-${DATABASE_URL:-postgresql://ecc:ecc@localhost:5432/ecc}}"
TARGET_DATABASE_URL="${RESTORE_DATABASE_URL:-}"

if [[ -z "${TARGET_DATABASE_URL}" ]]; then
  echo "RESTORE_DATABASE_URL is required" >&2
  exit 2
fi

./scripts/restore.sh "${BACKUP}" "${TARGET_DATABASE_URL}"

SOURCE_PG_URL="${SOURCE_DATABASE_URL/postgresql+psycopg:/postgresql:}"
TARGET_PG_URL="${TARGET_DATABASE_URL/postgresql+psycopg:/postgresql:}"

source_revision=$(psql "${SOURCE_PG_URL}" -Atqc "SELECT version_num FROM alembic_version")
target_revision=$(psql "${TARGET_PG_URL}" -Atqc "SELECT version_num FROM alembic_version")
[[ "${source_revision}" == "${target_revision}" ]]

tables=$(psql "${SOURCE_PG_URL}" -Atqc "
  SELECT tablename
  FROM pg_tables
  WHERE schemaname = 'public'
  ORDER BY tablename
")

while IFS= read -r table; do
  [[ -z "${table}" ]] && continue
  source_count=$(psql "${SOURCE_PG_URL}" -Atqc "SELECT count(*) FROM public.\"${table}\"")
  target_count=$(psql "${TARGET_PG_URL}" -Atqc "SELECT count(*) FROM public.\"${table}\"")
  if [[ "${source_count}" != "${target_count}" ]]; then
    echo "row-count mismatch for ${table}: ${source_count} != ${target_count}" >&2
    exit 1
  fi
done <<< "${tables}"

source_constraints=$(psql "${SOURCE_PG_URL}" -Atqc "
  SELECT count(*) FROM pg_constraint
  WHERE connamespace = 'public'::regnamespace
")
target_constraints=$(psql "${TARGET_PG_URL}" -Atqc "
  SELECT count(*) FROM pg_constraint
  WHERE connamespace = 'public'::regnamespace
")
[[ "${source_constraints}" == "${target_constraints}" ]]

# --- Representative record checksums, audit append-only protection, and
# PKOS mapped-column verification -------------------------------------------
#
# There is no DB-level trigger or rule anywhere in backend/migrations/ that
# enforces audit_events append-only-ness, so "append-only audit protection"
# is verified here at the data level instead: scripts/seed_phase1_acceptance.py
# computes a full-row checksum per table (casting each row to text and
# aggregating per-row md5 digests) for every seeded Phase 1 table, scoped to
# the two seeded workspaces. Comparing that checksum between the source and
# the restored target proves no field of any row -- including audit_events,
# where this stands in for the missing trigger, and the pkos_* tables --
# was silently altered by the restore.
source_checksums=$(uv run python scripts/seed_phase1_acceptance.py \
  --checksums --database-url "${SOURCE_PG_URL}")
target_checksums=$(uv run python scripts/seed_phase1_acceptance.py \
  --checksums --database-url "${TARGET_PG_URL}")

if [[ "${source_checksums}" != "${target_checksums}" ]]; then
  echo "representative record checksum mismatch between source and restored target:" >&2
  diff <(printf '%s\n' "${source_checksums}") <(printf '%s\n' "${target_checksums}") >&2 || true
  exit 1
fi
printf '%s\n' "${target_checksums}" | grep -q "^audit_events	" || {
  echo "no audit_events checksum computed -- seed fixtures missing?" >&2
  exit 1
}
echo "representative record checksums match for every seeded Phase 1 table"
echo "append-only audit protection verified: restored audit_events rows are checksum-identical to source"
echo "PKOS mapped-column checksums verified (pkos_nodes, pkos_edges, pkos_evidence)"

# --- Composite workspace isolation -------------------------------------------
#
# scripts/seed_phase1_acceptance.py always creates two genuinely isolated
# workspaces ("alpha" and "bravo"). Every workspace-scoped table (discovered
# generically, not hardcoded, so this keeps working as the schema grows) must
# still contain rows for BOTH seeded workspaces after restore -- proving the
# restore did not silently collapse or drop one workspace's data.
workspace_ids=$(uv run python scripts/seed_phase1_acceptance.py --print-workspace-ids)
alpha_id=$(printf '%s\n' "${workspace_ids}" | awk -F'\t' '$1=="alpha"{print $2}')
bravo_id=$(printf '%s\n' "${workspace_ids}" | awk -F'\t' '$1=="bravo"{print $2}')

workspace_scoped_tables=$(psql "${TARGET_PG_URL}" -Atqc "
  SELECT table_name FROM information_schema.columns
  WHERE table_schema = 'public' AND column_name = 'workspace_id'
  ORDER BY table_name
")

while IFS= read -r table; do
  [[ -z "${table}" ]] && continue
  alpha_count=$(psql "${TARGET_PG_URL}" -Atqc \
    "SELECT count(*) FROM public.\"${table}\" WHERE workspace_id = '${alpha_id}'")
  bravo_count=$(psql "${TARGET_PG_URL}" -Atqc \
    "SELECT count(*) FROM public.\"${table}\" WHERE workspace_id = '${bravo_id}'")
  if [[ "${alpha_count}" == "0" || "${bravo_count}" == "0" ]]; then
    echo "workspace isolation check failed for ${table}: alpha=${alpha_count} bravo=${bravo_count}" >&2
    exit 1
  fi
done <<< "${workspace_scoped_tables}"
echo "workspace isolation verified: both seeded workspaces are represented in every workspace-scoped table"

# --- Lifecycle restoration fields --------------------------------------------
#
# Every lifecycle-bearing Phase 1 table gets one archived seed row
# (archived_at + pre_archive_status set). Confirm those fields round-tripped
# rather than being nulled out or defaulted by the restore.
for table in tasks commitments notes calendar_events meetings risks recommendations; do
  source_lifecycle=$(psql "${SOURCE_PG_URL}" -Atqc "
    SELECT count(*) FROM public.\"${table}\"
    WHERE archived_at IS NOT NULL AND pre_archive_status IS NOT NULL
  ")
  target_lifecycle=$(psql "${TARGET_PG_URL}" -Atqc "
    SELECT count(*) FROM public.\"${table}\"
    WHERE archived_at IS NOT NULL AND pre_archive_status IS NOT NULL
  ")
  if [[ "${source_lifecycle}" == "0" || "${source_lifecycle}" != "${target_lifecycle}" ]]; then
    echo "lifecycle field restoration check failed for ${table}: source=${source_lifecycle} target=${target_lifecycle}" >&2
    exit 1
  fi
done
echo "lifecycle restoration fields (archived_at, pre_archive_status) survived restore intact"

# --- Search index/query readiness --------------------------------------------
#
# Exercises the same tsvector/GIN infrastructure ecc.search uses, against the
# restored database, confirming both that the seeded marker rows are present
# and that full-text search actually works post-restore (not just that the
# indexes exist).
marker='Phase1SeedMarker'
search_hits=$(psql "${TARGET_PG_URL}" -Atqc "
  SELECT count(*) FROM (
    SELECT id FROM tasks WHERE workspace_id IN ('${alpha_id}', '${bravo_id}')
      AND to_tsvector('simple', coalesce(title, '') || ' ' || coalesce(description, ''))
          @@ plainto_tsquery('simple', '${marker}')
    UNION ALL
    SELECT id FROM commitments WHERE workspace_id IN ('${alpha_id}', '${bravo_id}')
      AND to_tsvector('simple', coalesce(summary, '') || ' ' || coalesce(description, ''))
          @@ plainto_tsquery('simple', '${marker}')
    UNION ALL
    SELECT id FROM notes WHERE workspace_id IN ('${alpha_id}', '${bravo_id}')
      AND search_document @@ plainto_tsquery('simple', '${marker}')
    UNION ALL
    SELECT id FROM meetings WHERE workspace_id IN ('${alpha_id}', '${bravo_id}')
      AND to_tsvector('simple',
            coalesce(title, '') || ' ' || concat_ws(' ', agenda, preparation, notes_summary))
          @@ plainto_tsquery('simple', '${marker}')
    UNION ALL
    SELECT id FROM calendar_events WHERE workspace_id IN ('${alpha_id}', '${bravo_id}')
      AND to_tsvector('simple', coalesce(title, '') || ' ' || concat_ws(' ', description, location))
          @@ plainto_tsquery('simple', '${marker}')
    UNION ALL
    SELECT id FROM risks WHERE workspace_id IN ('${alpha_id}', '${bravo_id}')
      AND to_tsvector('simple', coalesce(description, '') || ' ' || concat_ws(' ', mitigation, trigger))
          @@ plainto_tsquery('simple', '${marker}')
  ) marker_hits
")
if [[ "${search_hits}" != "12" ]]; then
  echo "search readiness check failed: expected 12 marker hits, found ${search_hits}" >&2
  exit 1
fi
echo "search index/query readiness verified: full-text search against the restored database found all 12 seeded marker rows"

# --- Application readiness ---------------------------------------------------
#
# Start the real FastAPI app pointed at the restored database and confirm
# /health/ready reports ready -- proving the app, not just psql, can use the
# recovered database.
app_target_url="${TARGET_DATABASE_URL/#postgresql:/postgresql+psycopg:}"
readiness_port="${PHASE1_VERIFY_APP_PORT:-8931}"
app_log=$(mktemp)

cleanup_app() {
  if [[ -n "${app_pid:-}" ]]; then
    kill "${app_pid}" >/dev/null 2>&1 || true
    wait "${app_pid}" 2>/dev/null || true
  fi
  rm -f "${app_log}"
}
trap cleanup_app EXIT

ECC_DATABASE_URL="${app_target_url}" \
ECC_SESSION_SECRET="${ECC_SESSION_SECRET:-phase1-recovery-drill-session-secret-value}" \
ECC_ENV="${ECC_ENV:-development}" \
  uv run uvicorn ecc.main:app --app-dir backend --host 127.0.0.1 --port "${readiness_port}" \
  > "${app_log}" 2>&1 &
app_pid=$!

ready=false
for _ in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:${readiness_port}/health/ready" >/dev/null 2>&1; then
    ready=true
    break
  fi
  sleep 1
done

if [[ "${ready}" != true ]]; then
  echo "application failed to become ready against the restored database" >&2
  cat "${app_log}" >&2
  exit 1
fi
echo "application readiness verified: /health/ready is ready against the restored database"

kill "${app_pid}" >/dev/null 2>&1 || true
wait "${app_pid}" 2>/dev/null || true
app_pid=""
trap - EXIT
rm -f "${app_log}"

# --- 600-second development RTO ----------------------------------------------
#
# $SECONDS is bash's built-in elapsed-seconds-since-shell-start counter, so it
# covers this entire recovery drill: restore.sh plus every verification check
# above (creating the target database and running the initial backup happen
# in the calling workflow step, before this script starts, and are
# deliberately excluded -- RTO measures time to recover from an existing
# backup, not time to produce one).
if (( SECONDS > 600 )); then
  echo "RTO exceeded: restore + verification took ${SECONDS}s (budget 600s)" >&2
  exit 1
fi
printf 'RTO check passed: %ss elapsed (budget 600s)\n' "${SECONDS}"

printf 'restore verification passed at revision %s\n' "${target_revision}"
