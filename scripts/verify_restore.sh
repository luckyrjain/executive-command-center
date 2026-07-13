#!/usr/bin/env bash
set -euo pipefail
backup=${1:?usage: verify_restore.sh BACKUP}
target_db=${2:-ecc_restore}

./scripts/restore.sh "$backup" "$target_db"

source_db=${POSTGRES_DB:-ecc}
user=${POSTGRES_USER:-ecc}
required_tables=(
  workspaces users sessions pkos_nodes pkos_edges pkos_evidence
  event_outbox event_inbox event_dead_letters alembic_version
)

source_revision=$(docker compose exec -T postgres psql -U "$user" -d "$source_db" -Atqc "SELECT version_num FROM alembic_version")
restore_revision=$(docker compose exec -T postgres psql -U "$user" -d "$target_db" -Atqc "SELECT version_num FROM alembic_version")
[[ "$source_revision" == "$restore_revision" ]]

for table in "${required_tables[@]}"; do
  source_count=$(docker compose exec -T postgres psql -U "$user" -d "$source_db" -Atqc "SELECT count(*) FROM $table")
  restore_count=$(docker compose exec -T postgres psql -U "$user" -d "$target_db" -Atqc "SELECT count(*) FROM $table")
  [[ "$source_count" == "$restore_count" ]]
done

docker compose exec -T postgres psql -U "$user" -d "$target_db" -v ON_ERROR_STOP=1 <<'SQL'
SELECT conname FROM pg_constraint WHERE conname IN (
  'fk_sessions_workspace_user',
  'fk_pkos_edges_workspace_source',
  'fk_pkos_edges_workspace_target',
  'fk_pkos_evidence_workspace_node'
);
SQL

echo "restore verification passed for $target_db at revision $restore_revision"
