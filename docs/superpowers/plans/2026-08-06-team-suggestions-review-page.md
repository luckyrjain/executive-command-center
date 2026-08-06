# Team Suggestions Review Page Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give operators a single page that groups every unconfirmed `suggested_team_name` across `repositories` and `engineering_work_items` and lets them bulk-confirm or bulk-dismiss it, instead of confirming one row at a time.

**Architecture:** One new migration column pair (`team_suggestion_dismissed_at`), a small adapter-side reset rule in all three sync adapters, three new backend endpoints under `/api/v1/engineering/team-suggestions` (GET aggregate, POST confirm, POST dismiss), and a new `TeamSuggestionsPanel.tsx` tab in the Engineering workspace.

**Tech Stack:** FastAPI + SQLAlchemy Core (`text()` queries) + Alembic on the backend; React + TanStack Query on the frontend; pytest against real Postgres for backend tests, Vitest + Testing Library for frontend tests.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-08-06-team-suggestions-review-page-design.md` — every task below implements one section of it; read it first if anything here is ambiguous.
- No new table, no new FK — reuses `team_entity_id`/`team_assignment_version`/`team_assignment_updated_by` (migration `0050_phase6_team_linkage.py`) unchanged.
- `team_suggestion_dismissed_at` is sync-silent: adapters only ever *clear* it (when `suggested_team_name` changes), never set it.
- Bulk endpoints authorize per-row (`action="write"`), skipping unauthorized rows rather than failing the whole batch — never grant more access than the existing single-item endpoints already would.
- No `expected_version` on the bulk endpoints — see spec's "Backend endpoints" section for why.
- Local dev DB: `ECC_DATABASE_URL=postgresql+psycopg://ecc:ecc@localhost:5432/ecc`, run migrations with `set -a; source .env; set +a; uv run alembic -c backend/alembic.ini upgrade head`. Run backend tests with `CI=1 ECC_DATABASE_URL=postgresql+psycopg://ecc:ecc@localhost:5432/ecc .venv/bin/python -m pytest <path> -q`.

---

### Task 1: Migration — `team_suggestion_dismissed_at` columns

**Files:**
- Create: `backend/migrations/versions/0072_phase6_team_suggestion_dismissal.py`
- Test: `tests/test_engineering_gitlab_sync_postgres.py` (one new assertion-only test, no adapter change needed yet — proves the column exists and defaults to `NULL`)

**Interfaces:**
- Produces: `repositories.team_suggestion_dismissed_at` and `engineering_work_items.team_suggestion_dismissed_at`, both nullable `TIMESTAMPTZ`, used by every later task.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_engineering_gitlab_sync_postgres.py`, near the other `_reject_private_host`/schema-adjacent tests (anywhere at module level is fine):

```python
def test_repositories_have_team_suggestion_dismissed_at_column() -> None:
    """Migration `0072_phase6_team_suggestion_dismissal.py` -- proves the
    column exists and defaults to NULL before any adapter/endpoint code
    depends on it.
    """
    with engine.begin() as connection:
        row = connection.execute(
            text(
                "SELECT column_name, is_nullable FROM information_schema.columns "
                "WHERE table_name = 'repositories' AND column_name = 'team_suggestion_dismissed_at'"
            )
        ).one_or_none()
    assert row is not None
    assert row.is_nullable == "YES"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `CI=1 ECC_DATABASE_URL=postgresql+psycopg://ecc:ecc@localhost:5432/ecc .venv/bin/python -m pytest tests/test_engineering_gitlab_sync_postgres.py::test_repositories_have_team_suggestion_dismissed_at_column -v`
Expected: FAIL — `row is not None` assertion fails (column doesn't exist yet).

- [ ] **Step 3: Write the migration**

Create `backend/migrations/versions/0072_phase6_team_suggestion_dismissal.py`:

```python
"""Team suggestion dismissal (team suggestions review page).

Adds `team_suggestion_dismissed_at` (nullable `TIMESTAMPTZ`) to both
`repositories` and `engineering_work_items`, alongside `0050_phase6_
team_linkage.py`'s existing `team_entity_id`/`suggested_team_name` pair.
Sync-silent like `team_entity_id` -- no adapter's `ON CONFLICT ... DO
UPDATE` ever *sets* it, and only ever *clears* it when the incoming
`suggested_team_name` differs from the row's current stored value (see
`docs/superpowers/specs/2026-08-06-team-suggestions-review-page-design.md`'s
"Data model" section for the full reasoning). A human dismisses a
suggestion via the new `POST /api/v1/engineering/team-suggestions/dismiss`
endpoint; no sync ever sets this column.
"""

import sqlalchemy as sa
from alembic import op

revision = "0072_phase6_team_suggestion_dismissal"
down_revision = "0071_phase10_recs_create_path"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ("repositories", "engineering_work_items"):
        op.add_column(
            table,
            sa.Column("team_suggestion_dismissed_at", sa.DateTime(timezone=True), nullable=True),
        )


def downgrade() -> None:
    for table in ("repositories", "engineering_work_items"):
        op.drop_column(table, "team_suggestion_dismissed_at")
```

Apply it:

```bash
set -a; source .env; set +a
uv run alembic -c backend/alembic.ini upgrade head
```

- [ ] **Step 4: Run test to verify it passes**

Run: `CI=1 ECC_DATABASE_URL=postgresql+psycopg://ecc:ecc@localhost:5432/ecc .venv/bin/python -m pytest tests/test_engineering_gitlab_sync_postgres.py::test_repositories_have_team_suggestion_dismissed_at_column -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/migrations/versions/0072_phase6_team_suggestion_dismissal.py tests/test_engineering_gitlab_sync_postgres.py
git commit -m "feat(db): add team_suggestion_dismissed_at to repositories/work items"
```

---

### Task 2: GitHub adapter — dismiss-reset on changed suggestion

**Files:**
- Modify: `backend/ecc/domains/engineering/github_adapter.py:208-281` (`_upsert_repository`)
- Test: `tests/test_engineering_github_sync_postgres.py`

**Interfaces:**
- Consumes: `repositories.team_suggestion_dismissed_at` (Task 1).
- Produces: nothing new consumed by later tasks — this is a self-contained sync-behavior fix.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_engineering_github_sync_postgres.py`, right after `test_incremental_resync_refreshes_suggestion_without_touching_confirmed_team`:

```python
def test_incremental_resync_clears_dismissed_suggestion_when_name_changes(
    seeded_account_context: ConnectorAccountContext,
) -> None:
    """A human dismissal (`team_suggestion_dismissed_at`) is a judgment
    about one specific `suggested_team_name` -- if the repo's owner
    changes, the next sync must clear the old dismissal so the new
    suggestion isn't silently suppressed by a stale one.
    """
    repos = [_repo(1, full_name="acme/a", updated_at="2024-01-03T00:00:00Z", owner_login="acme")]
    adapter = GitHubAdapter(transport=httpx.MockTransport(lambda r: _json_response(repos)))
    adapter.backfill(seeded_account_context, "repository")

    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE repositories SET team_suggestion_dismissed_at = :now "
                "WHERE workspace_id = :workspace_id"
            ),
            {"now": datetime.now(UTC), "workspace_id": seeded_account_context.workspace_id},
        )

    # Same owner, later sync -- dismissal must survive.
    repos_same_owner = [
        _repo(1, full_name="acme/a", updated_at="2024-01-04T00:00:00Z", owner_login="acme")
    ]
    adapter2 = GitHubAdapter(transport=httpx.MockTransport(lambda r: _json_response(repos_same_owner)))
    adapter2.incremental_sync(seeded_account_context, "repository", "2024-01-03T00:00:00Z")

    with engine.begin() as connection:
        still_dismissed = connection.execute(
            text(
                "SELECT team_suggestion_dismissed_at FROM repositories "
                "WHERE workspace_id = :workspace_id"
            ),
            {"workspace_id": seeded_account_context.workspace_id},
        ).scalar_one()
    assert still_dismissed is not None

    # Owner changes -- dismissal must clear, new suggestion must show.
    repos_new_owner = [
        _repo(1, full_name="acme/a", updated_at="2024-01-05T00:00:00Z", owner_login="acme-new")
    ]
    adapter3 = GitHubAdapter(transport=httpx.MockTransport(lambda r: _json_response(repos_new_owner)))
    adapter3.incremental_sync(seeded_account_context, "repository", "2024-01-04T00:00:00Z")

    with engine.begin() as connection:
        cleared = connection.execute(
            text(
                "SELECT team_suggestion_dismissed_at, suggested_team_name FROM repositories "
                "WHERE workspace_id = :workspace_id"
            ),
            {"workspace_id": seeded_account_context.workspace_id},
        ).one()
    assert cleared.team_suggestion_dismissed_at is None
    assert cleared.suggested_team_name == "acme-new"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `CI=1 ECC_DATABASE_URL=postgresql+psycopg://ecc:ecc@localhost:5432/ecc .venv/bin/python -m pytest tests/test_engineering_github_sync_postgres.py::test_incremental_resync_clears_dismissed_suggestion_when_name_changes -v`
