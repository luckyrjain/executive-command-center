import os
from collections.abc import Iterator
from datetime import UTC, date, datetime, time, timedelta
from hashlib import sha256
from hmac import new
from time import perf_counter
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from fixtures.phase3_planning_scenarios import (
    FULL_CALENDAR_RESERVATION,
    FULL_WEEK_CAPACITY,
    MONDAY,
    NO_CAPACITY,
    OVERDUE_DEADLINE,
    TIED_CANDIDATES,
    TIMEZONE,
    candidate,
    monday_local,
    reserved,
)
from sqlalchemy import event, text

from ecc.config import get_settings
from ecc.database import engine
from ecc.domains.attention.planning import (
    DEFAULT_EFFORT_MINUTES,
    CandidateItemInput,
    CapacityDayInput,
    propose_plan,
)
from ecc.main import app

settings = get_settings()
pytestmark = pytest.mark.skipif(
    not settings.database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)


# ---------------------------------------------------------------------------
# Pure propose_plan scenario tests -- PLANNING-CONTRACT.md's Evaluation
# section names these explicitly: full calendars, no capacity, timezone/DST,
# overdue work, equal scores, missing estimates, fixed meetings, plus
# constraint conflicts. No database needed since propose_plan is pure.
# ---------------------------------------------------------------------------


def test_full_calendar_leaves_candidate_unscheduled() -> None:
    proposal = propose_plan(
        period_start=MONDAY,
        period_end=MONDAY,
        timezone=TIMEZONE,
        capacity_days=FULL_WEEK_CAPACITY,
        reserved_blocks=[FULL_CALENDAR_RESERVATION],
        deadline_constraints=[],
        candidates=[candidate("Write report", score=80)],
    )
    assert proposal.blocks == []
    assert len(proposal.unscheduled) == 1
    assert proposal.unscheduled[0].reason == "no_capacity"
    assert any(c.code == "capacity_exceeded" for c in proposal.conflicts)


def test_no_capacity_produces_all_unscheduled_and_zero_capacity_minutes() -> None:
    proposal = propose_plan(
        period_start=MONDAY,
        period_end=MONDAY,
        timezone=TIMEZONE,
        capacity_days=NO_CAPACITY,
        reserved_blocks=[],
        deadline_constraints=[],
        candidates=[candidate("A", score=90), candidate("B", score=10)],
    )
    assert proposal.capacity_minutes == 0
    assert proposal.blocks == []
    assert {u.label for u in proposal.unscheduled} == {"A", "B"}


def test_timezone_dst_spring_forward_boundary_stays_correct() -> None:
    # 2026-03-08 is a US spring-forward DST transition (America/New_York
    # loses an hour at 2am local). A day window anchored at local 09:00 for
    # 480 minutes must still resolve to the correct UTC instants either
    # side of the transition -- naive fixed-offset arithmetic would drift.
    dst_day = date(2026, 3, 8)
    zone = ZoneInfo("America/New_York")
    proposal = propose_plan(
        period_start=dst_day,
        period_end=dst_day,
        timezone="America/New_York",
        capacity_days=[CapacityDayInput(weekday=dst_day.weekday(), available_minutes=480)],
        reserved_blocks=[],
        deadline_constraints=[],
        candidates=[candidate("Post-DST task", score=50)],
    )
    assert len(proposal.blocks) == 1
    block = proposal.blocks[0]
    expected_start = datetime.combine(dst_day, time(9, 0), zone)
    assert block.starts_at == expected_start
    # March 8 2026 is the US spring-forward date itself (2am -> 3am); 9am
    # local that same day is already past the transition, so EDT (-4) is
    # correct here, not EST (-5) -- the point of this test is that the
    # *local* 09:00 anchor and duration survive the transition correctly
    # regardless of which side of it the day falls on.
    assert block.starts_at.utcoffset() == timedelta(hours=-4)
    assert (block.ends_at - block.starts_at) == timedelta(minutes=DEFAULT_EFFORT_MINUTES)


def test_overdue_deadline_produces_missed_deadline_conflict() -> None:
    proposal = propose_plan(
        period_start=MONDAY,
        period_end=MONDAY,
        timezone=TIMEZONE,
        capacity_days=FULL_WEEK_CAPACITY,
        reserved_blocks=[],
        deadline_constraints=[OVERDUE_DEADLINE],
        candidates=[],
    )
    assert proposal.blocks == []
    assert proposal.unscheduled[0].reason == "missed_deadline"
    assert any(c.code == "missed_deadline" for c in proposal.conflicts)


def test_equal_scores_tie_break_is_stable_across_runs() -> None:
    first = propose_plan(
        period_start=MONDAY,
        period_end=MONDAY,
        timezone=TIMEZONE,
        capacity_days=FULL_WEEK_CAPACITY,
        reserved_blocks=[],
        deadline_constraints=[],
        candidates=TIED_CANDIDATES,
    )
    second = propose_plan(
        period_start=MONDAY,
        period_end=MONDAY,
        timezone=TIMEZONE,
        capacity_days=FULL_WEEK_CAPACITY,
        reserved_blocks=[],
        deadline_constraints=[],
        candidates=list(reversed(TIED_CANDIDATES)),  # input order must not matter
    )
    first_order = [b.label for b in first.blocks]
    second_order = [b.label for b in second.blocks]
    # The property under test: with tied scores, placement order depends
    # only on entity_id (the stable final tie-breaker), never on input
    # order -- reversing the input candidate list must not change the
    # resulting block order.
    assert first_order == second_order
    assert {b.source_id for b in first.blocks} == {c.entity_id for c in TIED_CANDIDATES}
    expected_order = [c.label for c in sorted(TIED_CANDIDATES, key=lambda c: str(c.entity_id))]
    assert first_order == expected_order


