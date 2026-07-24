"""Phase 4 Task 1: model/provider registry and deterministic router.

Covers, per `docs/superpowers/plans/2026-07-23-phase-4-ai-runtime.md`'s
Task 1:

1. The fixed eligibility-then-preference routing pipeline
   (`MODEL-ROUTING-CONTRACT.md`, design doc Decision 2) against a seeded
   in-memory/fixture registry -- every one of the seven eligibility steps,
   in order, plus all five preference steps -- no live Ollama, no database.
2. Migration `0028_phase4_model_registry.py`'s seeded `model_definitions`/
   `routing_policies` rows, applied via `alembic upgrade head`.
3. `GET /api/v1/ai/models` and `GET /api/v1/ai/policies` -- authenticated,
   read-only, matching every existing Phase 1-3 endpoint's session
   requirement.
4. `ollama_client.py:OllamaAdapter` against a mocked HTTP transport
   (`httpx.MockTransport` -- this codebase's own HTTPX usage has no
   existing third-party mocking-library convention to match, and
   `MockTransport` is part of the already-pinned `httpx` package, adding no
   new dependency): request shape, streaming success, error handling, and
   the 20s per-model-call timeout never being exceeded.
5. Routing-overhead performance: p95 <50ms for the pipeline against a small
   registry (`PHASE-004-ai-runtime.md`'s own NFR, design doc Decision 5).
"""

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from time import perf_counter
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from ecc.config import get_settings
from ecc.database import engine
from ecc.domains.ai_runtime import router as air
from ecc.domains.ai_runtime.ollama_client import (
    Chunk,
    OllamaAdapter,
    OllamaCallFailed,
    OllamaCallTimeout,
)
from ecc.domains.ai_runtime.registry import ModelDefinition
from ecc.main import app

settings = get_settings()
pytestmark = pytest.mark.skipif(
    not settings.database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)

SEEDED_MODEL_ID = "qwen2.5:1.5b-instruct-q4_K_M"
SECOND_SEEDED_MODEL_ID = "qwen2.5:3b-instruct-q4_K_M"
SEEDED_TASK_TYPE = "attention.explain_item"


# ---------------------------------------------------------------------------
# Fixture registry helpers (Step 1 -- no live Ollama, no database)
# ---------------------------------------------------------------------------


def _model(
    *,
    model_id: str = "local-model",
    provider: str = "ollama",
    deployment: str = "local",
    data_classes: tuple[str, ...] = ("public", "internal", "sensitive", "restricted"),
    capabilities: tuple[str, ...] = ("explanation",),
    context_window_tokens: int = 32768,
    structured_output_supported: bool = True,
    status: str = "active",
) -> ModelDefinition:
    return ModelDefinition(
        id=uuid4(),
        provider=provider,
        model_id=model_id,
        deployment=deployment,
        data_classes=data_classes,
        capabilities=capabilities,
        context_window_tokens=context_window_tokens,
        structured_output_supported=structured_output_supported,
        status=status,
    )


def _ctx(prompt_tokens: int = 1000, output_tokens: int = 512) -> air.ContextEstimate:
    return air.ContextEstimate(
        estimated_prompt_tokens=prompt_tokens, declared_max_output_tokens=output_tokens
    )


# ---------------------------------------------------------------------------
# Eligibility pipeline -- each of the seven hard-filter steps, in order
# ---------------------------------------------------------------------------


def test_route_unregistered_task_type_is_feature_disabled() -> None:
    """A task type with no `TASK_REQUIREMENTS` entry is rejected before any
    candidate is even considered -- `feature_disabled`, matching
    `API-SCHEMAS.md`'s Errors section ("a task/tool not yet registered in
    this activation").
    """
    decision = air.route("unregistered.task", "sensitive", _ctx(), candidates=[_model()])
    assert isinstance(decision, air.NoEligibleCandidate)
    assert decision.reason == "feature_disabled"