Expected: FAIL — `still_dismissed is not None` may pass, but `cleared.team_suggestion_dismissed_at is None` fails (nothing clears it yet).

- [ ] **Step 3: Write minimal implementation**

In `backend/ecc/domains/engineering/github_adapter.py`, in `_upsert_repository`'s `ON CONFLICT ... DO UPDATE SET` clause, change:

```python
                    updated_at = EXCLUDED.updated_at,
                    suggested_team_name = EXCLUDED.suggested_team_name
```

to:

```python
                    updated_at = EXCLUDED.updated_at,
                    suggested_team_name = EXCLUDED.suggested_team_name,
                    team_suggestion_dismissed_at = CASE
                        WHEN repositories.suggested_team_name IS DISTINCT FROM EXCLUDED.suggested_team_name
                        THEN NULL
                        ELSE repositories.team_suggestion_dismissed_at
                    END
```

- [ ] **Step 4: Run test to verify it passes**

Run: `CI=1 ECC_DATABASE_URL=postgresql+psycopg://ecc:ecc@localhost:5432/ecc .venv/bin/python -m pytest tests/test_engineering_github_sync_postgres.py -q`
Expected: PASS (all tests in the file, including the pre-existing ones — confirms nothing else broke)

- [ ] **Step 5: Commit**

```bash
git add backend/ecc/domains/engineering/github_adapter.py tests/test_engineering_github_sync_postgres.py
git commit -m "feat(github): clear team-suggestion dismissal when suggested name changes"
```

---

### Task 3: GitLab adapter — dismiss-reset on changed suggestion

**Files:**
- Modify: `backend/ecc/domains/engineering/gitlab_adapter.py:287-353` (`_upsert_repository`)
- Test: `tests/test_engineering_gitlab_sync_postgres.py`

**Interfaces:**
- Consumes: `repositories.team_suggestion_dismissed_at` (Task 1). Identical shape to Task 2, different adapter.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_engineering_gitlab_sync_postgres.py`, near its own suggested-team-name tests (search for `_suggested_team_name` usage in that file's tests for the right spot — anywhere at module level works):

```python
def test_incremental_resync_clears_dismissed_suggestion_when_namespace_changes(
    seeded_account_context: ConnectorAccountContext,
) -> None:
    """Identical reasoning to `test_engineering_github_sync_postgres.py`'s
    own dismissal-reset test -- GitLab's suggestion source is the
    project's `namespace.name` instead of GitHub's `owner.login`.
    """
    projects = [
        _project(1, path="acme/a", updated_at="2024-01-03T00:00:00Z", namespace={"name": "acme"})
    ]
    adapter = GitLabAdapter(
        transport=httpx.MockTransport(lambda r: _json_response(projects)),
        resolve_host=lambda host: ["140.82.112.3"],
    )
    adapter.backfill(seeded_account_context, "repository")

    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE repositories SET team_suggestion_dismissed_at = :now "
                "WHERE workspace_id = :workspace_id"
            ),
            {"now": datetime.now(UTC), "workspace_id": seeded_account_context.workspace_id},
        )

    projects_new_namespace = [
        _project(
            1, path="acme/a", updated_at="2024-01-04T00:00:00Z", namespace={"name": "acme-new"}
        )
    ]
    adapter2 = GitLabAdapter(
        transport=httpx.MockTransport(lambda r: _json_response(projects_new_namespace)),
        resolve_host=lambda host: ["140.82.112.3"],
    )
    adapter2.incremental_sync(seeded_account_context, "repository", "2024-01-03T00:00:00Z")

    with engine.begin() as connection:
        cleared = connection.execute(
            text(
                "SELECT team_suggestion_dismissed_at, suggested_team_name FROM repositories "
                "WHERE workspace_id = :workspace_id"
            ),
            {"workspace_id": seeded_account_context.workspace_id},
        ).one()
    assert cleared.team_suggestion_dismissed_at is None
    assert cleared.suggested_team_name == "acme-new"
```

Check how `GitLabAdapter.backfill`/`incremental_sync` are called elsewhere in this file for the exact `resolve_host` keyword — if an existing test in this file constructs `GitLabAdapter` for a real sync call (not just `authorize`) without passing `resolve_host`, match that call's exact kwargs instead (grep `GitLabAdapter(transport=` in this file first).

- [ ] **Step 2: Run test to verify it fails**

Run: `CI=1 ECC_DATABASE_URL=postgresql+psycopg://ecc:ecc@localhost:5432/ecc .venv/bin/python -m pytest tests/test_engineering_gitlab_sync_postgres.py::test_incremental_resync_clears_dismissed_suggestion_when_namespace_changes -v`
Expected: FAIL

- [ ] **Step 3: Write minimal implementation**

In `backend/ecc/domains/engineering/gitlab_adapter.py`, in `_upsert_repository`'s `ON CONFLICT ... DO UPDATE SET` clause, change:

```python
                    updated_at = EXCLUDED.updated_at,
                    suggested_team_name = EXCLUDED.suggested_team_name
```

to:

```python
                    updated_at = EXCLUDED.updated_at,
                    suggested_team_name = EXCLUDED.suggested_team_name,
                    team_suggestion_dismissed_at = CASE
                        WHEN repositories.suggested_team_name IS DISTINCT FROM EXCLUDED.suggested_team_name
                        THEN NULL
                        ELSE repositories.team_suggestion_dismissed_at
                    END
```

- [ ] **Step 4: Run test to verify it passes**

Run: `CI=1 ECC_DATABASE_URL=postgresql+psycopg://ecc:ecc@localhost:5432/ecc .venv/bin/python -m pytest tests/test_engineering_gitlab_sync_postgres.py -q`
Expected: PASS (all tests in the file)

- [ ] **Step 5: Commit**

```bash
git add backend/ecc/domains/engineering/gitlab_adapter.py tests/test_engineering_gitlab_sync_postgres.py
git commit -m "feat(gitlab): clear team-suggestion dismissal when suggested namespace changes"
```

---

### Task 4: Jira adapter — dismiss-reset on changed suggestion

**Files:**
- Modify: `backend/ecc/domains/engineering/jira_adapter.py:~208-292` (`_upsert_work_item` — the function containing the SQL block shown in the spec exploration; confirm the exact function name with `grep -n "^def _upsert" backend/ecc/domains/engineering/jira_adapter.py` before editing)
- Test: `tests/test_engineering_jira_sync_postgres.py`

**Interfaces:**
- Consumes: `engineering_work_items.team_suggestion_dismissed_at` (Task 1). Identical shape to Tasks 2/3, third adapter, `engineering_work_items` table instead of `repositories`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_engineering_jira_sync_postgres.py`:

```python
def test_incremental_resync_clears_dismissed_suggestion_when_project_changes(
    seeded_account_context: ConnectorAccountContext,
) -> None:
    """Identical reasoning to the GitHub/GitLab adapters' own dismissal-
    reset tests -- Jira's suggestion source is the issue's `fields.
    project.name` (no native "team" construct exists in Jira's API).
    """
    issues = [
        _issue(
            1,
            key="ACME-1",
            summary="Fix the thing",
            updated="2024-01-03T00:00:00.000+0000",
            project={"name": "Acme Project"},
        )
    ]
    adapter = JiraAdapter(transport=httpx.MockTransport(lambda r: _json_response(_search_response(issues))))
    adapter.backfill(seeded_account_context, "work_item")

    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE engineering_work_items SET team_suggestion_dismissed_at = :now "
                "WHERE workspace_id = :workspace_id"
            ),
            {"now": datetime.now(UTC), "workspace_id": seeded_account_context.workspace_id},
        )

    issues_new_project = [
        _issue(
            1,
            key="ACME-1",
            summary="Fix the thing",
            updated="2024-01-04T00:00:00.000+0000",
            project={"name": "Acme Project New"},
        )
    ]
    adapter2 = JiraAdapter(
        transport=httpx.MockTransport(lambda r: _json_response(_search_response(issues_new_project)))
    )
    adapter2.incremental_sync(seeded_account_context, "work_item", "2024-01-03T00:00:00.000+0000")

    with engine.begin() as connection:
        cleared = connection.execute(
            text(
                "SELECT team_suggestion_dismissed_at, suggested_team_name FROM engineering_work_items "
                "WHERE workspace_id = :workspace_id"
            ),
            {"workspace_id": seeded_account_context.workspace_id},
        ).one()
    assert cleared.team_suggestion_dismissed_at is None
    assert cleared.suggested_team_name == "Acme Project New"
