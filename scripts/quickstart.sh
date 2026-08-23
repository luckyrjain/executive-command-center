#!/usr/bin/env bash
# One-command local setup: env file, Postgres, migrations, backend/frontend
# dependencies, and a local development session. See docs/SETUP.md for what
# each step below does and how to run it by hand.
set -euo pipefail
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ ! -f .env ]]; then
  cp .env.example .env
  # ECC_SESSION_SECRET is the HMAC key behind session cookies, CSRF tokens,
  # and (when the encryption-key vars below are left unset, as they are
  # here) the deterministic fallback source for the connector-token/
  # personal-data Fernet keys -- `cp`'s default mode leaves .env
  # world-readable, so lock it down before -- not after -- the real secret
  # ever lands in it, closing the window a shared/multi-user host would
  # otherwise leave open to reading it.
  chmod 600 .env
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

# .env.example's own placeholder ("replace-with-a-random-secret-at-least-32-
# characters-long") is 56 characters, so the length check above alone lets
# an un-edited .env through silently -- this only runs when .env already
# existed (the block above always overwrites it with a real generated
# secret), e.g. after `cp .env.example .env` from the manual walkthrough.
# Mirrors backend/ecc/config.py's own _PLACEHOLDER_SECRET_MARKERS check,
# which only runs outside development and so never catches this path.
shopt -s nocasematch
if [[ "${ECC_SESSION_SECRET}" == *replace-with* || "${ECC_SESSION_SECRET}" == *changeme* || "${ECC_SESSION_SECRET}" == *change-me* || "${ECC_SESSION_SECRET}" == *placeholder* ]]; then
  shopt -u nocasematch
  echo "ECC_SESSION_SECRET in .env still looks like the .env.example placeholder. Set a real random secret and re-run." >&2
  exit 1
fi
shopt -u nocasematch

echo "==> Starting PostgreSQL"
docker compose up -d postgres

echo "==> Waiting for PostgreSQL to accept connections"
ready=""
# 120 x 1s: at least as generous as docker-compose.yml's own postgres
# healthcheck (20 retries x 5s interval => ~100s), so this does not time
# out earlier than the project's own healthcheck would on a slow first
# start (e.g. a loaded CI shared runner -- see the widened performance
# budgets elsewhere in this repo for the same class of noise).
for _ in $(seq 1 120); do
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
