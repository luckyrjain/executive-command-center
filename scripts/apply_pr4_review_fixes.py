from pathlib import Path


def replace(path: str, old: str, new: str, count: int = 1) -> None:
    file = Path(path)
    content = file.read_text()
    if old not in content:
        raise SystemExit(f"target not found in {path}: {old[:80]!r}")
    file.write_text(content.replace(old, new, count))


replace(
    "backend/migrations/versions/0002_phase1_task_foundation.py",
    "def upgrade() -> None:\n    uuid = postgresql.UUID(as_uuid=True)\n\n",
    "def upgrade() -> None:\n    uuid = postgresql.UUID(as_uuid=True)\n\n"
    "    op.add_column(\n"
    "        \"workspaces\",\n"
    "        sa.Column(\n"
    "            \"timezone\",\n"
    "            sa.String(64),\n"
    "            nullable=False,\n"
    "            server_default=\"UTC\",\n"
    "        ),\n"
    "    )\n\n",
)
replace(
    "backend/migrations/versions/0002_phase1_task_foundation.py",
    "    op.drop_table(\"tasks\")\n",
    "    op.drop_table(\"tasks\")\n    op.drop_column(\"workspaces\", \"timezone\")\n",
)

replace(
    "backend/ecc/auth.py",
    "class AuthContext:\n    workspace_id: UUID\n    user_id: UUID\n",
    "class AuthContext:\n    workspace_id: UUID\n    user_id: UUID\n    timezone: str\n",
)
replace(
    "backend/ecc/auth.py",
    "            SELECT workspace_id, user_id\n            FROM sessions\n",
    "            SELECT s.workspace_id, s.user_id, w.timezone\n"
    "            FROM sessions AS s\n"
    "            JOIN workspaces AS w ON w.id = s.workspace_id\n",
)
replace(
    "backend/ecc/auth.py",
    "            WHERE token_hash = :token_hash\n              AND revoked_at IS NULL\n              AND expires_at > :now\n",
    "            WHERE s.token_hash = :token_hash\n"
    "              AND s.revoked_at IS NULL\n"
    "              AND s.expires_at > :now\n",
)
replace(
    "backend/ecc/auth.py",
    "    return AuthContext(workspace_id=row[\"workspace_id\"], user_id=row[\"user_id\"])\n",
    "    return AuthContext(\n"
    "        workspace_id=row[\"workspace_id\"],\n"
    "        user_id=row[\"user_id\"],\n"
    "        timezone=row[\"timezone\"],\n"
    "    )\n",
)

tasks = Path("backend/ecc/domains/planning/tasks.py")
content = tasks.read_text()
old_validator = (
    "        if self.due_date is not None and self.due_at is not None:\n"
    "            raise ValueError(\"due_date and due_at are mutually exclusive\")\n"
    "        return self\n"
)
new_validator = (
    "        if self.due_date is not None and self.due_at is not None:\n"
    "            raise ValueError(\"due_date and due_at are mutually exclusive\")\n"
    "        if self.due_at is not None and self.due_at.utcoffset() is None:\n"
    "            raise ValueError(\"due_at must include a timezone offset\")\n"
    "        return self\n"
)
if content.count(old_validator) < 2:
    raise SystemExit("task validators not found")
content = content.replace(old_validator, new_validator, 2)

marker = "def _load_idempotent_response(\n"
helper = (
    "def _lock_idempotency_key(\n"
    "    session: Session, auth: AuthContext, key: str\n"
    ") -> None:\n"
    "    lock_key = f\"{auth.workspace_id}:{auth.user_id}:{key}\"\n"
    "    session.execute(\n"
    "        text(\"SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))\"),\n"
    "        {\"lock_key\": lock_key},\n"
    "    )\n\n\n"
)
if marker not in content:
    raise SystemExit("idempotency insertion point not found")
content = content.replace(marker, helper + marker, 1)

transaction_marker = "    with session.begin():\n        cached = _load_idempotent_response(\n"
if content.count(transaction_marker) != 3:
    raise SystemExit("unexpected mutation transaction count")
content = content.replace(
    transaction_marker,
    "    with session.begin():\n"
    "        _lock_idempotency_key(session, auth, idempotency_key)\n"
    "        cached = _load_idempotent_response(\n",
    3,
)

content = content.replace(
    "        clauses.append(\"COALESCE(due_date, due_at::date) <= :due_before\")\n",
    "        clauses.append(\n"
    "            \"COALESCE(due_date, (due_at AT TIME ZONE :workspace_timezone)::date) \"\n"
    "            \"<= :due_before\"\n"
    "        )\n"
    "        params[\"workspace_timezone\"] = auth.timezone\n",
    1,
)
content = content.replace(
    "        clauses.append(\"COALESCE(due_date, due_at::date) >= :due_after\")\n",
    "        clauses.append(\n"
    "            \"COALESCE(due_date, (due_at AT TIME ZONE :workspace_timezone)::date) \"\n"
    "            \">= :due_after\"\n"
    "        )\n"
    "        params[\"workspace_timezone\"] = auth.timezone\n",
    1,
)

