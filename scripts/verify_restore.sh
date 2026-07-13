#!/usr/bin/env bash
set -euo pipefail
backup=${1:?usage: verify_restore.sh BACKUP}
./scripts/restore.sh "$backup"
docker compose exec -T postgres psql -U "${POSTGRES_USER:-ecc}" -d ecc_restore -v ON_ERROR_STOP=1 -c "SELECT count(*) FROM alembic_version;" -c "SELECT count(*) FROM workspaces;" -c "SELECT count(*) FROM event_outbox;"