def test_missing_effort_estimate_uses_default_bucket_with_lower_confidence_flag() -> None:
    proposal = propose_plan(
        period_start=MONDAY,
        period_end=MONDAY,
        timezone=TIMEZONE,
        capacity_days=FULL_WEEK_CAPACITY,
        reserved_blocks=[],
        deadline_constraints=[],
        candidates=[candidate("No estimate", score=50)],
    )
    block = proposal.blocks[0]
    assert block.is_default_effort is True
    assert (block.ends_at - block.starts_at) == timedelta(minutes=DEFAULT_EFFORT_MINUTES)


def test_explicit_effort_estimate_is_honored_and_not_flagged_default() -> None:
    proposal = propose_plan(
        period_start=MONDAY,
        period_end=MONDAY,
        timezone=TIMEZONE,
        capacity_days=FULL_WEEK_CAPACITY,
        reserved_blocks=[],
        deadline_constraints=[],
        candidates=[
            CandidateItemInput(
                entity_type="task",
                entity_id=uuid4(),
                label="Estimated",
                score=50,
                effort_minutes=90,
            )
        ],
    )
    block = proposal.blocks[0]
    assert block.is_default_effort is False
    assert (block.ends_at - block.starts_at) == timedelta(minutes=90)


def test_fixed_meeting_is_reserved_and_candidates_never_overlap_it() -> None:
    meeting = reserved("Standup", monday_local(9), monday_local(9, 30))
    proposal = propose_plan(
        period_start=MONDAY,
        period_end=MONDAY,
        timezone=TIMEZONE,
        capacity_days=FULL_WEEK_CAPACITY,
        reserved_blocks=[meeting],
        deadline_constraints=[],
        candidates=[candidate("Deep work", score=50)],
    )
    assert len(proposal.blocks) == 1
    block = proposal.blocks[0]
    assert block.starts_at >= meeting.ends_at or block.ends_at <= meeting.starts_at


def test_overlapping_hard_reservations_produce_constraint_conflict_but_both_kept() -> None:
    first = reserved("Board call", monday_local(10), monday_local(11))
    second = reserved("Client call", monday_local(10, 30), monday_local(11, 30))
    proposal = propose_plan(
        period_start=MONDAY,
        period_end=MONDAY,
        timezone=TIMEZONE,
        capacity_days=FULL_WEEK_CAPACITY,
        reserved_blocks=[first, second],
        deadline_constraints=[],
        candidates=[],
    )
    assert any(c.code == "constraint_conflict" for c in proposal.conflicts)


_IN_CI = os.getenv("CI") is not None
# PHASE-003-human-attention-engine.md's NFR ("deterministic daily plan p95
# <1 second") gives a 1-second ceiling, but propose_plan is a pure
# in-memory function -- real measurement against this exact dense-weekly
# scenario (7 days, 200 candidates, 20 reservations, 10 deadlines) is
# ~0.95 ms p95 locally. A 1-second budget would carry ~1000x headroom and
# catch nothing; 50 ms/100 ms still leaves 50-100x headroom (generous
# margin for sandbox jitter on a sub-millisecond operation) while actually
# catching an accidental quadratic blowup or a stray DB/network call.
PLAN_BUDGET_SECONDS = 0.1 if _IN_CI else 0.05
PLAN_SAMPLE_SIZE = 10