def test_route_no_candidates_registered() -> None:
    decision = air.route(SEEDED_TASK_TYPE, "sensitive", _ctx(), candidates=[])
    assert isinstance(decision, air.NoEligibleCandidate)
    assert decision.reason == "no_candidates_registered"


def test_eligibility_step1_data_residency_excludes_ineligible_data_class() -> None:
    """Step 1: the run's data class must be in the candidate's
    `data_classes` -- evaluated first, per `ADR-0007`'s "sensitive requests
    never silently fall back to cloud".
    """
    candidate = _model(data_classes=("public",))
    decision = air.route(SEEDED_TASK_TYPE, "restricted", _ctx(), candidates=[candidate])
    assert isinstance(decision, air.NoEligibleCandidate)
    assert decision.reason == "data_class_not_eligible"


def test_eligibility_step2_required_capability_excludes_missing_capability() -> None:
    """Step 2: the task's declared capability must be in the candidate's
    `capabilities`.
    """
    candidate = _model(capabilities=("summarization",))
    decision = air.route(SEEDED_TASK_TYPE, "sensitive", _ctx(), candidates=[candidate])
    assert isinstance(decision, air.NoEligibleCandidate)
    assert decision.reason == "capability_not_supported"


def test_eligibility_step3_structured_output_excludes_unsupported_candidate() -> None:
    """Step 3: a task requiring schema-validated output excludes a
    candidate with `structured_output_supported=False`.
    """
    candidate = _model(structured_output_supported=False)
    decision = air.route(SEEDED_TASK_TYPE, "sensitive", _ctx(), candidates=[candidate])
    assert isinstance(decision, air.NoEligibleCandidate)
    assert decision.reason == "structured_output_not_supported"


def test_eligibility_step4_context_limit_at_90_percent_margin() -> None:
    """Step 4: `estimated_prompt_tokens + declared_max_output_tokens` must
    fit within 90% of `context_window_tokens` -- exactly at the boundary
    still fails (the margin is a hard <=90% cutoff, not <100%).
    """
    candidate = _model(context_window_tokens=1000)
    # 900 tokens is exactly 90% of 1000 -- fits.
    fits = air.route(
        SEEDED_TASK_TYPE,
        "sensitive",
        _ctx(prompt_tokens=400, output_tokens=500),
        candidates=[candidate],
    )
    assert isinstance(fits, air.RoutingDecision)
    # 901 tokens exceeds the 900-token (90%) margin -- excluded.
    exceeds = air.route(
        SEEDED_TASK_TYPE,
        "sensitive",
        _ctx(prompt_tokens=401, output_tokens=500),
        candidates=[candidate],
    )
    assert isinstance(exceeds, air.NoEligibleCandidate)
    assert exceeds.reason == "context_limit_exceeded"


def test_eligibility_step5_health_excludes_open_circuit() -> None:
    """Step 5: a candidate whose circuit-breaker state is `open` is
    excluded from eligibility entirely.
    """
    candidate = _model()
    decision = air.route(
        SEEDED_TASK_TYPE,
        "sensitive",
        _ctx(),
        candidates=[candidate],
        candidate_states={candidate.model_id: air.CandidateState(health_state="open")},
    )
    assert isinstance(decision, air.NoEligibleCandidate)
    assert decision.reason == "circuit_open"


def test_eligibility_step5_half_open_is_not_excluded() -> None:
    """`half_open` is a probe state, not an exclusion state -- only `open`
    excludes (`MODEL-ROUTING-CONTRACT.md`: "excluded from eligibility ...
    while open").
    """
    candidate = _model()
    decision = air.route(
        SEEDED_TASK_TYPE,
        "sensitive",
        _ctx(),
        candidates=[candidate],
        candidate_states={candidate.model_id: air.CandidateState(health_state="half_open")},
    )
    assert isinstance(decision, air.RoutingDecision)