```

Before writing this, run `grep -n "class JiraAdapter\|def backfill\|def incremental_sync\|GitLabAdapter(transport" tests/test_engineering_jira_sync_postgres.py` to confirm the exact adapter constructor name/kwargs used elsewhere in that file (mirror whatever existing tests do — do not guess a different shape).

- [ ] **Step 2: Run test to verify it fails**

Run: `CI=1 ECC_DATABASE_URL=postgresql+psycopg://ecc:ecc@localhost:5432/ecc .venv/bin/python -m pytest tests/test_engineering_jira_sync_postgres.py::test_incremental_resync_clears_dismissed_suggestion_when_project_changes -v`
Expected: FAIL

- [ ] **Step 3: Write minimal implementation**

In `backend/ecc/domains/engineering/jira_adapter.py`, in the work-item upsert function's `ON CONFLICT ... DO UPDATE SET` clause, change:

```python
                    updated_at = EXCLUDED.updated_at,
                    suggested_team_name = EXCLUDED.suggested_team_name
```

to:

```python
                    updated_at = EXCLUDED.updated_at,
                    suggested_team_name = EXCLUDED.suggested_team_name,
                    team_suggestion_dismissed_at = CASE
                        WHEN engineering_work_items.suggested_team_name IS DISTINCT FROM EXCLUDED.suggested_team_name
                        THEN NULL
                        ELSE engineering_work_items.team_suggestion_dismissed_at
                    END
```

- [ ] **Step 4: Run test to verify it passes**

Run: `CI=1 ECC_DATABASE_URL=postgresql+psycopg://ecc:ecc@localhost:5432/ecc .venv/bin/python -m pytest tests/test_engineering_jira_sync_postgres.py -q`
Expected: PASS (all tests in the file)

- [ ] **Step 5: Commit**

```bash
git add backend/ecc/domains/engineering/jira_adapter.py tests/test_engineering_jira_sync_postgres.py
git commit -m "feat(jira): clear team-suggestion dismissal when suggested project changes"
```

---

### Task 5: Backend — `GET /api/v1/engineering/team-suggestions` aggregation endpoint

**Files:**
- Modify: `backend/ecc/domains/engineering/connector_accounts.py` (add response models near `WorkItemListResponse` at line ~544, add endpoint near `list_work_items_endpoint` at line ~1685)
- Test: Create `tests/test_engineering_team_suggestions_postgres.py`

**Interfaces:**
- Consumes: `authz.visible_resource_filter_sql` (already imported via `from ecc.platform import authz`), `AuthDep`, `SessionDep`, `router` (all already defined in this file).
- Produces: `TeamSuggestionSampleItem`, `TeamSuggestionGroup`, `TeamSuggestionListResponse` Pydantic models, reused by Tasks 6/7's response type and by the frontend's `types.ts` (Task 8) field-for-field.

- [ ] **Step 1: Write the failing test**

Create `tests/test_engineering_team_suggestions_postgres.py`:

```python
"""Team suggestions review page (`docs/superpowers/specs/2026-08-06-team-
suggestions-review-page-design.md`) -- `GET /api/v1/engineering/team-
suggestions` aggregation, `POST .../confirm`, `POST .../dismiss`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from hashlib import sha256
from hmac import new
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from identity_fixtures import create_identity
from sqlalchemy import text

from ecc.config import get_settings
from ecc.database import engine
from ecc.main import app

settings = get_settings()


@pytest.fixture
def suggestions_context():
    workspace_id = uuid4()
    user_id = uuid4()
    token = f"session-{uuid4()}"
    now = datetime.now(UTC)

    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, 'Team Suggestions Test', 'UTC', :now)"
            ),
            {"id": workspace_id, "now": now},
        )
        create_identity(
            connection, workspace_id=workspace_id, user_id=user_id,
            email=f"{user_id}@example.test", now=now,
        )
        connection.execute(
            text(
                "INSERT INTO sessions (id, workspace_id, user_id, token_hash, "
                "expires_at, last_seen_at) "
                "VALUES (:id, :workspace_id, :user_id, :token_hash, :expires_at, :now)"
            ),
            {
                "id": uuid4(), "workspace_id": workspace_id, "user_id": user_id,
                "token_hash": sha256(token.encode()).hexdigest(),
                "expires_at": now + timedelta(hours=1), "now": now,
            },
        )

    client = TestClient(app)
    client.cookies.set("ecc_session", token)
    try:
        yield client, workspace_id, user_id, token
    finally:
        client.close()
        with engine.begin() as connection:
            for table in (
                "repositories", "engineering_work_items", "connector_accounts",
                "event_outbox", "audit_events", "idempotency_records",
                "pkos_nodes", "sessions", "users",
            ):
                connection.execute(
                    text(f"DELETE FROM {table} WHERE workspace_id = :workspace_id"),  # noqa: S608
                    {"workspace_id": workspace_id},
                )
            connection.execute(
                text("DELETE FROM workspaces WHERE id = :workspace_id"),
                {"workspace_id": workspace_id},
            )


def _headers(token: str, key: str | None = None) -> dict[str, str]:
    csrf = new(settings.session_secret.encode(), token.encode(), "sha256").hexdigest()
    headers = {"X-CSRF-Token": csrf, "X-Correlation-ID": str(uuid4())}
    if key is not None:
        headers["Idempotency-Key"] = key
    return headers


def _insert_connector_account(workspace_id: UUID, *, provider: str = "github") -> UUID:
    account_id = uuid4()
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO connector_accounts (id, workspace_id, provider, "
                "external_account_id, display_name, granted_scopes, credential, "
                "status, version, created_at, updated_at) "
                "VALUES (:id, :workspace_id, :provider, 'ext-1', 'Acct', '{}', "
                "'encrypted', 'active', 1, :now, :now)"
            ),
            {"id": account_id, "workspace_id": workspace_id, "provider": provider, "now": now},
        )
    return account_id


def _insert_repository(
    workspace_id: UUID, connector_account_id: UUID, owner_id: UUID, *,
    name: str, suggested_team_name: str | None,
    team_entity_id: UUID | None = None, dismissed: bool = False,
) -> UUID:
    repo_id = uuid4()
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO repositories (id, workspace_id, connector_account_id, "
                "provider, external_id, name, source_url, permission_state, "
                "freshness_state, observed_at, created_at, updated_at, "
                "suggested_team_name, team_entity_id, team_suggestion_dismissed_at, "
                "owner_id, visibility) "
                "VALUES (:id, :workspace_id, :connector_account_id, 'github', :ext_id, "
                ":name, :source_url, 'active', 'fresh', :now, :now, :now, "
                ":suggested_team_name, :team_entity_id, :dismissed_at, :owner_id, 'workspace')"
            ),
            {
                "id": repo_id, "workspace_id": workspace_id,
                "connector_account_id": connector_account_id,
                "ext_id": str(repo_id), "name": name,
                "source_url": f"https://github.com/{name}", "now": now,
                "suggested_team_name": suggested_team_name, "team_entity_id": team_entity_id,
                "dismissed_at": now if dismissed else None, "owner_id": owner_id,
            },
        )
    return repo_id


def _insert_pkos_team(workspace_id: UUID, *, name: str = "Platform") -> UUID:
    team_id = uuid4()
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO pkos_nodes (id, workspace_id, node_type, canonical_name, "
                "status, confidence, version, created_at, updated_at) "
                "VALUES (:id, :workspace_id, 'team', :name, 'active', 1.0, 1, :now, :now)"
            ),
            {"id": team_id, "workspace_id": workspace_id, "name": name, "now": now},
        )
    return team_id


def test_team_suggestions_groups_by_suggested_name_across_resource_types(suggestions_context) -> None:
    client, workspace_id, user_id, token = suggestions_context
    account_id = _insert_connector_account(workspace_id)
    _insert_repository(workspace_id, account_id, user_id, name="acme/a", suggested_team_name="Platform")
    _insert_repository(workspace_id, account_id, user_id, name="acme/b", suggested_team_name="Platform")
    _insert_repository(workspace_id, account_id, user_id, name="acme/c", suggested_team_name="Growth")

    response = client.get("/api/v1/engineering/team-suggestions", headers=_headers(token))
    assert response.status_code == 200
    items = {item["suggested_team_name"]: item for item in response.json()["items"]}
    assert items["Platform"]["repository_count"] == 2
    assert items["Platform"]["work_item_count"] == 0
    assert items["Growth"]["repository_count"] == 1
    assert len(items["Platform"]["sample_items"]) == 2


def test_team_suggestions_excludes_confirmed_dismissed_and_null_names(suggestions_context) -> None:
    client, workspace_id, user_id, token = suggestions_context
    account_id = _insert_connector_account(workspace_id)
    team_id = _insert_pkos_team(workspace_id)
    _insert_repository(workspace_id, account_id, user_id, name="acme/confirmed", suggested_team_name="Platform", team_entity_id=team_id)
    _insert_repository(workspace_id, account_id, user_id, name="acme/dismissed", suggested_team_name="Growth", dismissed=True)
    _insert_repository(workspace_id, account_id, user_id, name="acme/no-suggestion", suggested_team_name=None)
    _insert_repository(workspace_id, account_id, user_id, name="acme/pending", suggested_team_name="Infra")

    response = client.get("/api/v1/engineering/team-suggestions", headers=_headers(token))
    names = {item["suggested_team_name"] for item in response.json()["items"]}
    assert names == {"Infra"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `CI=1 ECC_DATABASE_URL=postgresql+psycopg://ecc:ecc@localhost:5432/ecc .venv/bin/python -m pytest tests/test_engineering_team_suggestions_postgres.py -v`