def test_propose_plan_dense_weekly_p95_under_budget() -> None:
    """Strictly harder than PHASE-003's named "daily" case: a full dense
    week, not one day. Pure function, no DB: this is a genuine,
    repeatable measurement of propose_plan's own cost, not infrastructure
    noise.
    """
    from fixtures.phase3_planning_scenarios import deadline as make_deadline

    period_start = MONDAY
    period_end = MONDAY + timedelta(days=6)
    capacity_days = FULL_WEEK_CAPACITY
    reserved_blocks = [
        reserved(
            f"Meeting {i}",
            monday_local(10) + timedelta(days=i % 7, hours=i % 3),
            monday_local(10) + timedelta(days=i % 7, hours=(i % 3) + 1),
        )
        for i in range(20)
    ]
    deadline_constraints = [
        make_deadline(f"Deadline {i}", monday_local(17) + timedelta(days=i)) for i in range(10)
    ]
    candidates = [candidate(f"Candidate {i}", score=100 - (i % 100)) for i in range(200)]

    samples = []
    for _ in range(PLAN_SAMPLE_SIZE):
        started = perf_counter()
        propose_plan(
            period_start=period_start,
            period_end=period_end,
            timezone=TIMEZONE,
            capacity_days=capacity_days,
            reserved_blocks=reserved_blocks,
            deadline_constraints=deadline_constraints,
            candidates=candidates,
        )
        samples.append(perf_counter() - started)

    ordered = sorted(samples)
    index = min(len(ordered) - 1, -(-(95 * len(ordered)) // 100) - 1)
    p95 = ordered[index]
    assert p95 < PLAN_BUDGET_SECONDS, (
        f"propose_plan p95 {p95 * 1000:.1f} ms exceeded {PLAN_BUDGET_SECONDS * 1000:.0f} ms "
        f"budget (in_ci={_IN_CI}); samples(ms)={[round(s * 1000, 1) for s in samples]}"
    )


# ---------------------------------------------------------------------------
# HTTP integration tests for POST|GET /plans
# ---------------------------------------------------------------------------


@pytest.fixture
def planning_test_context() -> Iterator[tuple[TestClient, UUID, UUID, str]]:
    workspace_id = uuid4()
    user_id = uuid4()
    token = f"session-{uuid4()}"
    now = datetime.now(UTC)

    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, 'Planning Test', 'Asia/Kolkata', :created_at)"
            ),
            {"id": workspace_id, "created_at": now},
        )
        connection.execute(
            text(
                "INSERT INTO users (id, workspace_id, email, password_hash, created_at) "
                "VALUES (:id, :workspace_id, :email, 'test-password-hash', :created_at)"
            ),
            {
                "id": user_id,
                "workspace_id": workspace_id,
                "email": f"{user_id}@example.test",
                "created_at": now,
            },
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
                "plan_blocks",
                "plans",
                "attention_items",
                "event_outbox",
                "audit_events",
                "idempotency_records",
                "capacity_profiles",
                "planning_constraints",
                "tasks",
                "sessions",
                "users",
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


def _seed_task(workspace_id: UUID, owner_id: UUID, title: str, *, priority: str = "high") -> UUID:
    task_id = uuid4()
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO tasks (
                    id, workspace_id, owner_id, title, status, manual_priority,
                    pinned, source_type, created_by, updated_by, created_at, updated_at, version
                ) VALUES (
                    :id, :workspace_id, :owner_id, :title, 'planned', :priority,
                    false, 'local', :owner_id, :owner_id, :now, :now, 1
                )
                """
            ),
            {
                "id": task_id,
                "workspace_id": workspace_id,
                "owner_id": owner_id,
                "title": title,
                "priority": priority,
                "now": now,
            },
        )
    return task_id


def _set_full_week_capacity(client: TestClient, token: str) -> None:
    days = [{"weekday": w, "available_minutes": 480, "focus_minutes": 240} for w in range(7)]
    response = client.put(
        "/api/v1/planning/capacity",
        headers=_headers(token, "set-full-week-capacity"),
        json={"expected_version": 0, "timezone": "Asia/Kolkata", "days": days},
    )
    assert response.status_code == 200, response.text


def _next_period() -> tuple[date, date]:
    start = date.today() + timedelta(days=1)
    return start, start


def test_create_plan_persists_blocks_and_is_retrievable(
    planning_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, user_id, token = planning_test_context
    _set_full_week_capacity(client, token)
    _seed_task(workspace_id, user_id, "Write the board memo")

    regenerate = client.post("/api/v1/attention/regenerate", headers=_headers(token), json={})
    assert regenerate.status_code == 200

    period_start, period_end = _next_period()
    created = client.post(
        "/api/v1/plans",
        headers=_headers(token, "create-plan"),
        json={"period_start": period_start.isoformat(), "period_end": period_end.isoformat()},
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["status"] == "proposed"
    assert body["capacity_minutes"] == 480
    assert len(body["blocks"]) == 1
    assert body["blocks"][0]["is_default_effort"] is True

    fetched = client.get(f"/api/v1/plans/{body['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["id"] == body["id"]
    assert len(fetched.json()["blocks"]) == 1


def test_create_plan_rejects_period_over_max_days(
    planning_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _, _, token = planning_test_context
    start = date.today() + timedelta(days=1)
    response = client.post(
        "/api/v1/plans",
        headers=_headers(token, "create-plan-too-long"),
        json={
            "period_start": start.isoformat(),
            "period_end": (start + timedelta(days=10)).isoformat(),
        },
    )
    assert response.status_code == 422


def test_create_plan_idempotent_on_replay(
    planning_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _, _, token = planning_test_context
    _set_full_week_capacity(client, token)
    period_start, period_end = _next_period()
    headers = _headers(token, "idempotent-plan")
    payload = {"period_start": period_start.isoformat(), "period_end": period_end.isoformat()}

    first = client.post("/api/v1/plans", headers=headers, json=payload)
    assert first.status_code == 201
    replay = client.post("/api/v1/plans", headers=headers, json=payload)
    assert replay.status_code == 201
    assert replay.json()["id"] == first.json()["id"]


def test_create_plan_conflicting_replay_returns_409_and_records_metric(
    planning_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    """Reusing an Idempotency-Key with a materially different payload must
    409 IDEMPOTENCY_CONFLICT, and must record the same
    ``record_idempotency_conflict`` observability signal every other
    idempotency-replay path in the codebase emits on this same conflict.
    """
    from ecc.observability import render_metrics

    client, _, _, token = planning_test_context
    _set_full_week_capacity(client, token)
    period_start, period_end = _next_period()
    headers = _headers(token, "conflicting-plan")

    first = client.post(
        "/api/v1/plans",
        headers=headers,
        json={"period_start": period_start.isoformat(), "period_end": period_end.isoformat()},
    )
    assert first.status_code == 201, first.text

    conflicting = client.post(
        "/api/v1/plans",
        headers=headers,
        json={
            "period_start": period_start.isoformat(),
            "period_end": (period_end + timedelta(days=1)).isoformat(),
        },
    )
    assert conflicting.status_code == 409
    assert conflicting.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert 'ecc_idempotency_conflicts_total{domain="planning"}' in render_metrics()


def test_stale_attention_items_excluded_from_plan_candidates(
    planning_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    """Step 1's "validate source freshness": an attention_items row whose
    expires_at has already passed must never surface as a plan candidate,
    the same way it's already excluded from GET /attention.
    """
    client, workspace_id, user_id, token = planning_test_context
    _set_full_week_capacity(client, token)
    task_id = _seed_task(workspace_id, user_id, "Stale candidate")
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO attention_items (
                    id, workspace_id, entity_type, entity_id, source_entity_version,
                    score, confidence, factors, explanation, generated_at, expires_at,
                    pinned, policy_version
                ) VALUES (
                    :id, :workspace_id, 'task', :entity_id, 1,
                    90, 1.0, '[]'::jsonb, 'stale', :generated_at, :expires_at, false, 1
                )
                """
            ),
            {
                "id": uuid4(),
                "workspace_id": workspace_id,
                "entity_id": task_id,
                "generated_at": now - timedelta(hours=2),
                "expires_at": now - timedelta(hours=1),
            },
        )

    period_start, period_end = _next_period()
    created = client.post(
        "/api/v1/plans",
        headers=_headers(token, "create-plan-stale"),
        json={"period_start": period_start.isoformat(), "period_end": period_end.isoformat()},
    )
    assert created.status_code == 201
    assert created.json()["blocks"] == []