def test_eligibility_step6_latency_excludes_over_budget_candidate() -> None:
    """Step 6: rolling observed p95 latency must fit within the task's
    declared timeout minus the fixed 500ms overhead reserve. The seeded
    `attention.explain_item` timeout is 20s, so 19.6s observed p95 exceeds
    the 19.5s effective budget.
    """
    candidate = _model()
    decision = air.route(
        SEEDED_TASK_TYPE,
        "sensitive",
        _ctx(),
        candidates=[candidate],
        candidate_states={
            candidate.model_id: air.CandidateState(observed_p95_latency_seconds=19.6)
        },
    )
    assert isinstance(decision, air.NoEligibleCandidate)
    assert decision.reason == "latency_budget_exceeded"


def test_eligibility_step6_no_observed_latency_is_not_excluded() -> None:
    """A candidate with no observed latency history yet (never called for
    this task type) is not excluded -- otherwise the very first call for
    any task type would be permanently unroutable.
    """
    candidate = _model()
    decision = air.route(
        SEEDED_TASK_TYPE,
        "sensitive",
        _ctx(),
        candidates=[candidate],
        candidate_states={
            candidate.model_id: air.CandidateState(observed_p95_latency_seconds=None)
        },
    )
    assert isinstance(decision, air.RoutingDecision)


def test_eligibility_step7_budget_excludes_exhausted_candidate() -> None:
    """Step 7: remaining run/session token and time budget must be
    non-zero.
    """
    candidate = _model()
    decision = air.route(
        SEEDED_TASK_TYPE,
        "sensitive",
        _ctx(),
        candidates=[candidate],
        candidate_states={candidate.model_id: air.CandidateState(remaining_budget=0)},
    )
    assert isinstance(decision, air.NoEligibleCandidate)
    assert decision.reason == "budget_exhausted"


# ---------------------------------------------------------------------------
# Preference pipeline -- only reached with more than one eligible candidate
# ---------------------------------------------------------------------------


def test_preference_step1_local_before_remote() -> None:
    remote = _model(model_id="a-remote-model", deployment="remote")
    local = _model(model_id="z-local-model", deployment="local")
    decision = air.route(SEEDED_TASK_TYPE, "sensitive", _ctx(), candidates=[remote, local])
    assert isinstance(decision, air.RoutingDecision)
    assert decision.model_id == "z-local-model"


def test_preference_step2_evaluation_quality_floor() -> None:
    """A candidate that has passed the task's evaluation quality floor is
    preferred over one that has not, even when the one that has not is
    otherwise "better" on every later tie-break field.
    """
    passed = _model(model_id="z-passed")
    not_passed = _model(model_id="a-not-passed")
    decision = air.route(
        SEEDED_TASK_TYPE,
        "sensitive",
        _ctx(),
        candidates=[passed, not_passed],
        candidate_states={
            passed.model_id: air.CandidateState(evaluation_quality_floor_passed=True),
            not_passed.model_id: air.CandidateState(evaluation_quality_floor_passed=False),
        },
    )
    assert isinstance(decision, air.RoutingDecision)
    assert decision.model_id == "z-passed"


def test_preference_step3_lower_cost() -> None:
    cheap = _model(model_id="z-cheap")
    expensive = _model(model_id="a-expensive")
    decision = air.route(
        SEEDED_TASK_TYPE,
        "sensitive",
        _ctx(),
        candidates=[cheap, expensive],
        candidate_states={
            cheap.model_id: air.CandidateState(observed_cost=0.0),
            expensive.model_id: air.CandidateState(observed_cost=1.0),
        },
    )
    assert isinstance(decision, air.RoutingDecision)
    assert decision.model_id == "z-cheap"


