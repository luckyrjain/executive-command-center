# Setup and Usage

This guide gets Executive Command Center running locally with PostgreSQL, FastAPI, React, and a development-only authenticated session.

## Prerequisites

- Docker with Compose
- Python 3.14
- `uv`
- Node.js 22
- `pnpm` 10.12.4

## 1. Configure the repository

```bash
git clone https://github.com/luckyrjain/executive-command-center.git
cd executive-command-center
cp .env.example .env
```

Generate a session secret and place it in `.env`:

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(48))'
```

Load the environment:

```bash
set -a
source .env
set +a
```

## 2. Start PostgreSQL and migrate

```bash
docker compose up -d postgres
uv sync --frozen --all-groups --python 3.14
uv run alembic -c backend/alembic.ini upgrade head
```

## 3. Create a local authenticated session

Phase 1 does not include a production login screen. Create a development workspace, user, session, and CSRF token with:

```bash
uv run python scripts/bootstrap_dev.py
```

The bootstrap utility runs only when `ECC_ENV=development` and refuses non-local database hosts by default. Running it again reuses the existing local identity, revokes previous active sessions, and prints a fresh session and CSRF cookie pair.

Keep the printed session token private. The script prints two `document.cookie` commands that must be run in the browser console after the frontend opens. These JavaScript-set cookies are intentionally limited to local development and are not a replacement for production `HttpOnly` session cookies.

For an isolated remote development database only, explicitly set:

```bash
export ECC_BOOTSTRAP_ALLOW_REMOTE_DATABASE=1
```

Never enable this override for staging or production data.

## 4. Start the backend

```bash
uv run uvicorn ecc.main:app --app-dir backend --reload --host 127.0.0.1 --port 8000
```

Verify:

```bash
curl http://localhost:8000/health/live
curl http://localhost:8000/health/ready
```

API documentation is available at `http://localhost:8000/docs`.

## 5. Start the frontend

In another terminal:

```bash
corepack enable
corepack prepare pnpm@10.12.4 --activate
pnpm install --frozen-lockfile
pnpm --filter @ecc/frontend dev
```

Open `http://localhost:5173`, paste the two cookie commands from `bootstrap_dev.py` into the browser developer console, and reload.

## What is available

- Today dashboard
- Morning Brief
- recommendations and confirmation
- global Search
- immutable Audit history
- Phase 1 task, commitment, note, calendar, meeting, risk, and attention APIs

## Tests and quality gates

Backend:

```bash
uv run ruff check backend tests scripts
uv run ruff format --check backend tests scripts
uv run mypy backend
uv run pytest
uv run pip-audit
```

Frontend:

```bash
pnpm --filter @ecc/frontend typecheck
pnpm --filter @ecc/frontend test -- --run
pnpm --filter @ecc/frontend build
pnpm --filter @ecc/frontend exec playwright install --with-deps chromium
pnpm --filter @ecc/frontend test:e2e
```

## Docker Compose

To build the whole stack:

```bash
docker compose up --build
```

The services listen on:

- frontend: `http://localhost:5173`
- backend: `http://localhost:8000`
- PostgreSQL: `localhost:5432`

Migrations and the development identity still need to be created explicitly. The local-process workflow above is recommended during active development.

## Reset local data

```bash
docker compose down -v
docker compose up -d postgres
uv run alembic -c backend/alembic.ini upgrade head
uv run python scripts/bootstrap_dev.py
```

## Troubleshooting

### `ECC_SESSION_SECRET` validation error

Use a value with at least 32 characters and reload `.env` into the shell.

### Bootstrap refuses the environment or database

Confirm `ECC_ENV=development` and that `ECC_DATABASE_URL` points to `localhost`, `127.0.0.1`, or `::1`. Use the remote-development override only for an isolated non-production database.

### `401 Authentication required`

Run `scripts/bootstrap_dev.py` again, set both newly printed cookies, and reload. Use `localhost` consistently in browser URLs. Regeneration revokes the previous active session.

### `403 CSRF_TOKEN_REQUIRED` or `CSRF_TOKEN_INVALID`

Set the newly printed `ecc_csrf` cookie. It is tied to the generated session and current session secret.

### Database connection failure

```bash
docker compose ps
docker compose logs postgres
```

Confirm `ECC_DATABASE_URL` matches the Compose credentials.

### Frontend cannot reach the backend

Check `http://localhost:8000/health/ready`, confirm `VITE_API_BASE_URL`, and restart Vite after environment changes.

## Current limitations

- Production registration and login are not implemented in Phase 1.
- The bootstrap utility is development-only and deliberately refuses non-development environments.
- External Gmail, Google Calendar, GitHub, and Jira connectors are deferred.
- AI enrichment is optional and disabled by default; deterministic features remain available.
