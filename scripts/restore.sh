#!/usr/bin/env bash
set -euo pipefail

BACKUP=${1:?usage: restore.sh BACKUP}
TARGET_DATABASE_URL="${RESTORE_DATABASE_URL:-${2:-}}"

if [[ -z "${TARGET_DATABASE_URL}" ]]; then
  echo "RESTORE_DATABASE_URL or a target database URL is required" >&2
  exit 2
fi

if [[ ! -f "${BACKUP}" || ! -f "${BACKUP}.sha256" ]]; then
  echo "backup archive and checksum are required" >&2
  exit 2
fi

if command -v sha256sum >/dev/null 2>&1; then
  sha256sum -c "${BACKUP}.sha256"
else
  expected=$(awk '{print $1}' "${BACKUP}.sha256")
  actual=$(shasum -a 256 "${BACKUP}" | awk '{print $1}')
  [[ "${expected}" == "${actual}" ]]
fi

PG_URL="${TARGET_DATABASE_URL/postgresql+psycopg:/postgresql:}"
pg_restore \
  --clean \
  --if-exists \
  --exit-on-error \
  --no-owner \
  --no-privileges \
  --dbname="${PG_URL}" \
  "${BACKUP}"