def test_preference_step4_lower_observed_latency() -> None:
    fast = _model(model_id="z-fast")
    slow = _model(model_id="a-slow")
    decision = air.route(
        SEEDED_TASK_TYPE,
        "sensitive",
        _ctx(),
        candidates=[fast, slow],
        candidate_states={
            fast.model_id: air.CandidateState(observed_p95_latency_seconds=1.0),
            slow.model_id: air.CandidateState(observed_p95_latency_seconds=5.0),
        },
    )
    assert isinstance(decision, air.RoutingDecision)
    assert decision.model_id == "z-fast"


def test_preference_step5_deterministic_model_id_tie_break() -> None:
    """When every earlier preference field is equal, the final tie-break is
    ascending `model_id` string comparison.
    """
    b = _model(model_id="b-model")
    a = _model(model_id="a-model")
    decision = air.route(SEEDED_TASK_TYPE, "sensitive", _ctx(), candidates=[b, a])
    assert isinstance(decision, air.RoutingDecision)
    assert decision.model_id == "a-model"

    # Order-independence: the same candidates in the opposite input order
    # produce the same winner -- proves this is a real deterministic sort,
    # not incidentally picking "whichever came first".
    decision_reordered = air.route(SEEDED_TASK_TYPE, "sensitive", _ctx(), candidates=[a, b])
    assert isinstance(decision_reordered, air.RoutingDecision)
    assert decision_reordered.model_id == "a-model"


def test_preference_reached_only_with_multiple_eligible_candidates() -> None:
    """A single eligible candidate is returned directly -- the preference
    stage is a no-op for slice 1's single-model registry (design doc
    Decision 2), exercised here with a candidate that would otherwise lose
    every preference tie-break if it had to compete.
    """
    only = _model(model_id="zzz-only", deployment="remote")
    decision = air.route(
        SEEDED_TASK_TYPE,
        "sensitive",
        _ctx(),
        candidates=[only],
        candidate_states={only.model_id: air.CandidateState(evaluation_quality_floor_passed=False)},
    )
    assert isinstance(decision, air.RoutingDecision)
    assert decision.model_id == "zzz-only"


def test_ineligible_candidates_excluded_before_preference_ordering() -> None:
    """An ineligible candidate never wins on preference, even if its
    preference fields look better than an eligible one's -- exclusion is
    absolute, not merely deprioritizing.
    """
    ineligible = _model(model_id="a-ineligible", data_classes=("public",))
    eligible = _model(model_id="z-eligible")
    decision = air.route(SEEDED_TASK_TYPE, "restricted", _ctx(), candidates=[ineligible, eligible])
    assert isinstance(decision, air.RoutingDecision)
    assert decision.model_id == "z-eligible"


# ---------------------------------------------------------------------------
# Migration 0028 -- seeded rows
# ---------------------------------------------------------------------------


def test_model_definitions_seeded_row() -> None:
    with engine.connect() as connection:
        rows = (
            connection.execute(
                text(
                    "SELECT provider, model_id, deployment, data_classes, capabilities, "
                    "context_window_tokens, structured_output_supported, status "
                    "FROM model_definitions ORDER BY model_id"
                )
            )
            .mappings()
            .all()
        )
    # Migration 0028 seeded the first model; migration 0032 (this task's own
    # follow-up, deferred at the time 0028 was written -- see that
    # migration's module docstring) added a second, same task type, so both
    # candidates are real, simultaneously-eligible options for router.py's
    # preference stage to choose between, not just the trivial
    # single-candidate case.
    assert len(rows) == 2
    by_model_id = {record["model_id"]: record for record in rows}
    assert set(by_model_id) == {SEEDED_MODEL_ID, SECOND_SEEDED_MODEL_ID}
    for record in rows:
        assert record["provider"] == "ollama"
        assert record["deployment"] == "local"
        # Deliberately identical data_classes/capabilities across both rows
        # (migration 0032's own docstring) -- if they differed, eligibility
        # filtering alone would decide attention.explain_item routing
        # before the preference/tie-break stage is ever reached with two
        # live candidates.
        assert set(record["data_classes"]) == {"public", "internal", "sensitive", "restricted"}
        assert set(record["capabilities"]) == {"extraction", "summarization", "explanation"}
        assert record["structured_output_supported"] is True
        assert record["status"] == "active"