Expected: FAIL — `404 Not Found` (route doesn't exist yet).

- [ ] **Step 3: Write minimal implementation**

In `backend/ecc/domains/engineering/connector_accounts.py`, add after `WorkItemListResponse` (after line ~544, before the Datadog projections comment block):

```python
# --- Team suggestions review (docs/superpowers/specs/2026-08-06-team-
# suggestions-review-page-design.md) --------------------------------------


class TeamSuggestionSampleItem(BaseModel):
    id: UUID
    resource_type: Literal["repository", "work_item"]
    name: str


class TeamSuggestionGroup(BaseModel):
    suggested_team_name: str
    repository_count: int
    work_item_count: int
    sample_items: list[TeamSuggestionSampleItem]


class TeamSuggestionListResponse(BaseModel):
    items: list[TeamSuggestionGroup]


class TeamSuggestionConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    suggested_team_name: str = Field(min_length=1)
    team_entity_id: UUID


class TeamSuggestionDismissRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    suggested_team_name: str = Field(min_length=1)


class TeamSuggestionActionResponse(BaseModel):
    updated: list[UUID]
    skipped_unauthorized: list[UUID]


_TEAM_SUGGESTION_SAMPLE_CAP = 5
```

Then add the endpoint after `list_work_items_endpoint` (after line ~1685):

```python
@router.get("/team-suggestions", response_model=TeamSuggestionListResponse)
def list_team_suggestions_endpoint(auth: AuthDep, session: SessionDep) -> TeamSuggestionListResponse:
    """Grouped, read-only view of every unconfirmed, undismissed
    `suggested_team_name` across `repositories` and `engineering_work_
    items` -- see the design doc's "Backend endpoints" section. Runs two
    separate visibility-filtered queries (one per table) rather than a
    single `UNION ALL`, because `authz.visible_resource_filter_sql` binds
    its own fixed parameter names (`__authz_resource_type` etc.) per call
    -- merging two calls' params into one query text would make both
    subqueries silently share whichever `resource_type` value won the
    dict merge. Grouping by `suggested_team_name` happens in Python
    instead, over what are already small, pre-filtered row sets.
    """
    visibility_sql_repo, visibility_params_repo = authz.visible_resource_filter_sql(
        session, auth, resource_type="repositories", action="read", table_alias="repositories"
    )
    repo_rows = (
        session.execute(
            text(
                "SELECT id, name, suggested_team_name FROM repositories "
                f"WHERE workspace_id = :workspace_id AND ({visibility_sql_repo}) "  # noqa: S608
                "AND team_entity_id IS NULL AND team_suggestion_dismissed_at IS NULL "
                "AND suggested_team_name IS NOT NULL"
            ),
            {"workspace_id": auth.workspace_id, **visibility_params_repo},
        )
        .mappings()
        .all()
    )

    visibility_sql_wi, visibility_params_wi = authz.visible_resource_filter_sql(
        session, auth, resource_type="engineering_work_items", action="read",
        table_alias="engineering_work_items",
    )
    work_item_rows = (
        session.execute(
            text(
                "SELECT id, title AS name, suggested_team_name FROM engineering_work_items "
                f"WHERE workspace_id = :workspace_id AND ({visibility_sql_wi}) "  # noqa: S608
                "AND team_entity_id IS NULL AND team_suggestion_dismissed_at IS NULL "
                "AND suggested_team_name IS NOT NULL"
            ),
            {"workspace_id": auth.workspace_id, **visibility_params_wi},
        )
        .mappings()
        .all()
    )

    groups: dict[str, dict[str, Any]] = {}
    for row in repo_rows:
        group = groups.setdefault(
            row["suggested_team_name"],
            {"repository_count": 0, "work_item_count": 0, "sample_items": []},
        )
        group["repository_count"] += 1
        if len(group["sample_items"]) < _TEAM_SUGGESTION_SAMPLE_CAP:
            group["sample_items"].append(
                {"id": row["id"], "resource_type": "repository", "name": row["name"]}
            )
    for row in work_item_rows:
        group = groups.setdefault(
            row["suggested_team_name"],
            {"repository_count": 0, "work_item_count": 0, "sample_items": []},
        )
        group["work_item_count"] += 1
        if len(group["sample_items"]) < _TEAM_SUGGESTION_SAMPLE_CAP:
            group["sample_items"].append(
                {"id": row["id"], "resource_type": "work_item", "name": row["name"]}
            )

    items = [
        TeamSuggestionGroup(suggested_team_name=name, **data)
        for name, data in sorted(
            groups.items(),
            key=lambda kv: kv[1]["repository_count"] + kv[1]["work_item_count"],
            reverse=True,
        )
    ]
    return TeamSuggestionListResponse(items=items)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `CI=1 ECC_DATABASE_URL=postgresql+psycopg://ecc:ecc@localhost:5432/ecc .venv/bin/python -m pytest tests/test_engineering_team_suggestions_postgres.py -v`
Expected: PASS (both tests)

- [ ] **Step 5: Commit**

```bash
git add backend/ecc/domains/engineering/connector_accounts.py tests/test_engineering_team_suggestions_postgres.py
git commit -m "feat(engineering): add GET /team-suggestions aggregation endpoint"
```

---

### Task 6: Backend — `POST /api/v1/engineering/team-suggestions/confirm`

**Files:**
- Modify: `backend/ecc/domains/engineering/connector_accounts.py` (add helper + endpoint after Task 5's endpoint)
- Test: `tests/test_engineering_team_suggestions_postgres.py`

**Interfaces:**
- Consumes: `TeamSuggestionConfirmRequest`, `TeamSuggestionActionResponse` (Task 5), `_validate_team_entity`, `_write_team_assignment_side_effects`, `_lock_idempotency`, `_load_cached`, `_store_idempotency`, `_request_hash` (all pre-existing in this file).
- Produces: `_lock_and_authorize_suggestion_candidates` helper, reused by Task 7's dismiss endpoint.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_engineering_team_suggestions_postgres.py`:

```python
def test_confirm_team_suggestion_bulk_assigns_across_resource_types(suggestions_context) -> None:
    client, workspace_id, user_id, token = suggestions_context
    account_id = _insert_connector_account(workspace_id)
    team_id = _insert_pkos_team(workspace_id, name="Platform")
    repo_a = _insert_repository(workspace_id, account_id, user_id, name="acme/a", suggested_team_name="Platform")
    repo_b = _insert_repository(workspace_id, account_id, user_id, name="acme/b", suggested_team_name="Platform")
    _insert_repository(workspace_id, account_id, user_id, name="acme/other", suggested_team_name="Growth")

    response = client.post(
        "/api/v1/engineering/team-suggestions/confirm",
        json={"suggested_team_name": "Platform", "team_entity_id": str(team_id)},
        headers=_headers(token, key=str(uuid4())),
    )
    assert response.status_code == 200
    body = response.json()
    assert set(body["updated"]) == {str(repo_a), str(repo_b)}
    assert body["skipped_unauthorized"] == []

    with engine.begin() as connection:
        rows = connection.execute(
            text(
                "SELECT team_entity_id, team_assignment_version FROM repositories "
                "WHERE workspace_id = :workspace_id AND suggested_team_name = 'Platform'"
            ),
            {"workspace_id": workspace_id},
        ).all()
    assert all(row.team_entity_id == team_id for row in rows)
    assert all(row.team_assignment_version == 2 for row in rows)


def test_confirm_team_suggestion_is_idempotent_on_replay(suggestions_context) -> None:
    client, workspace_id, user_id, token = suggestions_context
    account_id = _insert_connector_account(workspace_id)
    team_id = _insert_pkos_team(workspace_id)
    _insert_repository(workspace_id, account_id, user_id, name="acme/a", suggested_team_name="Platform")
    key = str(uuid4())
    payload = {"suggested_team_name": "Platform", "team_entity_id": str(team_id)}

    first = client.post(
        "/api/v1/engineering/team-suggestions/confirm", json=payload, headers=_headers(token, key=key)
    )
    second = client.post(
        "/api/v1/engineering/team-suggestions/confirm", json=payload, headers=_headers(token, key=key)
    )
    assert first.json() == second.json()

    with engine.begin() as connection:
        version = connection.execute(
            text(
                "SELECT team_assignment_version FROM repositories WHERE workspace_id = :workspace_id"
            ),
            {"workspace_id": workspace_id},
        ).scalar_one()
    assert version == 2  # not bumped twice


def test_confirm_team_suggestion_rejects_non_team_entity(suggestions_context) -> None:
    client, workspace_id, user_id, token = suggestions_context
    account_id = _insert_connector_account(workspace_id)
    _insert_repository(workspace_id, account_id, user_id, name="acme/a", suggested_team_name="Platform")
    not_a_team_id = uuid4()
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO pkos_nodes (id, workspace_id, node_type, canonical_name, "
                "status, confidence, version, created_at, updated_at) "
                "VALUES (:id, :workspace_id, 'person', 'Not A Team', 'active', 1.0, 1, :now, :now)"
            ),
            {"id": not_a_team_id, "workspace_id": workspace_id, "now": now},
        )

    response = client.post(
        "/api/v1/engineering/team-suggestions/confirm",
        json={"suggested_team_name": "Platform", "team_entity_id": str(not_a_team_id)},
        headers=_headers(token, key=str(uuid4())),
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "TEAM_ENTITY_KIND_MISMATCH"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `CI=1 ECC_DATABASE_URL=postgresql+psycopg://ecc:ecc@localhost:5432/ecc .venv/bin/python -m pytest tests/test_engineering_team_suggestions_postgres.py -k confirm -v`
Expected: FAIL — `404 Not Found`.

- [ ] **Step 3: Write minimal implementation**

In `backend/ecc/domains/engineering/connector_accounts.py`, add after Task 5's `list_team_suggestions_endpoint`:

```python
def _lock_and_authorize_suggestion_candidates(
    session: Session,
    auth: AuthContext,
    *,
    table: Literal["repositories", "engineering_work_items"],
    suggested_team_name: str,
) -> tuple[list[UUID], list[UUID]]:
    """Locks every row in `table` still eligible for a team-suggestion
    bulk action (unconfirmed, undismissed, matching `suggested_team_
    name`), then authorizes each individually for `action="write"`.
    Returns `(authorized_ids, skipped_unauthorized_ids)`. Locking before
    authorizing matches the single-item endpoints' own `FOR UPDATE`
    timing, so a concurrent sync or another bulk action can't change a
    row out from under this one mid-check. `table` is always one of the
    two hardcoded literals below -- never request-derived.
    """
    rows = session.execute(
        text(
            f"SELECT id FROM {table} WHERE workspace_id = :workspace_id "  # noqa: S608
            "AND suggested_team_name = :suggested_team_name "
            "AND team_entity_id IS NULL AND team_suggestion_dismissed_at IS NULL FOR UPDATE"
        ),
        {"workspace_id": auth.workspace_id, "suggested_team_name": suggested_team_name},
    ).all()
    authorized: list[UUID] = []
    skipped: list[UUID] = []
    for (row_id,) in rows:
        if authz.authorize(session, auth, resource_type=table, resource_id=row_id, action="write"):
            authorized.append(row_id)
        else:
            skipped.append(row_id)
    return authorized, skipped


_TEAM_SUGGESTION_TABLES: tuple[
    tuple[Literal["repositories", "engineering_work_items"], str, str], ...
] = (
    ("repositories", "repository", "repository.team_assigned"),
    ("engineering_work_items", "engineering_work_item", "engineering_work_item.team_assigned"),
)


@router.post("/team-suggestions/confirm", response_model=TeamSuggestionActionResponse)
def confirm_team_suggestion_endpoint(
    payload: TeamSuggestionConfirmRequest,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
    idempotency_key: IdempotencyHeader,
) -> TeamSuggestionActionResponse:
    """Bulk sibling of `assign_repository_team_endpoint`/`assign_work_
    item_team_endpoint`: confirms every currently-unconfirmed, undismissed
    `repositories`/`engineering_work_items` row sharing one `suggested_
    team_name`, in one transaction. No `expected_version` -- see the
    design doc's "Backend endpoints" section for why a per-row version
    doesn't apply to a set-based confirm.
    """
    request_hash = _request_hash(payload, "confirm_team_suggestion")
    now = datetime.now(UTC)
    with session.begin():
        _lock_idempotency(session, auth, idempotency_key)
        cached = _load_cached(session, auth, idempotency_key, request_hash)
        if cached is not None:
            return TeamSuggestionActionResponse.model_validate(cached)

        _validate_team_entity(session, auth, payload.team_entity_id)

        updated: list[UUID] = []
        skipped: list[UUID] = []
        for table, aggregate_type, event_type in _TEAM_SUGGESTION_TABLES:
            authorized_ids, skipped_ids = _lock_and_authorize_suggestion_candidates(
                session, auth, table=table, suggested_team_name=payload.suggested_team_name
            )
            skipped.extend(skipped_ids)
            for row_id in authorized_ids:
                new_version = session.execute(
                    text(
                        f"UPDATE {table} SET team_entity_id = :team_entity_id, "  # noqa: S608
                        "team_assignment_version = team_assignment_version + 1, "
                        "team_assignment_updated_by = :actor_id, updated_at = :now "
                        "WHERE workspace_id = :workspace_id AND id = :id "
                        "RETURNING team_assignment_version"
                    ),
                    {
                        "team_entity_id": payload.team_entity_id,
                        "actor_id": auth.user_id,
                        "now": now,
                        "workspace_id": auth.workspace_id,
                        "id": row_id,
                    },
                ).scalar_one()
                _write_team_assignment_side_effects(
                    session, auth, request,
                    aggregate_type=aggregate_type, event_type=event_type,
                    aggregate_id=row_id, version=new_version, now=now,
                )
                updated.append(row_id)

        response = TeamSuggestionActionResponse(updated=updated, skipped_unauthorized=skipped)
        _store_idempotency(
            session, auth, idempotency_key, request_hash, response.model_dump(mode="json"), now
        )
        return response
```

- [ ] **Step 4: Run test to verify it passes**

Run: `CI=1 ECC_DATABASE_URL=postgresql+psycopg://ecc:ecc@localhost:5432/ecc .venv/bin/python -m pytest tests/test_engineering_team_suggestions_postgres.py -v`
Expected: PASS (all tests so far)

- [ ] **Step 5: Commit**

```bash
git add backend/ecc/domains/engineering/connector_accounts.py tests/test_engineering_team_suggestions_postgres.py
git commit -m "feat(engineering): add POST /team-suggestions/confirm bulk endpoint"
```

---

### Task 7: Backend — `POST /api/v1/engineering/team-suggestions/dismiss`

**Files:**
- Modify: `backend/ecc/domains/engineering/connector_accounts.py` (add endpoint after Task 6's)
- Test: `tests/test_engineering_team_suggestions_postgres.py`

**Interfaces:**
- Consumes: `TeamSuggestionDismissRequest`, `TeamSuggestionActionResponse`, `_lock_and_authorize_suggestion_candidates` (Task 6).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_engineering_team_suggestions_postgres.py`:

```python
def test_dismiss_team_suggestion_hides_it_without_assigning_team(suggestions_context) -> None:
    client, workspace_id, user_id, token = suggestions_context
    account_id = _insert_connector_account(workspace_id)
    _insert_repository(workspace_id, account_id, user_id, name="acme/a", suggested_team_name="Personal Namespace")

    response = client.post(
        "/api/v1/engineering/team-suggestions/dismiss",
        json={"suggested_team_name": "Personal Namespace"},
        headers=_headers(token, key=str(uuid4())),
    )
    assert response.status_code == 200
    assert len(response.json()["updated"]) == 1

    with engine.begin() as connection:
        row = connection.execute(
            text(
                "SELECT team_entity_id, team_suggestion_dismissed_at FROM repositories "
                "WHERE workspace_id = :workspace_id"
            ),
            {"workspace_id": workspace_id},
        ).one()
    assert row.team_entity_id is None
    assert row.team_suggestion_dismissed_at is not None

    list_response = client.get("/api/v1/engineering/team-suggestions", headers=_headers(token))
    assert list_response.json()["items"] == []


def test_dismiss_team_suggestion_is_idempotent_on_replay(suggestions_context) -> None:
    client, workspace_id, user_id, token = suggestions_context
    account_id = _insert_connector_account(workspace_id)
    _insert_repository(workspace_id, account_id, user_id, name="acme/a", suggested_team_name="Personal Namespace")
    key = str(uuid4())
    payload = {"suggested_team_name": "Personal Namespace"}

    first = client.post(
        "/api/v1/engineering/team-suggestions/dismiss", json=payload, headers=_headers(token, key=key)
    )
    second = client.post(
        "/api/v1/engineering/team-suggestions/dismiss", json=payload, headers=_headers(token, key=key)
    )
    assert first.json() == second.json()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `CI=1 ECC_DATABASE_URL=postgresql+psycopg://ecc:ecc@localhost:5432/ecc .venv/bin/python -m pytest tests/test_engineering_team_suggestions_postgres.py -k dismiss -v`
Expected: FAIL — `404 Not Found`.

- [ ] **Step 3: Write minimal implementation**

In `backend/ecc/domains/engineering/connector_accounts.py`, add after Task 6's `confirm_team_suggestion_endpoint`:

```python
@router.post("/team-suggestions/dismiss", response_model=TeamSuggestionActionResponse)
def dismiss_team_suggestion_endpoint(
    payload: TeamSuggestionDismissRequest,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
    idempotency_key: IdempotencyHeader,
) -> TeamSuggestionActionResponse:
    """Bulk-hides every currently-unconfirmed, undismissed row sharing one
    `suggested_team_name`, without assigning any team. No audit event --
    unlike confirm, dismissing writes no durable link a future reader
    needs an audit trail for; the adapter-side reset rule (Tasks 2-4) is
    what keeps this from permanently suppressing a since-changed
    suggestion.
    """
    request_hash = _request_hash(payload, "dismiss_team_suggestion")
    now = datetime.now(UTC)
    with session.begin():
        _lock_idempotency(session, auth, idempotency_key)
        cached = _load_cached(session, auth, idempotency_key, request_hash)
        if cached is not None:
            return TeamSuggestionActionResponse.model_validate(cached)

        updated: list[UUID] = []
        skipped: list[UUID] = []
        for table, _aggregate_type, _event_type in _TEAM_SUGGESTION_TABLES:
            authorized_ids, skipped_ids = _lock_and_authorize_suggestion_candidates(
                session, auth, table=table, suggested_team_name=payload.suggested_team_name
            )
            skipped.extend(skipped_ids)
            for row_id in authorized_ids:
                session.execute(
                    text(
                        f"UPDATE {table} SET team_suggestion_dismissed_at = :now "  # noqa: S608
                        "WHERE workspace_id = :workspace_id AND id = :id"
                    ),
                    {"now": now, "workspace_id": auth.workspace_id, "id": row_id},
                )
                updated.append(row_id)

        response = TeamSuggestionActionResponse(updated=updated, skipped_unauthorized=skipped)
        _store_idempotency(
            session, auth, idempotency_key, request_hash, response.model_dump(mode="json"), now
        )
        return response
```

- [ ] **Step 4: Run test to verify it passes**

Run: `CI=1 ECC_DATABASE_URL=postgresql+psycopg://ecc:ecc@localhost:5432/ecc .venv/bin/python -m pytest tests/test_engineering_team_suggestions_postgres.py -v`
Expected: PASS (all tests in the file)

- [ ] **Step 5: Commit**

```bash
git add backend/ecc/domains/engineering/connector_accounts.py tests/test_engineering_team_suggestions_postgres.py
git commit -m "feat(engineering): add POST /team-suggestions/dismiss bulk endpoint"
```

---

### Task 8: Frontend — types

**Files:**
- Modify: `frontend/src/features/engineering/types.ts`

**Interfaces:**
- Produces: `TeamSuggestionSampleItem`, `TeamSuggestionGroup`, `TeamSuggestionListResponse`, `TeamSuggestionConfirmRequest`, `TeamSuggestionDismissRequest`, `TeamSuggestionActionResponse` types, and `EngineeringView` gaining `'team-suggestions'` — all consumed by Task 9.

This task has no independent test (it's type declarations only, verified by Tasks 9/10's typecheck) — fold it into Task 9 by doing this edit first, then Task 9's steps. Do not commit separately; commit together with Task 9.

- [ ] **Step 1: Add the `'team-suggestions'` view and new types**

In `frontend/src/features/engineering/types.ts`, change:

```ts
export type EngineeringView =
  | 'overview'
  | 'delivery'
  | 'reliability'
  | 'repositories'
  | 'work-items'
  | 'incidents'
  | 'decisions'
  | 'connector-health'
  | 'coverage'
```

to:

```ts
export type EngineeringView =
  | 'overview'
  | 'delivery'
  | 'reliability'
  | 'repositories'
  | 'work-items'
  | 'team-suggestions'
  | 'incidents'
  | 'decisions'
  | 'connector-health'
  | 'coverage'
```

Then append at the end of the file, after `TeamAssignmentRequest`:

```ts
// --- Team suggestions review (`GET|POST /engineering/team-suggestions*`) --

export type TeamSuggestionSampleItem = {
  id: string
  resource_type: 'repository' | 'work_item'
  name: string
}

export type TeamSuggestionGroup = {
  suggested_team_name: string
  repository_count: number
  work_item_count: number
  sample_items: TeamSuggestionSampleItem[]
}

export type TeamSuggestionListResponse = { items: TeamSuggestionGroup[] }

export type TeamSuggestionConfirmRequest = { suggested_team_name: string; team_entity_id: string }
export type TeamSuggestionDismissRequest = { suggested_team_name: string }
export type TeamSuggestionActionResponse = { updated: string[]; skipped_unauthorized: string[] }
```

Proceed directly to Task 9 — no test/commit split for this task alone.

---

### Task 9: Frontend — `TeamSuggestionsPanel.tsx` + workspace wiring

**Files:**
- Create: `frontend/src/features/engineering/TeamSuggestionsPanel.tsx`
- Modify: `frontend/src/features/engineering/EngineeringWorkspace.tsx`
- Test: Create `frontend/src/features/engineering/TeamSuggestionsPanel.test.tsx` (written first, per TDD, then made to pass)

**Interfaces:**
- Consumes: types from Task 8, `apiRequest` from `../../api/client`, `EntityList` from `../knowledge/types` (same as `RepositoriesPanel.tsx`).
- Produces: default-exported `TeamSuggestionsPanel` component, consumed by `EngineeringWorkspace.tsx`.

- [ ] **Step 1: Write the failing test**

Create `frontend/src/features/engineering/TeamSuggestionsPanel.test.tsx`:

```tsx
// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import TeamSuggestionsPanel from './TeamSuggestionsPanel'
import type { TeamSuggestionGroup } from './types'

function response(body: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }))
}

function group(overrides: Partial<TeamSuggestionGroup> = {}): TeamSuggestionGroup {
  return {
    suggested_team_name: 'acme',
    repository_count: 2,
    work_item_count: 1,
    sample_items: [
      { id: 'repo-1', resource_type: 'repository', name: 'acme/widgets' },
      { id: 'repo-2', resource_type: 'repository', name: 'acme/gadgets' },
      { id: 'wi-1', resource_type: 'work_item', name: 'ACME-1' },
    ],
    ...overrides,
  }
}

function stubFetch({
  groups,
  teams = [],
}: {
  groups: TeamSuggestionGroup[]
  teams?: { id: string; canonical_name: string }[]
}) {
  // Stateful, not a fixed response -- proves confirm/dismiss remove the
  // group from the next refetch, mirroring RepositoriesPanel.test.tsx's
  // own identical pattern for the same reason.
  const state = groups.map((g) => ({ ...g }))
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.includes('/knowledge/entities')) return response({ items: teams })
      if (init?.method === 'POST' && url.includes('/team-suggestions/confirm')) {
        const body = JSON.parse(String(init.body)) as { suggested_team_name: string }
        const index = state.findIndex((g) => g.suggested_team_name === body.suggested_team_name)
        const removed = index >= 0 ? state.splice(index, 1)[0] : undefined
        const count = removed ? removed.repository_count + removed.work_item_count : 0
        return response({
          updated: Array.from({ length: count }, (_, i) => `id-${i}`),
          skipped_unauthorized: [],
        })
      }
      if (init?.method === 'POST' && url.includes('/team-suggestions/dismiss')) {
        const body = JSON.parse(String(init.body)) as { suggested_team_name: string }
        const index = state.findIndex((g) => g.suggested_team_name === body.suggested_team_name)
        if (index >= 0) state.splice(index, 1)
        return response({ updated: [], skipped_unauthorized: [] })
      }
      return response({ items: state })
    }),
  )
}

function renderPanel() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={client}><TeamSuggestionsPanel /></QueryClientProvider>)
}

afterEach(() => { cleanup(); vi.unstubAllGlobals() })

describe('TeamSuggestionsPanel', () => {
  it('shows an empty state when there are no pending suggestions', async () => {
    stubFetch({ groups: [] })
    renderPanel()
    expect(await screen.findByText('No pending team suggestions.')).toBeTruthy()
  })

  it('lists a suggestion group with its repository/work-item counts', async () => {
    stubFetch({ groups: [group()] })
    renderPanel()
    expect(await screen.findByText('acme')).toBeTruthy()
    expect(screen.getByText('2 repositories · 1 work items (3 total)')).toBeTruthy()
  })

  it('shows the sample items for a group', async () => {
    stubFetch({ groups: [group()] })
    renderPanel()
    expect(await screen.findByText('acme/widgets (repository)')).toBeTruthy()
    expect(screen.getByText('ACME-1 (work_item)')).toBeTruthy()
  })

  it('disables Confirm until a team is picked', async () => {
    stubFetch({ groups: [group()], teams: [{ id: 'team-1', canonical_name: 'Platform' }] })
    renderPanel()
    await screen.findByText('acme')
    const confirmButton = screen.getByRole('button', { name: 'Confirm' })
    expect(confirmButton.hasAttribute('disabled')).toBe(true)
    fireEvent.change(screen.getByLabelText('Assign team for acme'), { target: { value: 'team-1' } })
    expect(confirmButton.hasAttribute('disabled')).toBe(false)
  })

  it('confirming posts the suggested name and chosen team, then removes the group', async () => {
    stubFetch({ groups: [group()], teams: [{ id: 'team-1', canonical_name: 'Platform' }] })
    renderPanel()
    await screen.findByText('acme')
    fireEvent.change(screen.getByLabelText('Assign team for acme'), { target: { value: 'team-1' } })
    fireEvent.click(screen.getByRole('button', { name: 'Confirm' }))

    await waitFor(() => expect(screen.queryByText('acme')).toBeNull())
    const calls = (fetch as unknown as { mock: { calls: [RequestInfo | URL, RequestInit?][] } }).mock.calls
    const postCall = calls.find(([, init]) => init?.method === 'POST')
    expect(String(postCall?.[0])).toContain('/api/v1/engineering/team-suggestions/confirm')
    expect(JSON.parse(String(postCall?.[1]?.body))).toEqual({ suggested_team_name: 'acme', team_entity_id: 'team-1' })
  })

  it('dismissing removes the group without assigning a team', async () => {
    stubFetch({ groups: [group()] })
    renderPanel()
    await screen.findByText('acme')
    fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }))

    await waitFor(() => expect(screen.queryByText('acme')).toBeNull())
    const calls = (fetch as unknown as { mock: { calls: [RequestInfo | URL, RequestInit?][] } }).mock.calls
    const postCall = calls.find(([, init]) => init?.method === 'POST')
    expect(String(postCall?.[0])).toContain('/api/v1/engineering/team-suggestions/dismiss')
    expect(JSON.parse(String(postCall?.[1]?.body))).toEqual({ suggested_team_name: 'acme' })
  })

  it('shows a partial-authorization message when some items are skipped', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input)
        if (url.includes('/knowledge/entities')) return response({ items: [{ id: 'team-1', canonical_name: 'Platform' }] })
        if (init?.method === 'POST' && url.includes('/confirm')) {
          return response({ updated: ['repo-1', 'repo-2'], skipped_unauthorized: ['wi-1'] })
        }
        return response({ items: [group()] })
      }),
    )
    renderPanel()
    await screen.findByText('acme')
    fireEvent.change(screen.getByLabelText('Assign team for acme'), { target: { value: 'team-1' } })
    fireEvent.click(screen.getByRole('button', { name: 'Confirm' }))

    expect(
      await screen.findByText('Applied to 2 of 3 — 1 skipped: insufficient permission.'),
    ).toBeTruthy()
  })

  it('surfaces a load failure as an alert, not a silent empty list', async () => {
    vi.stubGlobal('fetch', vi.fn(() => Promise.reject(new TypeError('fetch failed'))))
    renderPanel()
    expect(await screen.findByRole('alert', {}, { timeout: 3000 })).toBeTruthy()
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pnpm --filter @ecc/frontend test -- --run TeamSuggestionsPanel`
Expected: FAIL — module `./TeamSuggestionsPanel` doesn't exist yet.

- [ ] **Step 3: Write minimal implementation**

First, apply Task 8's `types.ts` edit if not already done.

Create `frontend/src/features/engineering/TeamSuggestionsPanel.tsx`:

```tsx
import { useState } from 'react'

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { apiRequest } from '../../api/client'
import type { EntityList } from '../knowledge/types'
import type {
  TeamSuggestionActionResponse,
  TeamSuggestionConfirmRequest,
  TeamSuggestionDismissRequest,
  TeamSuggestionGroup,
  TeamSuggestionListResponse,
} from './types'

function listTeams(): Promise<EntityList> {
  return apiRequest('/api/v1/knowledge/entities?kind=team&status=active&limit=100')
}

function confirmSuggestion(suggestedTeamName: string, teamEntityId: string): Promise<TeamSuggestionActionResponse> {
  const body: TeamSuggestionConfirmRequest = { suggested_team_name: suggestedTeamName, team_entity_id: teamEntityId }
  return apiRequest('/api/v1/engineering/team-suggestions/confirm', { method: 'POST', body })
}

function dismissSuggestion(suggestedTeamName: string): Promise<TeamSuggestionActionResponse> {
  const body: TeamSuggestionDismissRequest = { suggested_team_name: suggestedTeamName }
  return apiRequest('/api/v1/engineering/team-suggestions/dismiss', { method: 'POST', body })
}

/**
 * One group's row: bulk-confirm (via a team picker) or bulk-dismiss every
 * `repositories`/`engineering_work_items` row sharing this `suggested_
 * team_name`. See `docs/superpowers/specs/2026-08-06-team-suggestions-
 * review-page-design.md`'s "Frontend" section.
 */
function SuggestionRow({ group, teamsById }: { group: TeamSuggestionGroup; teamsById: Map<string, string> }) {
  const queryClient = useQueryClient()
  const [teamEntityId, setTeamEntityId] = useState('')
  const [lastResult, setLastResult] = useState<TeamSuggestionActionResponse | null>(null)

  function invalidate() {
    void queryClient.invalidateQueries({ queryKey: ['engineering', 'team-suggestions'] })
    void queryClient.invalidateQueries({ queryKey: ['engineering', 'repositories'] })
    void queryClient.invalidateQueries({ queryKey: ['engineering', 'work-items'] })
  }

  const confirmMutation = useMutation({
    mutationFn: () => confirmSuggestion(group.suggested_team_name, teamEntityId),
    onSuccess: (result) => { setLastResult(result); invalidate() },
  })
  const dismissMutation = useMutation({
    mutationFn: () => dismissSuggestion(group.suggested_team_name),
    onSuccess: (result) => { setLastResult(result); invalidate() },
  })
  const busy = confirmMutation.isPending || dismissMutation.isPending
  const total = group.repository_count + group.work_item_count

  return (
    <li>
      <div>
        <strong>{group.suggested_team_name}</strong>
        <small>{`${group.repository_count} repositories · ${group.work_item_count} work items (${total} total)`}</small>
      </div>
      <ul>
        {group.sample_items.map((item) => (
          <li key={`${item.resource_type}-${item.id}`}>{`${item.name} (${item.resource_type})`}</li>
        ))}
      </ul>
      <div className="work-actions">
        <label>
          {`Assign team for ${group.suggested_team_name}`}
          <select
            aria-label={`Assign team for ${group.suggested_team_name}`}
            value={teamEntityId}
            disabled={busy}
            onChange={(event) => setTeamEntityId(event.target.value)}
          >
            <option value="">Select a team…</option>
            {[...teamsById.entries()].map(([id, name]) => <option key={id} value={id}>{name}</option>)}
          </select>
        </label>
        <button type="button" disabled={!teamEntityId || busy} onClick={() => confirmMutation.mutate()}>
          Confirm
        </button>
        <button type="button" disabled={busy} onClick={() => dismissMutation.mutate()}>
          Dismiss
        </button>
      </div>
      {confirmMutation.isError ? <span role="alert" className="inline-status error-panel">{confirmMutation.error.message}</span> : null}
      {dismissMutation.isError ? <span role="alert" className="inline-status error-panel">{dismissMutation.error.message}</span> : null}
      {lastResult && lastResult.skipped_unauthorized.length > 0 ? (
        <p role="status">
          {`Applied to ${lastResult.updated.length} of ${lastResult.updated.length + lastResult.skipped_unauthorized.length} — ${lastResult.skipped_unauthorized.length} skipped: insufficient permission.`}
        </p>
      ) : null}
    </li>
  )
}

/**
 * `GET /api/v1/engineering/team-suggestions` -- the grouped, bulk-action
 * review surface for migration `0050_phase6_team_linkage.py`'s "hybrid:
 * auto-suggest, human confirms" design. `RepositoriesPanel.tsx`'s own
 * per-row `TeamAssignment` component still exists unchanged for browsing
 * one repository; this panel clears every pending suggestion across a
 * workspace's connected GitHub, GitLab, and Jira accounts in as few
 * actions as possible.
 */
export default function TeamSuggestionsPanel() {
  const query = useQuery({
    queryKey: ['engineering', 'team-suggestions'],
    queryFn: () => apiRequest<TeamSuggestionListResponse>('/api/v1/engineering/team-suggestions'),
    retry: 1,
  })
  const teamsQuery = useQuery({ queryKey: ['knowledge', 'entities', 'team'], queryFn: listTeams, retry: 1 })

  const items = query.data?.items ?? []
  const teamsById = new Map((teamsQuery.data?.items ?? []).map((entity) => [entity.id, entity.canonical_name]))

  return (
    <section className="work-panel" aria-labelledby="engineering-team-suggestions-title">
      <h2 id="engineering-team-suggestions-title">Team suggestions</h2>
      <p>Repositories and work items still waiting on a confirmed team, grouped by their suggested name so you can confirm or dismiss every one sharing a name in one action.</p>

      {query.isLoading ? <p role="status">Loading team suggestions…</p> : null}
      {query.isError ? <div role="alert" className="inline-status error-panel">{query.error.message}</div> : null}
      {query.data && items.length === 0 ? <p className="empty-state">No pending team suggestions.</p> : null}

      <ul className="work-list">
        {items.map((group) => <SuggestionRow key={group.suggested_team_name} group={group} teamsById={teamsById} />)}
      </ul>
    </section>
  )
}
```

Then wire it into `EngineeringWorkspace.tsx`:

```ts
import RepositoriesPanel from './RepositoriesPanel'
import WorkItemsPanel from './WorkItemsPanel'
import TeamSuggestionsPanel from './TeamSuggestionsPanel'
```

(add the `TeamSuggestionsPanel` import line after `WorkItemsPanel`'s)

```ts
const TABS: ReadonlyArray<{ view: EngineeringView; label: string }> = [
  { view: 'overview', label: 'Overview' },
  { view: 'delivery', label: 'Delivery' },
  { view: 'reliability', label: 'Reliability' },
  { view: 'repositories', label: 'Repositories' },
  { view: 'work-items', label: 'Work items' },
  { view: 'team-suggestions', label: 'Team suggestions' },
  { view: 'incidents', label: 'Incidents' },
  { view: 'decisions', label: 'Decisions' },
  { view: 'connector-health', label: 'Connector health' },
  { view: 'coverage', label: 'Source coverage' },
]
```

```ts
        {view === 'overview' ? <EngineeringOverview onNavigate={setView} />
          : view === 'delivery' ? <DeliveryPanel />
          : view === 'reliability' ? <ReliabilityPanel />
          : view === 'repositories' ? <RepositoriesPanel />
          : view === 'work-items' ? <WorkItemsPanel />
          : view === 'team-suggestions' ? <TeamSuggestionsPanel />
          : view === 'incidents' ? <IncidentsPanel />
          : view === 'decisions' ? <DecisionsPanel />
          : view === 'connector-health' ? <ConnectorHealthPanel />
          : <CoveragePanel />}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pnpm --filter @ecc/frontend test -- --run TeamSuggestionsPanel`
Expected: PASS (all tests in `TeamSuggestionsPanel.test.tsx`)

Also run the full frontend suite and typecheck to confirm the `EngineeringWorkspace.tsx`/`types.ts` edits didn't break anything:

```bash
pnpm --filter @ecc/frontend typecheck
pnpm --filter @ecc/frontend test -- --run
```

- [ ] **Step 5: Commit**

```bash
git add frontend/src/features/engineering/types.ts frontend/src/features/engineering/TeamSuggestionsPanel.tsx frontend/src/features/engineering/TeamSuggestionsPanel.test.tsx frontend/src/features/engineering/EngineeringWorkspace.tsx
git commit -m "feat(frontend): add Team suggestions review tab to Engineering workspace"
```

---

## Final verification (after Task 9)

Run the full backend and frontend suites once more to confirm nothing regressed end-to-end:

```bash
CI=1 ECC_DATABASE_URL=postgresql+psycopg://ecc:ecc@localhost:5432/ecc .venv/bin/python -m pytest tests/test_engineering_team_suggestions_postgres.py tests/test_engineering_github_sync_postgres.py tests/test_engineering_gitlab_sync_postgres.py tests/test_engineering_jira_sync_postgres.py -q
pnpm --filter @ecc/frontend typecheck
pnpm --filter @ecc/frontend test -- --run
```