update_marker = (
    "        if current[\"archived_at\"] is not None:\n"
    "            raise HTTPException(status_code=409, detail=\"TASK_ARCHIVED\")\n\n"
    "        fields = payload.model_fields_set - {\"expected_version\"}\n"
)
update_replacement = (
    "        if current[\"archived_at\"] is not None:\n"
    "            raise HTTPException(status_code=409, detail=\"TASK_ARCHIVED\")\n"
    "        if (\n"
    "            current[\"status\"] in {\"completed\", \"cancelled\"}\n"
    "            and \"status\" in payload.model_fields_set\n"
    "            and payload.status != current[\"status\"]\n"
    "        ):\n"
    "            raise HTTPException(status_code=409, detail=\"TASK_TERMINAL\")\n\n"
    "        fields = payload.model_fields_set - {\"expected_version\"}\n"
)
if update_marker not in content:
    raise SystemExit("terminal update insertion point not found")
content = content.replace(update_marker, update_replacement, 1)

lifecycle_marker = (
    "            return response\n\n"
    "        if action in {\"complete\", \"cancel\"} and current[\"archived_at\"] is not None:\n"
)
lifecycle_replacement = (
    "            return response\n\n"
    "        if (\n"
    "            action in {\"complete\", \"cancel\"}\n"
    "            and current[\"status\"] in {\"completed\", \"cancelled\"}\n"
    "        ):\n"
    "            raise HTTPException(status_code=409, detail=\"TASK_TERMINAL\")\n\n"
    "        if action in {\"complete\", \"cancel\"} and current[\"archived_at\"] is not None:\n"
)
if lifecycle_marker not in content:
    raise SystemExit("terminal lifecycle insertion point not found")
content = content.replace(lifecycle_marker, lifecycle_replacement, 1)
tasks.write_text(content)

main = Path("backend/ecc/main.py")
content = main.read_text()
content = content.replace(
    "from collections.abc import Awaitable, Callable\nfrom uuid import uuid4\n",
    "from collections.abc import Awaitable, Callable\n"
    "from json import JSONDecodeError, loads\n"
    "from uuid import UUID, uuid4\n",
    1,
)
old_middleware = '''@app.middleware("http")
async def correlation_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    correlation_id = request.headers.get("X-Correlation-ID", str(uuid4()))
    request.state.correlation_id = correlation_id
    response = await call_next(request)
    response.headers["X-Correlation-ID"] = correlation_id
    return response
'''
new_middleware = '''def _request_uuid(raw: str | None) -> str:
    try:
        return str(UUID(raw)) if raw else str(uuid4())
    except ValueError:
        return str(uuid4())


def _error_payload(detail: object, request_id: str, correlation_id: str) -> dict[str, object]:
    if isinstance(detail, str):
        code = detail
        message = detail.replace("_", " ").title()
        details: object = {}
    elif isinstance(detail, dict):
        code = str(detail.get("code", "REQUEST_FAILED"))
        message = str(detail.get("message", code.replace("_", " ").title()))
        details = {
            key: value
            for key, value in detail.items()
            if key not in {"code", "message"}
        }
    else:
        code = "VALIDATION_ERROR"
        message = "Request validation failed"
        details = {"violations": detail}
    return {
        "error": {
            "code": code,
            "message": message,
            "request_id": request_id,
            "details": details,
        },
        "correlation_id": correlation_id,
    }


@app.middleware("http")
async def response_contract_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    request_id = str(uuid4())
    correlation_id = _request_uuid(request.headers.get("X-Correlation-ID"))
    request.state.request_id = request_id
    request.state.correlation_id = correlation_id
    response = await call_next(request)
    response.headers["X-Correlation-ID"] = correlation_id
    response.headers["X-Request-ID"] = request_id

    if not request.url.path.startswith("/api/v1"):
        return response
    if "application/json" not in response.headers.get("content-type", ""):
        return response

    body_iterator = getattr(response, "body_iterator", None)
    if body_iterator is None:
        return response
    body = b"".join([chunk async for chunk in body_iterator])
    try:
        payload = loads(body)
    except (JSONDecodeError, UnicodeDecodeError):
        return Response(
            content=body,
            status_code=response.status_code,
            headers=dict(response.headers),
            media_type=response.media_type,
        )

    if response.status_code >= 400:
        detail = payload.get("detail") if isinstance(payload, dict) else payload
        payload = _error_payload(detail, request_id, correlation_id)
    elif isinstance(payload, dict):
        payload["request_id"] = request_id
        payload["correlation_id"] = correlation_id

    headers = {
        key: value
        for key, value in response.headers.items()
        if key.lower() not in {"content-length", "content-type"}
    }
    return JSONResponse(
        content=payload,
        status_code=response.status_code,
        headers=headers,
        background=response.background,
    )
'''
if old_middleware not in content:
    raise SystemExit("response middleware block not found")