def test_list_plans_signed_cursor_pagination_and_tamper_rejection(
    planning_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _, _, token = planning_test_context
    _set_full_week_capacity(client, token)
    base = date.today() + timedelta(days=1)
    for i in range(3):
        response = client.post(
            "/api/v1/plans",
            headers=_headers(token, f"paginate-plan-{i}"),
            json={
                "period_start": (base + timedelta(days=i)).isoformat(),
                "period_end": (base + timedelta(days=i)).isoformat(),
            },
        )
        assert response.status_code == 201

    first_page = client.get("/api/v1/plans", params={"limit": 2})
    assert first_page.status_code == 200
    body = first_page.json()
    assert len(body["items"]) == 2
    assert body["next_cursor"] is not None

    second_page = client.get("/api/v1/plans", params={"limit": 2, "cursor": body["next_cursor"]})
    assert len(second_page.json()["items"]) == 1

    tampered = client.get("/api/v1/plans", params={"cursor": body["next_cursor"][:-1] + "x"})
    assert tampered.status_code == 400
    assert tampered.json()["error"]["code"] == "CURSOR_INVALID"


def test_list_plans_batches_block_fetch_across_page(
    planning_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    """list_plans must not fire one plan_blocks query per plan in the page
    (an N+1) -- fetching blocks for a whole page of plans must be O(1)
    queries, not O(page size). Seeds ``plan_count`` plans each with their own
    block and asserts both correctness (each plan's response carries exactly
    its own seeded block, never another plan's) and that the total query
    count for the list call stays bounded well under plan_count.
    """
    client, workspace_id, user_id, token = planning_test_context
    base = date.today() + timedelta(days=1)
    now = datetime.now(UTC)
    plan_count = 12
    expected_blocks: dict[UUID, list[UUID]] = {}

    with engine.begin() as connection:
        for i in range(plan_count):
            plan_id = uuid4()
            period = base + timedelta(days=i)
            connection.execute(
                text(
                    """
                    INSERT INTO plans (
                        id, workspace_id, user_id, period_start, period_end, status,
                        policy_version, capacity_minutes, source_versions, conflicts,
                        unscheduled, created_by, updated_by, created_at, updated_at, version
                    ) VALUES (
                        :id, :workspace_id, :user_id, :period, :period, 'proposed',
                        1, 0, '{}'::jsonb, '[]'::jsonb, '[]'::jsonb,
                        :user_id, :user_id, :created_at, :created_at, 1
                    )
                    """
                ),
                {
                    "id": plan_id,
                    "workspace_id": workspace_id,
                    "user_id": user_id,
                    "period": period,
                    "created_at": now - timedelta(seconds=plan_count - i),
                },
            )
            block_id = uuid4()
            connection.execute(
                text(
                    """
                    INSERT INTO plan_blocks (
                        id, workspace_id, plan_id, source_type, source_id, starts_at, ends_at,
                        status, rationale, is_default_effort, created_at, updated_at
                    ) VALUES (
                        :id, :workspace_id, :plan_id, 'task', NULL, :starts_at, :ends_at,
                        'proposed', :rationale, false, :now, :now
                    )
                    """
                ),
                {
                    "id": block_id,
                    "workspace_id": workspace_id,
                    "plan_id": plan_id,
                    "starts_at": datetime.combine(period, time(9, 0), UTC),
                    "ends_at": datetime.combine(period, time(9, 30), UTC),
                    "rationale": f"block for plan {i}",
                    "now": now,
                },
            )
            expected_blocks[plan_id] = [block_id]

    query_count = 0

    def _count(*args: object, **kwargs: object) -> None:
        nonlocal query_count
        query_count += 1

    event.listen(engine, "before_cursor_execute", _count)
    try:
        response = client.get("/api/v1/plans", params={"limit": plan_count})
    finally:
        event.remove(engine, "before_cursor_execute", _count)

    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["items"]) == plan_count

    for item in body["items"]:
        plan_id = UUID(item["id"])
        assert [UUID(b["id"]) for b in item["blocks"]] == expected_blocks[plan_id]

    # Regardless of how many plans are in the page, block-fetching must stay
    # O(1): one query for the page of plans, one batched query for all their
    # blocks (plus a small constant for auth/session lookups) -- never one
    # query per plan, which is what would make this scale with plan_count.
    assert query_count < plan_count, (
        f"expected O(1) queries for list_plans, got {query_count} for {plan_count} plans"
    )


def test_plan_is_hidden_across_workspaces(
    planning_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    """A different, real workspace's session must not be able to read a
    plan that belongs to the fixture workspace -- not just a bare
    ``uuid4()`` 404 probe against the fixture's own client, which proves
    nothing about workspace scoping.
    """
    client, workspace_id, user_id, token = planning_test_context
    plan = _create_plan_with_one_block(client, workspace_id, user_id, token)

    other_workspace_id = uuid4()
    other_user_id = uuid4()
    other_token = f"session-{uuid4()}"
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, 'Other Workspace', 'UTC', :now)"
            ),
            {"id": other_workspace_id, "now": now},
        )
        connection.execute(
            text(
                "INSERT INTO users (id, workspace_id, email, password_hash, created_at) "
                "VALUES (:id, :workspace_id, :email, 'hash', :now)"
            ),
            {
                "id": other_user_id,
                "workspace_id": other_workspace_id,
                "email": f"{other_user_id}@example.test",
                "now": now,
            },
        )
        connection.execute(
            text(
                "INSERT INTO sessions (id, workspace_id, user_id, token_hash, "
                "expires_at, last_seen_at) "
                "VALUES (:id, :workspace_id, :user_id, :token_hash, :expires_at, :now)"
            ),
            {
                "id": uuid4(),
                "workspace_id": other_workspace_id,
                "user_id": other_user_id,
                "token_hash": sha256(other_token.encode()).hexdigest(),
                "expires_at": now + timedelta(hours=1),
                "now": now,
            },
        )
    other_client = TestClient(app)
    other_client.cookies.set("ecc_session", other_token)
    try:
        response = other_client.get(f"/api/v1/plans/{plan['id']}")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "PLAN_NOT_FOUND"
    finally:
        other_client.close()
        with engine.begin() as connection:
            for table in ("sessions", "users"):
                connection.execute(
                    text(f"DELETE FROM {table} WHERE workspace_id = :workspace_id"),  # noqa: S608
                    {"workspace_id": other_workspace_id},
                )
            connection.execute(
                text("DELETE FROM workspaces WHERE id = :workspace_id"),
                {"workspace_id": other_workspace_id},
            )


