#!/usr/bin/env bash
set -euo pipefail
mkdir -p .local/backups
stamp=$(date -u +%Y%m%dT%H%M%SZ)
file=".local/backups/ecc-${stamp}-0001_foundation.dump"
docker compose exec -T postgres pg_dump -U "${POSTGRES_USER:-ecc}" -d "${POSTGRES_DB:-ecc}" --format=custom > "$file"
sha256sum "$file" > "${file}.sha256"
echo "$file"
