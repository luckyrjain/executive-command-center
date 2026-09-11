# Navigation badge-count endpoints Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add 4 small `GET .../count` endpoints (attention, tasks, resolution candidates, recommendations) so the frontend navigation-redesign sidebar can show a badge count next to 6 workspaces (2 of the 6 — Risks, Automation — already have this covered by existing unbounded list endpoints and need no backend change).

**Architecture:** Each new endpoint is a thin sibling of an existing `list_*` endpoint in the same router file: same auth/session dependencies, same `authz.visible_resource_filter_sql` visibility scoping, same `WHERE` clause (minus pagination/ordering), but `SELECT COUNT(*)` instead of `SELECT <fields> ... LIMIT`. No new tables, no new business logic, no new dependencies.

**Tech Stack:** FastAPI, SQLAlchemy Core (`sqlalchemy.text`), Pydantic, pytest against a real PostgreSQL instance (existing `pytestmark = pytest.mark.skipif(not settings.database_url.startswith("postgresql"), ...)` convention).

**Spec:** [docs/superpowers/specs/2026-09-11-navigation-redesign-design.md](../specs/2026-09-11-navigation-redesign-design.md) — "Badge counts" section. This plan covers only the 4 backend endpoints that section lists as needing new work; it does not touch the frontend sidebar, routing, or the 2 free (Risks/Automation) counts.

## Global Constraints

- Every new endpoint reuses its sibling list endpoint's exact `authz.visible_resource_filter_sql(session, auth, resource_type=..., action="read", table_alias=...)` call with the same `resource_type`/`table_alias` — never a new visibility rule.
- Every new endpoint uses the same `AuthDep`/`SessionDep` pair already imported in its file — no new auth dependency.
- Response shape is always `{"count": int}` — a small `BaseModel` named `<Domain>Count`, placed next to that domain's existing response models (inline in the router file for attention/tasks/knowledge-resolution; in `recommendation_models.py` for governance, since that's where `RecommendationListResponse` already lives).
- A new `/count` (or `/candidates/count`) route must be registered **before** any existing `/{id}`-shaped GET route in the same router — FastAPI/Starlette matches routes in registration order, and a path param would otherwise swallow the literal `count` segment. Every task below states exactly where to insert the new route for this reason.
- Every new endpoint ends with `session.rollback()` if its sibling list endpoint does (attention, knowledge-resolution, recommendations all do; tasks does not) — matches the existing "close a read-only transaction cleanly" convention in this codebase, not a new pattern.
- This plan runs in a separate worktree/branch (`feat/nav-badge-count-endpoints`) from the frontend sidebar/routing work, so it can be reviewed and merged independently. Do not touch any file under `frontend/`.

---

### Task 1: Attention count endpoint

**Files:**
- Modify: `backend/ecc/domains/attention/attention.py` (insert after `list_attention`, which ends immediately before the line `@router.get("/{item_id}", response_model=AttentionItem)`)
- Test: `tests/test_attention_count_postgres.py` (create)

**Interfaces:**
- Produces: `GET /api/v1/attention/count` → `AttentionCount` (`{"count": int}`), a new Pydantic model `AttentionCount` defined in `attention.py`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_attention_count_postgres.py`:

```python
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from identity_fixtures import create_identity
from sqlalchemy import text

from ecc.config import get_settings
from ecc.database import engine
from ecc.main import app

settings = get_settings()
pytestmark = pytest.mark.skipif(
    not settings.database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)