def _add_second_user_in_same_workspace(workspace_id: UUID) -> tuple[UUID, str]:
    """A second, real, logged-in user in the *same* workspace as the
    fixture's primary user -- not a different workspace, and not a bare
    ``uuid4()`` 404 probe. Used by the IDOR regression tests below (finding
    #1): plans are per-(workspace, user) everywhere else, so another user
    in the same workspace must be unable to read or mutate a plan that
    isn't theirs, exactly as if it belonged to a different workspace.
    """
    other_user_id = uuid4()
    other_token = f"session-{uuid4()}"
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users (id, workspace_id, email, password_hash, created_at) "
                "VALUES (:id, :workspace_id, :email, 'test-password-hash', :created_at)"
            ),
            {
                "id": other_user_id,
                "workspace_id": workspace_id,
                "email": f"{other_user_id}@example.test",
                "created_at": now,
            },
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
                "user_id": other_user_id,
                "token_hash": sha256(other_token.encode()).hexdigest(),
                "expires_at": now + timedelta(hours=1),
                "last_seen_at": now,
            },
        )
    return other_user_id, other_token


def test_get_plan_hidden_from_other_user_in_same_workspace(
    planning_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    """Finding #1 (critical IDOR): ``get_plan`` filtered only by
    ``workspace_id`` -- any authenticated user in the workspace could read
    another user's plan by guessing/enumerating plan IDs. Fails before the
    fix (200 with the other user's plan body), passes after (404).
    """
    client, workspace_id, user_id, token = planning_test_context
    plan = _create_plan_with_one_block(client, workspace_id, user_id, token)

    _other_user_id, other_token = _add_second_user_in_same_workspace(workspace_id)
    other_client = TestClient(app)
    other_client.cookies.set("ecc_session", other_token)
    try:
        response = other_client.get(f"/api/v1/plans/{plan['id']}")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "PLAN_NOT_FOUND"
    finally:
        other_client.close()


def test_accept_plan_rejected_for_other_user_in_same_workspace(
    planning_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    """Same finding #1, mutating side: ``_get_plan_for_update`` (used by
    accept/supersede/replan/move_block/remove_block) had the identical gap
    -- any user in the workspace could accept (or otherwise mutate)
    another user's plan. Fails before the fix (200, plan accepted by the
    wrong user), passes after (404, plan untouched).
    """
    client, workspace_id, user_id, token = planning_test_context
    plan = _create_plan_with_one_block(client, workspace_id, user_id, token)

    _other_user_id, other_token = _add_second_user_in_same_workspace(workspace_id)
    other_client = TestClient(app)
    other_client.cookies.set("ecc_session", other_token)
    try:
        response = other_client.post(
            f"/api/v1/plans/{plan['id']}/accept",
            headers=_headers(other_token, "cross-user-accept"),
            json={"expected_version": plan["version"]},
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "PLAN_NOT_FOUND"
    finally:
        other_client.close()

    # The plan must be untouched -- still 'proposed' at its original
    # version, visible to its real owner.
    owned = client.get(f"/api/v1/plans/{plan['id']}")
    assert owned.status_code == 200
    assert owned.json()["status"] == "proposed"
    assert owned.json()["version"] == plan["version"]


# ---------------------------------------------------------------------------
# Task 6: acceptance, manual retirement, replan diff, block editing.
# ---------------------------------------------------------------------------


def _create_plan_with_one_block(
    client: TestClient, workspace_id: UUID, user_id: UUID, token: str
) -> dict:
    _set_full_week_capacity(client, token)
    _seed_task(workspace_id, user_id, "Only candidate")
    regenerate = client.post("/api/v1/attention/regenerate", headers=_headers(token), json={})
    assert regenerate.status_code == 200
    period_start, period_end = _next_period()
    created = client.post(
        "/api/v1/plans",
        headers=_headers(token, "create-plan-for-task6"),
        json={"period_start": period_start.isoformat(), "period_end": period_end.isoformat()},
    )
    assert created.status_code == 201, created.text
    assert len(created.json()["blocks"]) == 1
    return created.json()


def test_accept_plan_transitions_status_and_is_idempotent_on_replay(
    planning_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, user_id, token = planning_test_context
    plan = _create_plan_with_one_block(client, workspace_id, user_id, token)
    headers = _headers(token, "accept-plan")

    accepted = client.post(
        f"/api/v1/plans/{plan['id']}/accept",
        headers=headers,
        json={"expected_version": plan["version"]},
    )
    assert accepted.status_code == 200, accepted.text
    body = accepted.json()
    assert body["status"] == "accepted"
    assert body["accepted_at"] is not None
    assert all(b["status"] == "accepted" for b in body["blocks"])

    replay = client.post(
        f"/api/v1/plans/{plan['id']}/accept",
        headers=headers,
        json={"expected_version": plan["version"]},
    )
    assert replay.status_code == 200
    replay_body = replay.json()
    for key in ("id", "status", "version", "accepted_at", "blocks"):
        assert replay_body[key] == body[key]


def test_accept_plan_rejects_stale_version(
    planning_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, user_id, token = planning_test_context
    plan = _create_plan_with_one_block(client, workspace_id, user_id, token)

    stale = client.post(
        f"/api/v1/plans/{plan['id']}/accept",
        headers=_headers(token, "accept-stale"),
        json={"expected_version": 999},
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "VERSION_CONFLICT"


def test_accept_plan_rejects_when_not_proposed(
    planning_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, user_id, token = planning_test_context
    plan = _create_plan_with_one_block(client, workspace_id, user_id, token)

    first = client.post(
        f"/api/v1/plans/{plan['id']}/accept",
        headers=_headers(token, "accept-first"),
        json={"expected_version": plan["version"]},
    )
    assert first.status_code == 200

    second = client.post(
        f"/api/v1/plans/{plan['id']}/accept",
        headers=_headers(token, "accept-second"),
        json={"expected_version": first.json()["version"]},
    )
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "PLAN_NOT_PROPOSED"


def test_accept_plan_rejects_stale_source(
    planning_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    """PLANNING-CONTRACT.md's Replanning section: source changes mark a
    proposal stale. Changing capacity after the plan was generated must
    reject acceptance with stale_plan, prompting a replan first.
    """
    client, workspace_id, user_id, token = planning_test_context
    plan = _create_plan_with_one_block(client, workspace_id, user_id, token)

    changed = client.put(
        "/api/v1/planning/capacity",
        headers=_headers(token, "change-capacity-stale-source"),
        json={
            "expected_version": 1,
            "timezone": "Asia/Kolkata",
            "days": [
                {"weekday": w, "available_minutes": 600, "focus_minutes": 300} for w in range(7)
            ],
        },
    )
    assert changed.status_code == 200

    stale = client.post(
        f"/api/v1/plans/{plan['id']}/accept",
        headers=_headers(token, "accept-stale-source"),
        json={"expected_version": plan["version"]},
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "STALE_PLAN"


def test_move_block_produces_new_plan_version(
    planning_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, user_id, token = planning_test_context
    plan = _create_plan_with_one_block(client, workspace_id, user_id, token)
    block = plan["blocks"][0]
    new_start = datetime.fromisoformat(block["starts_at"]) + timedelta(hours=2)
    new_end = datetime.fromisoformat(block["ends_at"]) + timedelta(hours=2)

    moved = client.post(
        f"/api/v1/plans/{plan['id']}/blocks/{block['id']}/move",
        headers=_headers(token, "move-block"),
        json={
            "expected_version": plan["version"],
            "starts_at": new_start.isoformat(),
            "ends_at": new_end.isoformat(),
        },
    )
    assert moved.status_code == 200, moved.text
    body = moved.json()
    assert body["version"] == plan["version"] + 1
    assert body["id"] == plan["id"]  # same plan, new version -- not a new plan
    assert datetime.fromisoformat(body["blocks"][0]["starts_at"]) == new_start


def test_move_block_rejects_overlap_with_another_block_in_the_same_plan(
    planning_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, user_id, token = planning_test_context
    _set_full_week_capacity(client, token)
    _seed_task(workspace_id, user_id, "First candidate")
    _seed_task(workspace_id, user_id, "Second candidate")
    regenerate = client.post("/api/v1/attention/regenerate", headers=_headers(token), json={})
    assert regenerate.status_code == 200
    period_start, period_end = _next_period()
    created = client.post(
        "/api/v1/plans",
        headers=_headers(token, "create-plan-for-overlap"),
        json={"period_start": period_start.isoformat(), "period_end": period_end.isoformat()},
    )
    assert created.status_code == 201, created.text
    plan = created.json()
    assert len(plan["blocks"]) == 2
    first_block, second_block = plan["blocks"]
    assert datetime.fromisoformat(first_block["starts_at"]) != datetime.fromisoformat(
        second_block["starts_at"]
    )

    moved = client.post(
        f"/api/v1/plans/{plan['id']}/blocks/{first_block['id']}/move",
        headers=_headers(token, "move-block-overlap"),
        json={
            "expected_version": plan["version"],
            "starts_at": second_block["starts_at"],
            "ends_at": second_block["ends_at"],
        },
    )
    assert moved.status_code == 422, moved.text
    assert moved.json()["error"]["code"] == "BLOCK_OVERLAP"

    unchanged = client.get(f"/api/v1/plans/{plan['id']}")
    assert unchanged.status_code == 200
    assert unchanged.json()["version"] == plan["version"]  # rejected move did not bump version


def test_move_block_rejects_move_outside_period_bounds(
    planning_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    """Finding #11: move_block previously let a block move to any time,
    including outside the plan's own planned period.
    """
    client, workspace_id, user_id, token = planning_test_context
    plan = _create_plan_with_one_block(client, workspace_id, user_id, token)
    block = plan["blocks"][0]
    # _next_period() is a single day -- three days later is well outside it.
    new_start = datetime.fromisoformat(block["starts_at"]) + timedelta(days=3)
    new_end = datetime.fromisoformat(block["ends_at"]) + timedelta(days=3)

    moved = client.post(
        f"/api/v1/plans/{plan['id']}/blocks/{block['id']}/move",
        headers=_headers(token, "move-block-out-of-period"),
        json={
            "expected_version": plan["version"],
            "starts_at": new_start.isoformat(),
            "ends_at": new_end.isoformat(),
        },
    )
    assert moved.status_code == 422, moved.text
    assert moved.json()["error"]["code"] == "BLOCK_OUT_OF_PERIOD"

    unchanged = client.get(f"/api/v1/plans/{plan['id']}")
    assert unchanged.json()["version"] == plan["version"]


def test_move_block_rejects_hard_constraint_conflict(
    planning_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    """Finding #11: move_block previously didn't check hard calendar
    reservations/planning constraints at all -- a block could be moved
    directly onto one.
    """
    client, workspace_id, user_id, token = planning_test_context
    plan = _create_plan_with_one_block(client, workspace_id, user_id, token)
    block = plan["blocks"][0]

    # A hard fixed_time constraint later the same day, clear of the
    # original block's slot.
    constraint_start = datetime.fromisoformat(block["starts_at"]) + timedelta(hours=3)
    constraint_end = constraint_start + timedelta(minutes=30)
    constraint = client.post(
        "/api/v1/planning/constraints",
        headers={**_headers(token), "Idempotency-Key": "move-block-hard-constraint"},
        json={
            "kind": "fixed_time",
            "label": "Unmovable board call",
            "starts_at": constraint_start.isoformat(),
            "ends_at": constraint_end.isoformat(),
            "hardness": "hard",
            "priority": 90,
        },
    )
    assert constraint.status_code == 201, constraint.text

    moved = client.post(
        f"/api/v1/plans/{plan['id']}/blocks/{block['id']}/move",
        headers=_headers(token, "move-block-onto-constraint"),
        json={
            "expected_version": plan["version"],
            "starts_at": constraint_start.isoformat(),
            "ends_at": constraint_end.isoformat(),
        },
    )
    assert moved.status_code == 409, moved.text
    assert moved.json()["error"]["code"] == "CONSTRAINT_CONFLICT"

    unchanged = client.get(f"/api/v1/plans/{plan['id']}")
    assert unchanged.json()["version"] == plan["version"]


def test_move_block_rejected_when_plan_not_proposed(
    planning_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, user_id, token = planning_test_context
    plan = _create_plan_with_one_block(client, workspace_id, user_id, token)
    block = plan["blocks"][0]

    accepted = client.post(
        f"/api/v1/plans/{plan['id']}/accept",
        headers=_headers(token, "accept-before-move"),
        json={"expected_version": plan["version"]},
    )
    assert accepted.status_code == 200

    moved = client.post(
        f"/api/v1/plans/{plan['id']}/blocks/{block['id']}/move",
        headers=_headers(token, "move-after-accept"),
        json={
            "expected_version": accepted.json()["version"],
            "starts_at": block["starts_at"],
            "ends_at": block["ends_at"],
        },
    )
    assert moved.status_code == 409
    assert moved.json()["error"]["code"] == "PLAN_NOT_EDITABLE"


def test_remove_block_produces_new_plan_version(
    planning_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, user_id, token = planning_test_context
    plan = _create_plan_with_one_block(client, workspace_id, user_id, token)
    block = plan["blocks"][0]

    removed = client.post(
        f"/api/v1/plans/{plan['id']}/blocks/{block['id']}/remove",
        headers=_headers(token, "remove-block"),
        json={"expected_version": plan["version"]},
    )
    assert removed.status_code == 200, removed.text
    body = removed.json()
    assert body["version"] == plan["version"] + 1
    assert body["blocks"] == []


def test_supersede_plan_manual_retirement(
    planning_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, user_id, token = planning_test_context
    plan = _create_plan_with_one_block(client, workspace_id, user_id, token)

    superseded = client.post(
        f"/api/v1/plans/{plan['id']}/supersede",
        headers=_headers(token, "supersede-plan"),
        json={"expected_version": plan["version"]},
    )
    assert superseded.status_code == 200
    assert superseded.json()["status"] == "superseded"
    assert superseded.json()["superseded_by"] is None  # manual retirement, no replacement


def test_replan_creates_new_plan_with_diff_and_supersedes_old(
    planning_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    """PLANNING-CONTRACT.md's Replanning section: replanning creates a new
    proposal with a diff (added/removed/moved/unchanged/newly-conflicted)
    against the prior version; the old plan is superseded, never silently
    rewritten.
    """
    client, workspace_id, user_id, token = planning_test_context
    plan = _create_plan_with_one_block(client, workspace_id, user_id, token)
    original_block = plan["blocks"][0]

    # Lower priority than the original ("high") candidate so it can never
    # outrank or tie with it for the same slot -- the test asserts the
    # original block is "unchanged", which only holds if it keeps its slot.
    _seed_task(workspace_id, user_id, "New candidate for replan", priority="low")
    regenerate = client.post("/api/v1/attention/regenerate", headers=_headers(token), json={})
    assert regenerate.status_code == 200

    replanned = client.post(
        f"/api/v1/plans/{plan['id']}/propose",
        headers=_headers(token, "replan"),
        json={"expected_version": plan["version"]},
    )
    assert replanned.status_code == 201, replanned.text
    new_plan = replanned.json()
    assert new_plan["id"] != plan["id"]
    assert new_plan["status"] == "proposed"
    assert len(new_plan["blocks"]) == 2

    diff = new_plan["diff"]
    assert diff is not None
    changes_by_source_id = {entry["source_id"]: entry["change"] for entry in diff}
    assert changes_by_source_id[original_block["source_id"]] == "unchanged"
    added = [entry for entry in diff if entry["change"] == "added"]
    assert len(added) == 1

    old = client.get(f"/api/v1/plans/{plan['id']}")
    assert old.status_code == 200
    assert old.json()["status"] == "superseded"
    assert old.json()["superseded_by"] == new_plan["id"]


def test_replan_rejects_stale_version(
    planning_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, user_id, token = planning_test_context
    plan = _create_plan_with_one_block(client, workspace_id, user_id, token)

    stale = client.post(
        f"/api/v1/plans/{plan['id']}/propose",
        headers=_headers(token, "replan-stale"),
        json={"expected_version": 999},
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "VERSION_CONFLICT"


def test_replan_idempotent_on_replay_and_records_201(
    planning_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    """Finding #12: ``_store_idempotent_plan`` hardcoded
    ``response_status=200`` for every caller, including ``replan``, whose
    endpoint actually returns 201. The replayed HTTP response is always
    correct either way (FastAPI applies the route's declared status code
    regardless of what's stored), but the *stored* idempotency record must
    reflect the real status for it to be a trustworthy audit record.
    """
    client, workspace_id, user_id, token = planning_test_context
    plan = _create_plan_with_one_block(client, workspace_id, user_id, token)
    headers = _headers(token, "replan-idempotent")

    first = client.post(
        f"/api/v1/plans/{plan['id']}/propose",
        headers=headers,
        json={"expected_version": plan["version"]},
    )
    assert first.status_code == 201, first.text

    replay = client.post(
        f"/api/v1/plans/{plan['id']}/propose",
        headers=headers,
        json={"expected_version": plan["version"]},
    )
    assert replay.status_code == 201
    assert replay.json()["id"] == first.json()["id"]

    stored_status = (
        engine.connect()
        .execute(
            text(
                "SELECT response_status FROM idempotency_records "
                "WHERE workspace_id = :workspace_id AND actor_id = :actor_id "
                "AND key = 'replan-idempotent'"
            ),
            {"workspace_id": workspace_id, "actor_id": user_id},
        )
        .scalar_one()
    )
    assert stored_status == 201