def test_routing_policies_seeded_row() -> None:
    with engine.connect() as connection:
        rows = (
            connection.execute(
                text(
                    "SELECT task_type, version, candidates, constraints, fallback, status "
                    "FROM routing_policies"
                )
            )
            .mappings()
            .all()
        )
    # Still exactly one policy row -- migration 0032 updates this row's
    # candidates JSONB in place (documentation/audit accuracy; router.py's
    # route() draws its candidate pool from every active model_definitions
    # row, not this column -- see that migration's own docstring), it does
    # not add a second policy row or bump version.
    assert len(rows) == 1
    record = rows[0]
    assert record["task_type"] == SEEDED_TASK_TYPE
    assert record["version"] == 1
    assert record["candidates"] == [
        {"provider": "ollama", "model_id": SEEDED_MODEL_ID},
        {"provider": "ollama", "model_id": SECOND_SEEDED_MODEL_ID},
    ]
    assert record["constraints"]["max_input_tokens"] == 3072
    assert record["constraints"]["max_output_tokens"] == 512
    assert record["constraints"]["per_model_call_timeout_seconds"] == 20
    assert record["fallback"] == {}
    assert record["status"] == "active"


def test_registry_functions_read_the_seeded_row() -> None:
    from ecc.database import SessionFactory
    from ecc.domains.ai_runtime.registry import get_model, list_models

    with SessionFactory() as session:
        models = list_models(session)
        assert len(models) == 2
        assert {model.model_id for model in models} == {SEEDED_MODEL_ID, SECOND_SEEDED_MODEL_ID}
        # list_models orders ascending by model_id -- matching the router's
        # own preference-stage final tie-break (registry.py's docstring).
        assert [model.model_id for model in models] == sorted(
            [SEEDED_MODEL_ID, SECOND_SEEDED_MODEL_ID]
        )

        found = get_model(session, SEEDED_MODEL_ID)
        assert found is not None
        assert found.provider == "ollama"

        found_second = get_model(session, SECOND_SEEDED_MODEL_ID)
        assert found_second is not None
        assert found_second.provider == "ollama"

        assert get_model(session, "nonexistent-model") is None


def test_router_policy_functions_read_the_seeded_row() -> None:
    from ecc.database import SessionFactory

    with SessionFactory() as session:
        policies = air.list_policies(session)
        assert len(policies) == 1
        assert policies[0].task_type == SEEDED_TASK_TYPE
        assert len(policies[0].candidates) == 2

        policy = air.get_policy(session, SEEDED_TASK_TYPE)
        assert policy is not None
        assert policy.version == 1

        assert air.get_policy(session, "unregistered.task") is None


