#!/usr/bin/env bash
set -euo pipefail
backup=${1:?usage: restore.sh BACKUP}
sha256sum -c "${backup}.sha256"
docker compose exec -T postgres dropdb -U "${POSTGRES_USER:-ecc}" --if-exists ecc_restore
docker compose exec -T postgres createdb -U "${POSTGRES_USER:-ecc}" ecc_restore
cat "$backup" | docker compose exec -T postgres pg_restore -U "${POSTGRES_USER:-ecc}" -d ecc_restore --clean --if-exists
