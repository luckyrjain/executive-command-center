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

printf 'restore verification passed at revision %s\n' "${target_revision}"
