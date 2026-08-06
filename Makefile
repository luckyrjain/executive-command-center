SHELL := /bin/bash

.PHONY: setup dev stop check docs-check test migrate backup restore verify-restore

setup:
	uv sync --all-groups
	corepack enable
	pnpm install --frozen-lockfile=false

dev:
	docker compose up --build

stop:
	docker compose down

check:
	uv run ruff check backend tests
	uv run ruff format --check backend tests
	uv run mypy backend
	pnpm --filter @ecc/frontend lint
	pnpm --filter @ecc/frontend typecheck

docs-check:
	python scripts/docs_status.py check
	python scripts/check_docs.py

test:
	uv run pytest
	pnpm --filter @ecc/frontend test -- --run

migrate:
	uv run alembic -c backend/alembic.ini upgrade head

backup:
	./scripts/backup.sh

restore:
	./scripts/restore.sh "$(BACKUP)"

verify-restore:
	./scripts/verify_restore.sh "$(BACKUP)"