@pytest.fixture
def attention_count_context() -> Iterator[tuple[TestClient, UUID, UUID, str]]:
    workspace_id = uuid4()
    user_id = uuid4()
    token = f"session-{uuid4()}"
    now = datetime.now(UTC)

    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, 'Attention Count Test', 'Asia/Kolkata', :created_at)"
            ),
            {"id": workspace_id, "created_at": now},
        )
        create_identity(
            connection,
            workspace_id=workspace_id,
            user_id=user_id,
            email=f"{user_id}@example.test",
            now=now,
        )
        connection.execute(
            text(
                "INSERT INTO sessions (id, workspace_id, user_id, token_hash, "
                "expires_at, last_seen_at) "
                "VALUES (:id, :workspace_id, :user_id, :token_hash, :expires_at, :last_seen_at)"
            ),
            {
                "id": uuid4(),
                "workspace_id": workspace_id,
                "user_id": user_id,
                "token_hash": sha256(token.encode()).hexdigest(),
                "expires_at": now + timedelta(hours=1),
                "last_seen_at": now,
            },
        )

    client = TestClient(app)
    client.cookies.set("ecc_session", token)
    try:
        yield client, workspace_id, user_id, token
    finally:
        client.close()
        with engine.begin() as connection:
            for table in ("attention_items", "sessions", "users", "workspaces"):
                connection.execute(
                    text(f"DELETE FROM {table} WHERE workspace_id = :workspace_id")
                    if table != "workspaces"
                    else text(f"DELETE FROM {table} WHERE id = :workspace_id"),
                    {"workspace_id": workspace_id},
                )


