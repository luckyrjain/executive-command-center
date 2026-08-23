#!/usr/bin/env bash
# One-command local setup: env file, Postgres, migrations, backend/frontend
# dependencies, and a local development session. See docs/SETUP.md for what
# each step below does and how to run it by hand.
set -euo pipefail
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ ! -f .env ]]; then
  cp .env.example .env
  secret="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
  sed -i.bak "s#^ECC_SESSION_SECRET=.*#ECC_SESSION_SECRET=${secret}#" .env
  rm -f .env.bak
  echo "==> Created .env with a generated ECC_SESSION_SECRET"
else
  echo "==> .env already exists; leaving it as-is"
fi

set -a
source .env
set +a

if [[ ${#ECC_SESSION_SECRET} -lt 32 ]]; then
  echo "ECC_SESSION_SECRET in .env must be at least 32 characters. Edit .env and re-run." >&2
  exit 1
fi

echo "==> Starting PostgreSQL"
docker compose up -d postgres

echo "==> Waiting for PostgreSQL to accept connections"
ready=""
for _ in $(seq 1 30); do
  if docker compose exec -T postgres pg_isready -U "${POSTGRES_USER:-ecc}" -d "${POSTGRES_DB:-ecc}" >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 1
done
if [[ -z "${ready}" ]]; then
  echo "PostgreSQL did not become ready in time. Check 'docker compose logs postgres'." >&2
  exit 1
fi

echo "==> Installing backend dependencies"
uv sync --frozen --all-groups --python 3.14

echo "==> Running database migrations"
uv run alembic -c backend/alembic.ini upgrade head

echo "==> Installing frontend dependencies"
corepack enable
corepack prepare pnpm@10.12.4 --activate
pnpm install --frozen-lockfile

echo "==> Creating a local development session"
uv run python scripts/bootstrap_dev.py

cat <<'EOF'

==> Setup complete. Start the app in two terminals:

  Terminal 1 (backend):
    uv run uvicorn ecc.main:app --app-dir backend --reload --host 127.0.0.1 --port 8000

  Terminal 2 (frontend):
    pnpm --filter @ecc/frontend dev

Then open the one-time bootstrap URL printed above (it expires in 15 minutes;
re-run this script to get a fresh one).
EOF
