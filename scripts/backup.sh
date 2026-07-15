#!/usr/bin/env bash
set -euo pipefail

DATABASE_URL="${ECC_DATABASE_URL:-${DATABASE_URL:-postgresql://ecc:ecc@localhost:5432/ecc}}"
BACKUP_DIR="${BACKUP_DIR:-.local/backups}"
SCHEMA_VERSION="${SCHEMA_VERSION:-unknown}"
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
ARCHIVE="${BACKUP_DIR}/ecc-${TIMESTAMP}-${SCHEMA_VERSION}.dump"
PG_URL="${DATABASE_URL/postgresql+psycopg:/postgresql:}"

mkdir -p "${BACKUP_DIR}"

pg_dump \
  --format=custom \
  --no-owner \
  --no-privileges \
  --file="${ARCHIVE}" \
  "${PG_URL}"

if command -v sha256sum >/dev/null 2>&1; then
  sha256sum "${ARCHIVE}" > "${ARCHIVE}.sha256"
else
  shasum -a 256 "${ARCHIVE}" > "${ARCHIVE}.sha256"
fi

printf '%s\n' "${ARCHIVE}"