def _insert_attention_item(workspace_id: UUID, owner_id: UUID, now: datetime) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO attention_items (
                    id, workspace_id, entity_type, entity_id, source_entity_version,
                    score, confidence, factors, explanation, generated_at, expires_at,
                    pinned, policy_version, owner_id, visibility
                ) VALUES (
                    :id, :workspace_id, 'task', :entity_id, 1,
                    50, 0.9, '[]'::jsonb, 'test item', :now, :expires_at,
                    false, 1, :owner_id, 'private'
                )
                """
            ),
            {
                "id": uuid4(),
                "workspace_id": workspace_id,
                "entity_id": uuid4(),
                "now": now,
                "expires_at": now + timedelta(days=1),
                "owner_id": owner_id,
            },
        )


def test_attention_count_matches_list_length(
    attention_count_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, user_id, _token = attention_count_context
    now = datetime.now(UTC)
    _insert_attention_item(workspace_id, user_id, now)
    _insert_attention_item(workspace_id, user_id, now)

    listed = client.get("/api/v1/attention")
    assert listed.status_code == 200
    assert len(listed.json()["items"]) == 2

    counted = client.get("/api/v1/attention/count")
    assert counted.status_code == 200
    assert counted.json() == {"count": 2}


def test_attention_count_is_zero_for_an_empty_workspace(
    attention_count_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, _token = attention_count_context
    counted = client.get("/api/v1/attention/count")
    assert counted.status_code == 200
    assert counted.json() == {"count": 0}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_attention_count_postgres.py -v`
Expected: FAIL — `404 Not Found` on `GET /api/v1/attention/count` (route doesn't exist yet).

- [ ] **Step 3: Write minimal implementation**

In `backend/ecc/domains/attention/attention.py`, insert this immediately after the `list_attention` function and before `@router.get("/{item_id}", response_model=AttentionItem)`:

```python
class AttentionCount(BaseModel):
    count: int


@router.get("/count", response_model=AttentionCount)
def count_attention(auth: AuthDep, session: SessionDep) -> AttentionCount:
    now = datetime.now(UTC)
    visibility_sql, visibility_params = authz.visible_resource_filter_sql(
        session, auth, resource_type="attention_items", action="read", table_alias="ai"
    )
    count = session.execute(
        text(f"""
            SELECT COUNT(*)
            FROM attention_items ai
            WHERE ai.workspace_id = :workspace_id
              AND ai.expires_at > :now
              AND (ai.dismissed_at IS NULL
                   OR ai.dismissed_entity_version <> ai.source_entity_version)
              AND (ai.deferred_until IS NULL OR ai.deferred_until <= :now)
              AND ({visibility_sql})
        """),
        {"workspace_id": auth.workspace_id, "now": now, **visibility_params},
    ).scalar_one()
    session.rollback()
    return AttentionCount(count=count)
```

This must be placed **before** the `@router.get("/{item_id}", ...)` route in the file (registration order matters — see Global Constraints).

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_attention_count_postgres.py -v`
Expected: PASS (both tests).

- [ ] **Step 5: Commit**

```bash
git add backend/ecc/domains/attention/attention.py tests/test_attention_count_postgres.py
git commit -m "feat(attention): add GET /api/v1/attention/count for the nav sidebar badge"
```

---

### Task 2: Tasks count endpoint

**Files:**
- Modify: `backend/ecc/domains/planning/tasks.py` (insert after `list_tasks`, which ends immediately before `@router.get("/{task_id}", response_model=TaskResponse)`)
- Test: `tests/test_tasks_count_postgres.py` (create)

**Interfaces:**
- Produces: `GET /api/v1/tasks/count` → `TaskCount` (`{"count": int}`), a new Pydantic model `TaskCount` defined in `tasks.py`.
- "Open" is defined as: `archived_at IS NULL AND status NOT IN ('completed', 'cancelled')` — the two terminal `TaskStatus` values in this file's own `Literal["captured", "planned", "in_progress", "blocked", "completed", "cancelled"]`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_tasks_count_postgres.py`:

```python
from collections.abc import Iterator
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
pytestmark = pytest.mark.skipif(
    not settings.database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)


@pytest.fixture
def tasks_count_context() -> Iterator[tuple[TestClient, UUID, UUID, str]]:
    workspace_id = uuid4()
    user_id = uuid4()
    token = f"session-{uuid4()}"
    now = datetime.now(UTC)

    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, 'Tasks Count Test', 'Asia/Kolkata', :created_at)"
            ),
            {"id": workspace_id, "created_at": now},
        )
        create_identity(
            connection,
            workspace_id=workspace_id,
            user_id=user_id,
            email=f"{user_id}@example.test",
            now=now,
        )
        connection.execute(
            text(
                "INSERT INTO sessions (id, workspace_id, user_id, token_hash, "
                "expires_at, last_seen_at) "
                "VALUES (:id, :workspace_id, :user_id, :token_hash, :expires_at, :last_seen_at)"
            ),
            {
                "id": uuid4(),
                "workspace_id": workspace_id,
                "user_id": user_id,
                "token_hash": sha256(token.encode()).hexdigest(),
                "expires_at": now + timedelta(hours=1),
                "last_seen_at": now,
            },
        )

    client = TestClient(app)
    client.cookies.set("ecc_session", token)
    try:
        yield client, workspace_id, user_id, token
    finally:
        client.close()
        with engine.begin() as connection:
            for table in ("tasks", "sessions", "users", "workspaces"):
                connection.execute(
                    text(f"DELETE FROM {table} WHERE id = :workspace_id")
                    if table == "workspaces"
                    else text(f"DELETE FROM {table} WHERE workspace_id = :workspace_id"),
                    {"workspace_id": workspace_id},
                )


def _headers(token: str, key: str) -> dict[str, str]:
    # Mirrors tests/test_knowledge_resolution_postgres.py's own `_headers` --
    # every mutating endpoint in this app requires both an Idempotency-Key
    # and an HMAC CSRF token derived from the session token.
    csrf = new(settings.session_secret.encode(), token.encode(), "sha256").hexdigest()
    return {"Idempotency-Key": key, "X-CSRF-Token": csrf}


def _create_task(client: TestClient, token: str, title: str) -> str:
    response = client.post(
        "/api/v1/tasks",
        json={"title": title},
        headers=_headers(token, f"count-test-{uuid4()}"),
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_tasks_count_excludes_completed_and_archived(
    tasks_count_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = tasks_count_context
    _create_task(client, token, "open task one")
    _create_task(client, token, "open task two")
    completed_id = _create_task(client, token, "will be completed")
    client.post(
        f"/api/v1/tasks/{completed_id}/complete",
        headers=_headers(token, f"count-test-{uuid4()}"),
    )

    counted = client.get("/api/v1/tasks/count")
    assert counted.status_code == 200
    assert counted.json() == {"count": 2}
```

`POST /api/v1/tasks/{task_id}/complete` is the real, confirmed route (`backend/ecc/domains/planning/tasks.py:839`) — this test calls it directly, not a guess.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_tasks_count_postgres.py -v`
Expected: FAIL — `404 Not Found` on `GET /api/v1/tasks/count`.

- [ ] **Step 3: Write minimal implementation**

In `backend/ecc/domains/planning/tasks.py`, insert this immediately after `list_tasks` and before `@router.get("/{task_id}", response_model=TaskResponse)`:

```python
class TaskCount(BaseModel):
    count: int


@router.get("/count", response_model=TaskCount)
def count_tasks(auth: AuthDep, session: SessionDep) -> TaskCount:
    visibility_sql, visibility_params = authz.visible_resource_filter_sql(
        session, auth, resource_type="tasks", action="read", table_alias="tasks"
    )
    count = session.execute(
        text(f"""
            SELECT COUNT(*) FROM tasks
            WHERE workspace_id = :workspace_id
              AND ({visibility_sql})
              AND archived_at IS NULL
              AND status NOT IN ('completed', 'cancelled')
        """),
        {"workspace_id": auth.workspace_id, **visibility_params},
    ).scalar_one()
    return TaskCount(count=count)
```

No `session.rollback()` here — `list_tasks` in this same file doesn't call it either.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_tasks_count_postgres.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/ecc/domains/planning/tasks.py tests/test_tasks_count_postgres.py
git commit -m "feat(tasks): add GET /api/v1/tasks/count for the nav sidebar badge"
```

---

### Task 3: Knowledge resolution-candidates count endpoint

**Files:**
- Modify: `backend/ecc/domains/knowledge/resolution.py` (insert after `list_candidates`, before line 763's `@router.post("/candidates/{candidate_id}/confirm", ...)` — that route is a POST, not a GET, so there's no strict FastAPI ordering requirement against it, but insert here anyway to keep the count endpoint next to its list sibling)
- Test: `tests/test_resolution_count_postgres.py` (create)

**Interfaces:**
- Produces: `GET /api/v1/knowledge/resolution/candidates/count` → `ResolutionCandidateCount` (`{"count": int}`), a new Pydantic model defined in `resolution.py`.
- "Open" candidates: `status = 'open'` (the `CandidateStatus = Literal["open", "confirmed", "rejected", "expired"]` value already used by `list_candidates`'s own `status` query param).

- [ ] **Step 1: Write the failing test**

Create `tests/test_resolution_count_postgres.py`:

```python
from collections.abc import Iterator
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
pytestmark = pytest.mark.skipif(
    not settings.database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)


@pytest.fixture
def resolution_count_context() -> Iterator[tuple[TestClient, UUID, UUID, str]]:
    workspace_id = uuid4()
    user_id = uuid4()
    token = f"session-{uuid4()}"
    now = datetime.now(UTC)

    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, 'Resolution Count Test', 'Asia/Kolkata', :created_at)"
            ),
            {"id": workspace_id, "created_at": now},
        )
        create_identity(
            connection,
            workspace_id=workspace_id,
            user_id=user_id,
            email=f"{user_id}@example.test",
            now=now,
        )
        connection.execute(
            text(
                "INSERT INTO sessions (id, workspace_id, user_id, token_hash, "
                "expires_at, last_seen_at) "
                "VALUES (:id, :workspace_id, :user_id, :token_hash, :expires_at, :last_seen_at)"
            ),
            {
                "id": uuid4(),
                "workspace_id": workspace_id,
                "user_id": user_id,
                "token_hash": sha256(token.encode()).hexdigest(),
                "expires_at": now + timedelta(hours=1),
                "last_seen_at": now,
            },
        )

    client = TestClient(app)
    client.cookies.set("ecc_session", token)
    try:
        yield client, workspace_id, user_id, token
    finally:
        client.close()
        with engine.begin() as connection:
            for table in ("resolution_candidates", "pkos_nodes", "sessions", "users", "workspaces"):
                connection.execute(
                    text(f"DELETE FROM {table} WHERE id = :workspace_id")
                    if table == "workspaces"
                    else text(f"DELETE FROM {table} WHERE workspace_id = :workspace_id"),
                    {"workspace_id": workspace_id},
                )


def _headers(token: str, key: str) -> dict[str, str]:
    # Both entity creation (`entities.py`) and candidate creation
    # (`resolution.py::create_candidate`) require CSRF + Idempotency-Key,
    # same as every other mutating endpoint in this app.
    csrf = new(settings.session_secret.encode(), token.encode(), "sha256").hexdigest()
    return {"Idempotency-Key": key, "X-CSRF-Token": csrf}


def _create_entity(client: TestClient, token: str, key: str, kind: str, name: str) -> str:
    # EntityCreate (backend/ecc/domains/knowledge/entities.py) takes exactly
    # kind/canonical_name/summary -- confirmed against the real model, not guessed.
    response = client.post(
        "/api/v1/knowledge/entities",
        json={"kind": kind, "canonical_name": name},
        headers=_headers(token, key),
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_resolution_count_matches_open_candidates(
    resolution_count_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = resolution_count_context
    left_id = _create_entity(client, token, "count-left", "person", "Grace Hopper")
    right_id = _create_entity(client, token, "count-right", "person", "Grace Hoper")
    created = client.post(
        "/api/v1/knowledge/resolution/candidates",
        headers=_headers(token, "count-candidate"),
        json={"left_entity_id": left_id, "right_entity_id": right_id},
    )
    assert created.status_code == 201, created.text

    counted = client.get("/api/v1/knowledge/resolution/candidates/count")
    assert counted.status_code == 200
    assert counted.json() == {"count": 1}

    listed = client.get("/api/v1/knowledge/resolution/candidates", params={"status": "open"})
    assert len(listed.json()["items"]) == counted.json()["count"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_resolution_count_postgres.py -v`
Expected: FAIL — `404 Not Found` on `GET /api/v1/knowledge/resolution/candidates/count`.

- [ ] **Step 3: Write minimal implementation**

In `backend/ecc/domains/knowledge/resolution.py`, insert this immediately after `list_candidates` (after its closing `return ResolutionCandidateListResponse(...)` line):

```python
class ResolutionCandidateCount(BaseModel):
    count: int


@router.get("/candidates/count", response_model=ResolutionCandidateCount)
def count_candidates(
    auth: AuthDep, session: SessionDep, status: CandidateStatus | None = "open"
) -> ResolutionCandidateCount:
    visibility_sql, visibility_params = authz.visible_resource_filter_sql(
        session, auth, resource_type="resolution_candidates", action="read",
        table_alias="resolution_candidates",
    )
    left_visibility_sql, left_visibility_params = authz.visible_resource_filter_sql(
        session, auth, resource_type="pkos_nodes", action="read",
        table_alias="left_entity", param_prefix="left_entity_",
    )
    right_visibility_sql, right_visibility_params = authz.visible_resource_filter_sql(
        session, auth, resource_type="pkos_nodes", action="read",
        table_alias="right_entity", param_prefix="right_entity_",
    )
    clauses = [
        "resolution_candidates.workspace_id = :workspace_id",
        "(resolution_candidates.deferred_until IS NULL "
        "OR resolution_candidates.deferred_until <= :now)",
        f"({visibility_sql})",
        f"({left_visibility_sql})",
        f"({right_visibility_sql})",
    ]
    params: dict[str, object] = {
        "workspace_id": auth.workspace_id,
        "now": datetime.now(UTC),
        **visibility_params,
        **left_visibility_params,
        **right_visibility_params,
    }
    if status is not None:
        clauses.append("resolution_candidates.status = :status")
        params["status"] = status

    count = session.execute(
        text(f"""
            SELECT COUNT(*)
            FROM resolution_candidates
            JOIN pkos_nodes left_entity
              ON left_entity.workspace_id = resolution_candidates.workspace_id
             AND left_entity.id = resolution_candidates.left_entity_id
            JOIN pkos_nodes right_entity
              ON right_entity.workspace_id = resolution_candidates.workspace_id
             AND right_entity.id = resolution_candidates.right_entity_id
            WHERE {" AND ".join(clauses)}
        """),
        params,
    ).scalar_one()
    session.rollback()
    return ResolutionCandidateCount(count=count)
```

Note the default `status: CandidateStatus | None = "open"` — unlike `list_candidates` (which defaults to `None`, meaning "all statuses"), the sidebar badge specifically wants the "open" count, so this endpoint's own default narrows to that. A caller can still pass `?status=` empty to get an all-statuses count if some other consumer ever needs it, but the sidebar itself calls this with no query params and gets the open count by default.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_resolution_count_postgres.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/ecc/domains/knowledge/resolution.py tests/test_resolution_count_postgres.py
git commit -m "feat(knowledge): add GET /api/v1/knowledge/resolution/candidates/count for the nav sidebar badge"
```

---

### Task 4: Recommendations count endpoint

**Files:**
- Modify: `backend/ecc/domains/governance/recommendation_models.py` (add `RecommendationCount` model)
- Modify: `backend/ecc/domains/governance/recommendation_queries.py` (insert new endpoint after `list_recommendations`, before `@router.get("/{recommendation_id}", ...)` at line 115)
- Test: `tests/test_recommendations_count_postgres.py` (create)

**Interfaces:**
- Produces: `GET /api/v1/recommendations/count` → `RecommendationCount` (`{"count": int}`).
- "Pending" recommendations: `status IN ('proposed', 'pending_confirmation')` — the two not-yet-decided values of `RecommendationStatus`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_recommendations_count_postgres.py`. This mirrors `tests/test_recommendations_postgres.py`'s own `recommendation_context` fixture — read that file first to copy its exact fixture body (workspace/session setup is identical to the pattern used in Tasks 1-3 above, but this domain's teardown deletes `recommendations`, `recommendation_feedback`, `tasks`, `commitments`, `risks`, `sessions`, `users`, `workspaces` — copy the real fixture from that file verbatim rather than retyping it from memory, since recommendation creation there likely goes through a specific internal helper, not a public POST endpoint):

```python
from collections.abc import Iterator
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
pytestmark = pytest.mark.skipif(
    not settings.database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)


@pytest.fixture
def recommendation_count_context() -> Iterator[tuple[TestClient, UUID, UUID, str]]:
    workspace_id = uuid4()
    user_id = uuid4()
    token = f"session-{uuid4()}"
    now = datetime.now(UTC)

    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, 'Recommendation Count Test', 'Asia/Kolkata', :created_at)"
            ),
            {"id": workspace_id, "created_at": now},
        )
        create_identity(
            connection,
            workspace_id=workspace_id,
            user_id=user_id,
            email=f"{user_id}@example.test",
            now=now,
        )
        connection.execute(
            text(
                "INSERT INTO sessions (id, workspace_id, user_id, token_hash, "
                "expires_at, last_seen_at) "
                "VALUES (:id, :workspace_id, :user_id, :token_hash, :expires_at, :last_seen_at)"
            ),
            {
                "id": uuid4(),
                "workspace_id": workspace_id,
                "user_id": user_id,
                "token_hash": sha256(token.encode()).hexdigest(),
                "expires_at": now + timedelta(hours=1),
                "last_seen_at": now,
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
                "recommendations", "recommendation_feedback", "tasks", "commitments",
                "risks", "sessions", "users", "workspaces",
            ):
                connection.execute(
                    text(f"DELETE FROM {table} WHERE id = :workspace_id")
                    if table == "workspaces"
                    else text(f"DELETE FROM {table} WHERE workspace_id = :workspace_id"),
                    {"workspace_id": workspace_id},
                )


def _insert_recommendation(workspace_id: UUID, user_id: UUID, status: str, now: datetime) -> None:
    # Matches backend/migrations/versions/0009_phase1_recommendations.py's
    # real NOT-NULL columns exactly (confirmed against that migration, not
    # guessed): recommendation_type, target_type, proposed_action (jsonb),
    # rationale, confidence, source, created_by, updated_by are all
    # required with no server default; version/pinned/evidence_ids all
    # have server defaults and are omitted here.
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO recommendations (
                    id, workspace_id, recommendation_type, target_type, proposed_action,
                    rationale, confidence, status, source, created_by, updated_by,
                    created_at, updated_at
                ) VALUES (
                    :id, :workspace_id, 'test_type', 'task', '{}'::jsonb,
                    'test rationale', 0.9, :status, 'rule', :user_id, :user_id,
                    :now, :now
                )
                """
            ),
            {
                "id": uuid4(),
                "workspace_id": workspace_id,
                "status": status,
                "user_id": user_id,
                "now": now,
            },
        )


