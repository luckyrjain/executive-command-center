from pathlib import Path


tasks = Path("backend/ecc/domains/planning/tasks.py")
content = tasks.read_text()
old = '''def _request_ids(request: Request) -> tuple[UUID, UUID]:
    request_id = uuid4()
    raw = request.headers.get("X-Correlation-ID")
    try:
        correlation_id = UUID(raw) if raw else uuid4()
    except ValueError:
        correlation_id = uuid4()
    return request_id, correlation_id
'''
new = '''def _request_ids(request: Request) -> tuple[UUID, UUID]:
    request_id = getattr(request.state, "request_id", None)
    correlation_id = getattr(request.state, "correlation_id", None)
    try:
        return UUID(request_id), UUID(correlation_id)
    except (TypeError, ValueError):
        return uuid4(), uuid4()
'''
if old not in content:
    raise SystemExit("request-id helper not found")
tasks.write_text(content.replace(old, new, 1))

postgres = Path("tests/test_task_postgres.py")
content = postgres.read_text()
old_query = '''                SELECT event_type
                FROM audit_events
                WHERE workspace_id = :workspace_id
                  AND aggregate_id = :task_id
                ORDER BY occurred_at
'''
new_query = '''                SELECT event_type
                FROM audit_events
                WHERE workspace_id = :workspace_id
                  AND aggregate_id = :task_id
                ORDER BY occurred_at
'''
if old_query not in content:
    raise SystemExit("audit query not found")
marker = '''    assert "task.restored.v1" in outbox_types
'''
addition = '''    assert "task.restored.v1" in outbox_types

    with engine.connect() as connection:
        trace = connection.execute(
            text(
                "SELECT request_id, correlation_id FROM audit_events "
                "WHERE workspace_id = :workspace_id AND aggregate_id = :task_id "
                "AND event_type = 'task.created'"
            ),
            {"workspace_id": workspace_id, "task_id": task_id},
        ).one()
    assert str(trace.request_id) == created["request_id"]
    assert str(trace.correlation_id) == created["correlation_id"]
'''
if marker not in content:
    raise SystemExit("test insertion point not found")
postgres.write_text(content.replace(marker, addition, 1))