def test_refresh_cache_and_route_pick_correctly_among_two_real_seeded_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every preference test elsewhere in this file exercises `_preference_
    key`'s tie-break logic against synthetic in-memory `ModelDefinition`
    fixtures -- proving the algorithm is correct, but never proving the
    actual migration-seeded, multi-row `model_definitions` table round-trips
    correctly through `refresh_cache` (the real production glue between the
    database and `route()`'s module-level cache) into a real routing
    decision. This test is that missing link: two real rows, read via a
    real `refresh_cache(session)` call, routed via `route()`'s production
    default path (no `candidates`/`candidate_states` override).

    `monkeypatch` restores `router._cached_candidates`/`_cached_states` to
    their pre-test values afterward -- `refresh_cache` mutates module-level
    global state, and leaving a populated cache behind would leak into any
    other test in this process that calls `route()` without an explicit
    `candidates` override.
    """
    from ecc.database import SessionFactory

    monkeypatch.setattr(air, "_cached_candidates", air._cached_candidates)
    monkeypatch.setattr(air, "_cached_states", air._cached_states)

    with SessionFactory() as session:
        air.refresh_cache(session)

    decision = air.route(SEEDED_TASK_TYPE, "sensitive", _ctx())
    assert isinstance(decision, air.RoutingDecision)
    # Both real candidates are brand-new (no observed cost/latency history,
    # migration 0032's own docstring) -- identical on every preference step
    # except the final ascending model_id string tie-break, which
    # "qwen2.5:1.5b..." wins over "qwen2.5:3b..." ('1' < '3').
    assert decision.model_id == SEEDED_MODEL_ID
    assert decision.provider == "ollama"

    # The losing candidate is confirmed present and eligible, not merely
    # absent from the cache -- proves this was a real preference-stage
    # tie-break between two eligible candidates, not eligibility filtering
    # trivially leaving only one.
    eligible_second = air.route(
        SEEDED_TASK_TYPE,
        "sensitive",
        _ctx(),
        candidates=[
            candidate
            for candidate in (air._cached_candidates or [])
            if candidate.model_id == SECOND_SEEDED_MODEL_ID
        ],
    )
    assert isinstance(eligible_second, air.RoutingDecision)
    assert eligible_second.model_id == SECOND_SEEDED_MODEL_ID


# ---------------------------------------------------------------------------
# GET /api/v1/ai/models, GET /api/v1/ai/policies
# ---------------------------------------------------------------------------


@pytest.fixture
def ai_runtime_test_context() -> Iterator[tuple[TestClient, UUID, str]]:
    workspace_id = uuid4()
    user_id = uuid4()
    token = f"session-{uuid4()}"
    now = datetime.now(UTC)

    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, 'AI Runtime Test', 'Asia/Kolkata', :created_at)"
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
        yield client, workspace_id, token
    finally:
        client.close()
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM sessions WHERE workspace_id = :workspace_id"),
                {"workspace_id": workspace_id},
            )
            connection.execute(
                text("DELETE FROM users WHERE workspace_id = :workspace_id"),
                {"workspace_id": workspace_id},
            )
            connection.execute(
                text("DELETE FROM workspaces WHERE id = :workspace_id"),
                {"workspace_id": workspace_id},
            )


def test_get_ai_models_returns_seeded_model(
    ai_runtime_test_context: tuple[TestClient, UUID, str],
) -> None:
    client, _workspace_id, _token = ai_runtime_test_context
    response = client.get("/api/v1/ai/models")
    assert response.status_code == 200
    body = response.json()
    assert len(body["models"]) == 2
    models_by_id = {model["model_id"]: model for model in body["models"]}
    assert set(models_by_id) == {SEEDED_MODEL_ID, SECOND_SEEDED_MODEL_ID}
    for model in body["models"]:
        assert model["provider"] == "ollama"
        assert model["deployment"] == "local"
        assert set(model["data_classes"]) == {"public", "internal", "sensitive", "restricted"}
        assert set(model["capabilities"]) == {"extraction", "summarization", "explanation"}
    # No caller-supplied model_id/provider is ever accepted by this
    # endpoint (`MODEL-ROUTING-CONTRACT.md`) -- it is a bare GET with no
    # request body/query parameters selecting a model at all.


def test_get_ai_models_requires_authentication(
    ai_runtime_test_context: tuple[TestClient, UUID, str],
) -> None:
    client, _workspace_id, _token = ai_runtime_test_context
    client.cookies.clear()
    response = client.get("/api/v1/ai/models")
    assert response.status_code == 401


def test_get_ai_policies_returns_seeded_policy(
    ai_runtime_test_context: tuple[TestClient, UUID, str],
) -> None:
    client, _workspace_id, _token = ai_runtime_test_context
    response = client.get("/api/v1/ai/policies")
    assert response.status_code == 200
    body = response.json()
    assert len(body["policies"]) == 1
    policy = body["policies"][0]
    assert policy["task_type"] == SEEDED_TASK_TYPE
    assert policy["version"] == 1
    assert policy["status"] == "active"
    assert policy["candidates"] == [
        {"provider": "ollama", "model_id": SEEDED_MODEL_ID},
        {"provider": "ollama", "model_id": SECOND_SEEDED_MODEL_ID},
    ]


def test_get_ai_policies_requires_authentication(
    ai_runtime_test_context: tuple[TestClient, UUID, str],
) -> None:
    client, _workspace_id, _token = ai_runtime_test_context
    client.cookies.clear()
    response = client.get("/api/v1/ai/policies")
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# ollama_client.py:OllamaAdapter -- mocked HTTP transport, no live Ollama
# ---------------------------------------------------------------------------


def _ndjson_response(parts: list[dict]) -> httpx.Response:
    body = "\n".join(json.dumps(part) for part in parts) + "\n"
    return httpx.Response(
        200, content=body.encode(), headers={"content-type": "application/x-ndjson"}
    )


def test_ollama_adapter_generate_request_shape() -> None:
    """Asserts the exact request Ollama's streaming `generate` endpoint
    receives: method, path, and body fields (`model`, `prompt`, `stream`,
    and `max_tokens` mapped to Ollama's `options.num_predict`).
    """
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _ndjson_response(
            [{"model": "m", "created_at": "now", "response": "ok", "done": True, "eval_count": 1}]
        )

    adapter = OllamaAdapter(host="http://localhost:11434", transport=httpx.MockTransport(handler))
    list(adapter.generate("explain this item", "qwen2.5:1.5b-instruct-q4_K_M", 512))

    assert len(captured) == 1
    request = captured[0]
    assert request.method == "POST"
    assert request.url.path == "/api/generate"
    payload = json.loads(request.content)
    assert payload["model"] == "qwen2.5:1.5b-instruct-q4_K_M"
    assert payload["prompt"] == "explain this item"
    assert payload["stream"] is True
    assert payload["options"]["num_predict"] == 512


def test_ollama_adapter_generate_streams_chunks_in_order() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _ndjson_response(
            [
                {"model": "m", "created_at": "now", "response": "Hello", "done": False},
                {
                    "model": "m",
                    "created_at": "now",
                    "response": " world",
                    "done": True,
                    "eval_count": 2,
                    "prompt_eval_count": 5,
                },
            ]
        )

    adapter = OllamaAdapter(transport=httpx.MockTransport(handler))
    chunks = list(adapter.generate("hi", "m", 100))
    assert chunks == [
        Chunk(text="Hello", done=False, eval_count=None, prompt_eval_count=None),
        Chunk(text=" world", done=True, eval_count=2, prompt_eval_count=5),
    ]


def test_ollama_adapter_list_models() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        return httpx.Response(200, json={"models": [{"model": SEEDED_MODEL_ID}]})

    adapter = OllamaAdapter(transport=httpx.MockTransport(handler))
    assert adapter.list_models() == [SEEDED_MODEL_ID]


def test_ollama_adapter_response_error_raises_typed_exception() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "model not found"})

    adapter = OllamaAdapter(transport=httpx.MockTransport(handler))
    with pytest.raises(OllamaCallFailed):
        adapter.list_models()


def test_ollama_adapter_never_blocks_past_per_model_call_timeout() -> None:
    """The client never blocks past the design doc's 20s per-model-call
    timeout, even when the (mocked) server keeps streaming chunks that
    individually arrive fast but cumulatively exceed the deadline. A fake
    clock stands in for wall-clock time so this test runs in milliseconds,
    not 20 real seconds.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        parts = [
            {"model": "m", "created_at": "now", "response": f"tok{i}", "done": False}
            for i in range(50)
        ]
        parts.append({"model": "m", "created_at": "now", "response": "", "done": True})
        return _ndjson_response(parts)

    elapsed = {"seconds": 0.0}

    def fake_clock() -> float:
        # Each check "costs" 3 simulated seconds -- the 20s budget is
        # exhausted well before all 51 chunks are consumed.
        elapsed["seconds"] += 3.0
        return elapsed["seconds"]

    adapter = OllamaAdapter(
        host="http://localhost:11434",
        timeout_seconds=20.0,
        transport=httpx.MockTransport(handler),
        clock=fake_clock,
    )

    received: list[Chunk] = []
    with pytest.raises(OllamaCallTimeout):
        for chunk in adapter.generate("hi", "m", 100):
            received.append(chunk)

    # Proves the adapter actually stopped early -- it did not drain all 51
    # available chunks before raising.
    assert 0 < len(received) < 51


def test_ollama_adapter_completes_within_timeout_when_fast() -> None:
    """The converse of the above: a call that finishes well inside the
    timeout budget is not spuriously interrupted.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return _ndjson_response(
            [{"model": "m", "created_at": "now", "response": "done", "done": True, "eval_count": 1}]
        )

    adapter = OllamaAdapter(
        host="http://localhost:11434",
        timeout_seconds=20.0,
        transport=httpx.MockTransport(handler),
    )
    chunks = list(adapter.generate("hi", "m", 100))
    assert len(chunks) == 1
    assert chunks[0].done is True


# ---------------------------------------------------------------------------
# Routing-overhead performance: p95 <50ms (design doc Decision 5)
# ---------------------------------------------------------------------------

ROUTING_P95_BUDGET_SECONDS = 0.050
ROUTING_SAMPLE_SIZE = 200


def _p95(samples: list[float]) -> float:
    """Nearest-rank 95th percentile, matching this codebase's existing
    `_p95` helper convention (`tests/test_risks_attention_postgres.py`).
    """
    ordered = sorted(samples)
    index = min(len(ordered) - 1, -(-(95 * len(ordered)) // 100) - 1)
    return ordered[index]


def _small_registry(size: int = 10) -> list[ModelDefinition]:
    return [_model(model_id=f"model-{i:03d}") for i in range(size)]


def test_routing_overhead_p95_under_50ms() -> None:
    """The eligibility/preference pipeline is pure in-memory comparison
    against a cached snapshot (design doc Decision 2) -- no database, no
    network call -- so it comfortably stays inside the phase's own p95
    <50ms non-functional requirement even measured directly in-process
    (a tighter, more direct measurement than an HTTP round-trip would be).
    """
    candidates = _small_registry()
    context_estimate = _ctx()
    states = {
        candidate.model_id: air.CandidateState(observed_p95_latency_seconds=1.0 + i * 0.1)
        for i, candidate in enumerate(candidates)
    }

    # Untimed warm-up, matching this codebase's existing p95 test
    # convention (test_risks_attention_postgres.py) -- avoids an unprimed
    # first-call cost inflating the timed sample.
    air.route(
        SEEDED_TASK_TYPE,
        "sensitive",
        context_estimate,
        candidates=candidates,
        candidate_states=states,
    )

    samples: list[float] = []
    for _ in range(ROUTING_SAMPLE_SIZE):
        started = perf_counter()
        decision = air.route(
            SEEDED_TASK_TYPE,
            "sensitive",
            context_estimate,
            candidates=candidates,
            candidate_states=states,
        )
        samples.append(perf_counter() - started)
        assert isinstance(decision, air.RoutingDecision)

    p95 = _p95(samples)
    assert p95 < ROUTING_P95_BUDGET_SECONDS, (
        f"routing overhead p95 {p95 * 1000:.3f} ms exceeded the "
        f"{ROUTING_P95_BUDGET_SECONDS * 1000:.0f} ms budget; "
        f"samples(ms)={[round(s * 1000, 3) for s in samples[:10]]}..."
    )
