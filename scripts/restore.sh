#!/usr/bin/env bash
set -euo pipefail
backup=${1:?usage: restore.sh BACKUP [TARGET_DB]}
target_db=${2:-ecc_restore}

sha256sum -c "${backup}.sha256"
docker compose exec -T postgres dropdb -U "${POSTGRES_USER:-ecc}" --if-exists "$target_db"
docker compose exec -T postgres createdb -U "${POSTGRES_USER:-ecc}" "$target_db"
docker compose exec -T postgres pg_restore \
  -U "${POSTGRES_USER:-ecc}" \
  -d "$target_db" \
  --clean \
  --if-exists \
  --exit-on-error < "$backup"