def test_recommendations_count_only_counts_pending_statuses(
    recommendation_count_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, user_id, _token = recommendation_count_context
    now = datetime.now(UTC)
    _insert_recommendation(workspace_id, user_id, "proposed", now)
    _insert_recommendation(workspace_id, user_id, "pending_confirmation", now)
    _insert_recommendation(workspace_id, user_id, "accepted", now)

    counted = client.get("/api/v1/recommendations/count")
    assert counted.status_code == 200
    assert counted.json() == {"count": 2}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_recommendations_count_postgres.py -v`
Expected: FAIL — `404 Not Found` on `GET /api/v1/recommendations/count` (or a DB error from the direct INSERT if the schema guess above is wrong; fix the INSERT to match the real schema first if so, then confirm the failure is genuinely the missing route).

- [ ] **Step 3: Write minimal implementation**

In `backend/ecc/domains/governance/recommendation_models.py`, add near the top-level model definitions (alongside `RecommendationListResponse`):

```python
class RecommendationCount(BaseModel):
    count: int
```

In `backend/ecc/domains/governance/recommendation_queries.py`:
1. Add `RecommendationCount` to the existing `from ecc.domains.governance.recommendation_models import (...)` import block.
2. Insert this immediately after `list_recommendations` and before `@router.get("/{recommendation_id}", ...)`:

```python
@router.get("/count", response_model=RecommendationCount)
def count_recommendations(auth: AuthDep, session: SessionDep) -> RecommendationCount:
    visibility_sql, visibility_params = authz.visible_resource_filter_sql(
        session, auth, resource_type="recommendations", action="read", table_alias="recommendations"
    )
    count = session.execute(
        text(f"""
            SELECT COUNT(*) FROM recommendations
            WHERE workspace_id = :workspace_id
              AND ({visibility_sql})
              AND archived_at IS NULL
              AND status = ANY(CAST(:statuses AS text[]))
        """),
        {
            "workspace_id": auth.workspace_id,
            "statuses": ["proposed", "pending_confirmation"],
            **visibility_params,
        },
    ).scalar_one()
    session.rollback()
    return RecommendationCount(count=count)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_recommendations_count_postgres.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/ecc/domains/governance/recommendation_models.py backend/ecc/domains/governance/recommendation_queries.py tests/test_recommendations_count_postgres.py
git commit -m "feat(governance): add GET /api/v1/recommendations/count for the nav sidebar badge"
```

---

### Task 5: Full verification and API reference update

**Files:**
- Read: `docs/API-SCHEMAS.md` or equivalent API reference doc, if one documents every endpoint in this repo (check for a file matching that name at the repo root or under `docs/`) — add the 4 new endpoints there following its existing per-endpoint format. If no such file exists, skip this without inventing one.

- [ ] **Step 1: Run the full backend test suite**

Run: `uv run pytest`
Expected: PASS — all 4 new test files plus every pre-existing test, no regressions.

- [ ] **Step 2: Run lint and type checks**

Run: `uv run ruff check backend tests && uv run ruff format --check backend tests && uv run mypy backend`
Expected: all clean. Fix any issue these surface (e.g. missing type narrowing on the new `count: int` returns) before moving on.

- [ ] **Step 3: Document the 4 new endpoints, if this repo has a living API reference**

Run: `grep -rl "GET /api/v1/attention\b" docs/ 2>/dev/null` to check whether an existing doc enumerates this endpoint already. If yes, add the 4 new `.../count` routes there in the same format/section as their list siblings. If no such file exists, skip this step — do not create a new API reference doc speculatively.

- [ ] **Step 4: Commit any doc update**

```bash
git add -A
git commit -m "docs: document the 4 new nav sidebar badge count endpoints"
```

Skip this commit entirely if Step 3 found nothing to update.

- [ ] **Step 5: Push the branch**

```bash
git push -u origin feat/nav-badge-count-endpoints
```

This branch merges independently of the frontend navigation-redesign work — open its own PR once pushed, don't wait on the frontend branch.
