# Setup and Usage

This guide gets Executive Command Center running locally with PostgreSQL, the FastAPI backend, the React frontend, and a development-only authenticated session.

## What you get

After setup, open `http://localhost:5173` to use:

- Today dashboard
- Morning Brief
- recommendations and durable confirmation
- global Search
- immutable Audit history
- Phase 1 task, commitment, note, calendar, meeting, risk, and attention APIs

## Prerequisites

Install:

- Docker Desktop or another Docker Compose implementation
- Python 3.14
- `uv` 0.7.19 or compatible
- Node.js 22
- `pnpm` 10.12.4

Check the tools:

```bash
docker --version
docker compose version
python3 --version
uv --version
node --version
pnpm --version
```

The Python project is pinned to Python 3.14. A different Python version may fail dependency or type checks.

## 1. Clone and configure

```bash
git clone https://github.com/luckyrjain/executive-command-center.git
cd executive-command-center
cp .env.example .env
```

Replace `ECC_SESSION_SECRET` in `.env` with a random value of at least 32 characters:

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(48))'
```

Example `.env`:

```dotenv
ECC_ENV=development
ECC_DATABASE_URL=postgresql+psycopg://ecc:ecc@localhost:5432/ecc
ECC_SESSION_SECRET=replace-with-a-long-random-value
ECC_CORS_ORIGINS=http://localhost:5173
POSTGRES_DB=ecc
POSTGRES_USER=ecc
POSTGRES_PASSWORD=ecc
VITE_API_BASE_URL=http://localhost:8000
```

## 2. Start PostgreSQL

The recommended development workflow runs PostgreSQL in Docker and the application processes locally:

```bash
docker compose up -d postgres
docker compose ps
```

Wait until the PostgreSQL service is healthy.

## 3. Install backend dependencies

```bash
uv sync --frozen --all-groups --python 3.14
```

Load `.env` into the current shell:

```bash
set -a
source .env
set +a
```

For `fish`, export the variables using your preferred environment loader instead.

## 4. Apply database migrations

```bash
uv run alembic -c backend/alembic.ini upgrade head
```

Verify the backend can connect to the database:

```bash
uv run uvicorn ecc.main:app --app-dir backend --host 127.0.0.1 --port 8000
```

In another terminal:

```bash
curl http://localhost:8000/health/live
curl http://localhost:8000/health/ready
```

Both endpoints should report an `ok` or `ready` status.

## 5. Create a development workspace and session

Phase 1 intentionally does not include a production login screen. For local development, run:

```bash
set -a
source .env
set +a
uv run python scripts/bootstrap_dev.py
```

The command creates:

- a local workspace using the `Asia/Kolkata` timezone
- a local development user
- a 30-day server-side session
- a matching CSRF token

It prints two `document.cookie` commands. Keep the terminal output private because the session token authenticates the local user.

## 6. Install and start the frontend

In a new terminal:

```bash
corepack enable
corepack prepare pnpm@10.12.4 --activate
pnpm install --frozen-lockfile
pnpm --filter @ecc/frontend dev
```

Open `http://localhost:5173`.

Open the browser developer console, paste the two cookie commands printed by `scripts/bootstrap_dev.py`, and reload the page.

The frontend sends the `ecc_session` cookie with authenticated requests and reads `ecc_csrf` when making state-changing requests.

## Daily development commands

Backend:

```bash
set -a; source .env; set +a
uv run uvicorn ecc.main:app --app-dir backend --reload --host 127.0.0.1 --port 8000
```

Frontend:

```bash
pnpm --filter @ecc/frontend dev
```

Database:

```bash
docker compose up -d postgres
```

## Running the test and quality gates

Backend:

```bash
uv run ruff check backend tests
uv run ruff format --check backend tests
uv run mypy backend
uv run alembic -c backend/alembic.ini upgrade head
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

## Docker Compose application stack

To build and start all containers:

```bash
docker compose up --build
```

The services listen on:

- frontend: `http://localhost:5173`
- backend: `http://localhost:8000`
- PostgreSQL: `localhost:5432`

Database migrations and the development identity must still be created explicitly. Run migrations against the Compose database before using the app. The local-process workflow above is recommended while the project remains in active Phase 1 development.

## First-use workflow

Once authenticated:

1. Open the Today dashboard.
2. Create domain data through the Phase 1 APIs or development tooling.
3. Refresh the deterministic attention projection.
4. Refresh the Morning Brief.
5. Review recommendations; publication is required before confirmation.
6. Use Search to find entities across the workspace.
7. Use Audit to inspect immutable mutation history.

FastAPI API documentation is available at:

- `http://localhost:8000/docs`
- `http://localhost:8000/redoc`

## Resetting local data

Stop the stack and delete the PostgreSQL volume:

```bash
docker compose down -v
```

Then restart PostgreSQL, rerun migrations, and rerun `scripts/bootstrap_dev.py`.

## Common problems

### `ECC_SESSION_SECRET` validation error

Set a value at least 32 characters long and reload `.env` into the shell.

### `401 Authentication required`

Rerun `scripts/bootstrap_dev.py`, set both printed cookies in the browser console, and reload. Ensure the backend and frontend use `localhost` consistently rather than mixing `localhost` and `127.0.0.1` for browser URLs.

### `403 CSRF_TOKEN_REQUIRED` or `CSRF_TOKEN_INVALID`

Set the printed `ecc_csrf` cookie again. The CSRF token is tied to the generated session and the current `ECC_SESSION_SECRET`.

### Database connection failure

Check:

```bash
docker compose ps
docker compose logs postgres
```

Confirm `ECC_DATABASE_URL` matches the local Compose credentials.

### Migration errors after switching branches

Run:

```bash
uv run alembic -c backend/alembic.ini upgrade head
```

For disposable local data, reset the volume and migrate from an empty database.

### Frontend shows backend unavailable

Confirm:

```bash
curl http://localhost:8000/health/ready
```

Also confirm `VITE_API_BASE_URL=http://localhost:8000` and restart Vite after changing frontend environment variables.

## Current limitations

- Production registration and login are not implemented in Phase 1.
- External Gmail, Google Calendar, GitHub, and Jira connectors are deferred.
- The local bootstrap utility is for development only and must not be used as production authentication.
- AI enrichment is optional and disabled by default; deterministic features remain available.