main.write_text(content.replace(old_middleware, new_middleware, 1))

contract = Path("tests/test_task_contract.py")
content = contract.read_text()
if "test_task_create_rejects_timezone_naive_due_at" not in content:
    content += '''


def test_task_create_rejects_timezone_naive_due_at() -> None:
    with pytest.raises(ValidationError):
        TaskCreate(
            title="Timezone required",
            due_at=datetime(2026, 7, 14, 9, 0),
        )


def test_task_patch_rejects_timezone_naive_due_at() -> None:
    with pytest.raises(ValidationError):
        TaskPatch(
            expected_version=1,
            due_at=datetime(2026, 7, 14, 9, 0),
        )
'''
contract.write_text(content)

postgres = Path("tests/test_task_postgres.py")
content = postgres.read_text()
content = content.replace(
    "from collections.abc import Iterator\n",
    "from collections.abc import Iterator\nfrom concurrent.futures import ThreadPoolExecutor\n",
    1,
)
content = content.replace(
    "    created = create.json()\n",
    "    created = create.json()\n"
    "    assert created[\"request_id\"]\n"
    "    assert created[\"correlation_id\"]\n",
    1,
)
content = content.replace(
    "    assert conflict.status_code == 409\n",
    "    assert conflict.status_code == 409\n"
    "    assert conflict.json()[\"error\"][\"code\"] == \"VERSION_CONFLICT\"\n"
    "    assert conflict.json()[\"error\"][\"request_id\"]\n",
    1,
)
if "test_terminal_transitions_and_workspace_timezone_are_enforced" not in content:
    content += '''


def test_terminal_transitions_and_workspace_timezone_are_enforced(
    task_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, _user_id, token = task_test_context
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE workspaces SET timezone = 'Asia/Kolkata' WHERE id = :workspace_id"),
            {"workspace_id": workspace_id},
        )

    created = client.post(
        "/api/v1/tasks",
        headers=_headers(token, "timezone-task"),
        json={
            "title": "Local next-day task",
            "due_at": "2026-07-14T20:00:00Z",
        },
    )
    assert created.status_code == 201
    task_id = created.json()["id"]

    before_local_day = client.get("/api/v1/tasks?due_before=2026-07-14")
    assert before_local_day.status_code == 200
    assert task_id not in {item["id"] for item in before_local_day.json()["items"]}

    on_local_day = client.get("/api/v1/tasks?due_after=2026-07-15")
    assert on_local_day.status_code == 200
    assert task_id in {item["id"] for item in on_local_day.json()["items"]}

    complete = client.post(
        f"/api/v1/tasks/{task_id}/complete",
        headers=_headers(token, "terminal-complete"),
        json={"expected_version": 1},
    )
    assert complete.status_code == 200

    reopen = client.patch(
        f"/api/v1/tasks/{task_id}",
        headers=_headers(token, "terminal-reopen"),
        json={"expected_version": 2, "status": "in_progress"},
    )
    assert reopen.status_code == 409
    assert reopen.json()["error"]["code"] == "TASK_TERMINAL"

    cancel = client.post(
        f"/api/v1/tasks/{task_id}/cancel",
        headers=_headers(token, "terminal-cancel"),
        json={"expected_version": 2},
    )
    assert cancel.status_code == 409
    assert cancel.json()["error"]["code"] == "TASK_TERMINAL"


def test_concurrent_idempotent_create_returns_one_task(
    task_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    _client, workspace_id, _user_id, token = task_test_context

    def create_once() -> tuple[int, str]:
        worker = TestClient(app)
        worker.cookies.set("ecc_session", token)
        try:
            response = worker.post(
                "/api/v1/tasks",
                headers=_headers(token, "concurrent-create"),
                json={"title": "Concurrent idempotent task"},
            )
            return response.status_code, response.json()["id"]
        finally:
            worker.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: create_once(), range(2)))

    assert [status for status, _task_id in results] == [201, 201]
    assert len({task_id for _status, task_id in results}) == 1
    with engine.connect() as connection:
        count = connection.execute(
            text(
                "SELECT count(*) FROM tasks "
                "WHERE workspace_id = :workspace_id AND title = :title"
            ),
            {"workspace_id": workspace_id, "title": "Concurrent idempotent task"},
        ).scalar_one()
    assert count == 1
'''
postgres.write_text(content)
