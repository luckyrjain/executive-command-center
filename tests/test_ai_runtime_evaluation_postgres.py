"""Phase 4 Task 5: the evaluation harness and first dataset
(`ecc.domains.ai_runtime.evaluation`).

Covers, per `docs/superpowers/plans/2026-07-23-phase-4-ai-runtime.md`'s
Task 5 Step 3 and `docs/phases/phase-004/EVALUATION-CONTRACT.md`'s four
metrics:

1. `check_promotion_floors` as a pure function: passes only when all four
   floors (100% schema validity, 100% grounding, zero prohibited-fact
   occurrences, p95 latency <20s) hold simultaneously; fails when any one
   is missed, tested independently for each floor.
2. Migration `0031_phase4_evaluation.py`'s seeded `evaluation_sets` version
   1 row -- 20 examples, matching `tests/fixtures/
   phase4_evaluation_attention_explain.py` exactly.
3. `run_evaluation` end to end (mocked Ollama transport, no live model):
   a fully grounded/valid run over the real 20-example dataset passes every
   floor; a citation to a nonexistent factor on one example fails only the
   grounding floor; a `must_not_state` violation on one example fails only
   the prohibited-fact floor; a permanently schema-invalid response on one
   example fails only the schema-validity floor.
4. `run_evaluation`'s configuration assertions (`EvaluationConfigError`):
   unknown task type, a `prompt_version` that is not the currently active
   one, an unregistered `model_id`, no active `evaluation_sets` row.
5. Ephemeral synthetic `attention_items` rows are cleaned up after a run
   (never left behind for the calling workspace's real Attention Queue).
6. `GET /ai/evaluations`, `POST /ai/evaluations/runs`,
   `GET /ai/evaluations/runs/{id}` -- auth, CSRF, idempotency, cross-
   workspace 404 isolation.
7. The promotion-floor gate wired into `POST /ai/policies/{id}/activate`
   for `attention.explain_item.v1` specifically: rejected with
   `EVALUATION_FLOORS_NOT_MET` when no passing `evaluation_runs` row exists
   in the caller's workspace; allowed once one does. Tool activation
   (`attention.get_item`) is confirmed unaffected by the gate.
"""

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from hmac import new
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from fixtures.phase4_evaluation_attention_explain import DATASET_VERSION, EXAMPLES, TASK_TYPE
from sqlalchemy import text

from ecc.auth import AuthContext
from ecc.config import get_settings
from ecc.database import SessionFactory, engine
from ecc.domains.ai_runtime.evaluation import (
    EvaluationConfigError,
    EvaluationMetrics,
    EvaluationRun,
    check_promotion_floors,
    get_evaluation_run,
    run_evaluation,
)
from ecc.domains.ai_runtime.ollama_client import OllamaAdapter
from ecc.domains.ai_runtime.runtime import get_ollama_adapter, reset_circuit_breakers
from ecc.main import app

settings = get_settings()
pytestmark = pytest.mark.skipif(
    not settings.database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)

_SEEDED_MODEL_ID = "qwen2.5:1.5b-instruct-q4_K_M"
_SEEDED_PROMPT_ID = "attention.explain_item.v1"


# ---------------------------------------------------------------------------
# Section 1: check_promotion_floors -- pure function, no database.
# ---------------------------------------------------------------------------


def _metrics(**overrides: object) -> EvaluationMetrics:
    base = {
        "schema_validity_rate": 1.0,
        "grounding_rate": 1.0,
        "prohibited_fact_count": 0,
        "latency_p95_seconds": 5.0,
        "total_examples": 20,
    }
    base.update(overrides)
    return EvaluationMetrics(**base)  # type: ignore[arg-type]


def _run(metrics: EvaluationMetrics) -> EvaluationRun:
    now = datetime.now(UTC)
    return EvaluationRun(
        id=uuid4(),
        task_type=TASK_TYPE,
        prompt_id=_SEEDED_PROMPT_ID,
        prompt_version=1,
        model_id=_SEEDED_MODEL_ID,
        provider="ollama",
        dataset_version=DATASET_VERSION,
        metrics=metrics,
        failures=[],
        status="completed",
        started_at=now,
        completed_at=now,
    )


def test_check_promotion_floors_passes_when_all_four_floors_met() -> None:
    assert check_promotion_floors(_run(_metrics())) is True


def test_check_promotion_floors_fails_on_schema_validity_below_floor() -> None:
    assert check_promotion_floors(_run(_metrics(schema_validity_rate=0.95))) is False


def test_check_promotion_floors_fails_on_grounding_below_floor() -> None:
    assert check_promotion_floors(_run(_metrics(grounding_rate=0.95))) is False


def test_check_promotion_floors_fails_on_any_prohibited_fact() -> None:
    assert check_promotion_floors(_run(_metrics(prohibited_fact_count=1))) is False


def test_check_promotion_floors_fails_on_latency_at_or_above_ceiling() -> None:
    assert check_promotion_floors(_run(_metrics(latency_p95_seconds=20.0))) is False
    assert check_promotion_floors(_run(_metrics(latency_p95_seconds=25.0))) is False


def test_check_promotion_floors_passes_at_latency_just_under_ceiling() -> None:
    assert check_promotion_floors(_run(_metrics(latency_p95_seconds=19.999))) is True


# ---------------------------------------------------------------------------
# Section 2: the seeded evaluation_sets row.
# ---------------------------------------------------------------------------


def test_seeded_evaluation_set_matches_the_checked_in_fixture() -> None:
    with engine.connect() as connection:
        row = (
            connection.execute(
                text(
                    "SELECT task_type, version, classification, example_count, examples, status "
                    "FROM evaluation_sets WHERE task_type = :task_type AND status = 'active'"
                ),
                {"task_type": TASK_TYPE},
            )
            .mappings()
            .one()
        )
    assert row["version"] == DATASET_VERSION
    assert row["classification"] == "labelled"
    assert row["example_count"] == len(EXAMPLES) == 20
    assert row["examples"] == EXAMPLES
    entity_types = {example["entity_type"] for example in row["examples"]}
    assert entity_types == {
        "task",
        "commitment",
        "risk",
        "waiting_link",
        "risk_review",
        "meeting",
    }


# ---------------------------------------------------------------------------
# Fixtures shared by the run_evaluation / HTTP sections below.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_breakers() -> Iterator[None]:
    reset_circuit_breakers()
    yield
    reset_circuit_breakers()


@pytest.fixture
def run_context() -> Iterator[dict]:
    workspace_id = uuid4()
    user_id = uuid4()
    token = f"session-{uuid4()}"
    now = datetime.now(UTC)

    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, 'AI Runtime Evaluation Test', 'UTC', :created_at)"
            ),
            {"id": workspace_id, "created_at": now},
        )
        connection.execute(
            text(
                "INSERT INTO users (id, workspace_id, email, password_hash, created_at) "
                "VALUES (:id, :workspace_id, :email, 'hash', :created_at)"
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

    yield {
        "workspace_id": workspace_id,
        "user_id": user_id,
        "token": token,
        "auth": AuthContext(workspace_id=workspace_id, user_id=user_id, timezone="UTC"),
    }

    with engine.begin() as connection:
        for table in (
            "generated_artifacts",
            "evaluation_runs",
            "ai_run_steps",
            "ai_runs",
            "event_outbox",
            "audit_events",
            "idempotency_records",
            "attention_items",
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


def _adapter_with_responses(*response_texts: str) -> OllamaAdapter:
    """Mirrors `test_ai_runtime_runtime_postgres.py`'s own helper exactly
    (a real `OllamaAdapter` over `httpx.MockTransport`) -- each call to
    `.generate()` consumes the next queued response text in order; the
    last one repeats if `.generate()` is called more times than supplied.
    """
    remaining = list(response_texts)

    def handler(request: httpx.Request) -> httpx.Response:
        text_value = remaining.pop(0) if remaining else response_texts[-1]
        body = (
            json.dumps(
                {
                    "model": "m",
                    "created_at": "now",
                    "response": text_value,
                    "done": True,
                    "eval_count": 12,
                    "prompt_eval_count": 40,
                }
            )
            + "\n"
        )
        return httpx.Response(
            200, content=body.encode(), headers={"content-type": "application/x-ndjson"}
        )

    return OllamaAdapter(transport=httpx.MockTransport(handler))


def _valid_response(
    example: dict, *, extra_citation: str | None = None, prohibited: bool = False
) -> str:
    explanation = example["reference_explanation"]
    if prohibited and example["must_not_state"]:
        explanation = f"{explanation} Specifically, {example['must_not_state'][0]}."
    cited = list(example["must_cite"])
    if extra_citation is not None:
        cited.append(extra_citation)
    return json.dumps({"explanation_text": explanation, "cited_factor_codes": cited})


def _flat_responses(
    *,
    invalid_key: str | None = None,
    bad_citation_key: str | None = None,
    prohibited_key: str | None = None,
) -> list[str]:
    """One flat, ordered response list spanning every example in
    `EXAMPLES`, matching the exact sequence `run_evaluation` will call
    `.generate()` in (dataset order, one call per example except the
    deliberately schema-invalid one, which consumes two -- the bounded
    repair retry, Task 2's `validate_with_bounded_repair`, unmodified).
    At most one of the three override keys is exercised per test, mirroring
    Task 5 Step 3's four scenarios (a fully valid run needs none of them).
    """
    responses: list[str] = []
    for example in EXAMPLES:
        if example["key"] == invalid_key:
            responses.append("not valid json at all")
            responses.append("still not valid json either")
            continue
        responses.append(
            _valid_response(
                example,
                extra_citation="nonexistent_factor_code"
                if example["key"] == bad_citation_key
                else None,
                prohibited=example["key"] == prohibited_key,
            )
        )
    return responses


# ---------------------------------------------------------------------------
# Section 3: run_evaluation end to end, over the real 20-example dataset.
# ---------------------------------------------------------------------------


def test_run_evaluation_fully_grounded_run_passes_every_floor(run_context: dict) -> None:
    adapter = _adapter_with_responses(*_flat_responses())
    with SessionFactory() as session:
        run = run_evaluation(
            TASK_TYPE,
            1,
            _SEEDED_MODEL_ID,
            session=session,
            auth=run_context["auth"],
            ollama_adapter=adapter,
        )

    assert run.metrics.total_examples == 20
    assert run.metrics.schema_validity_rate == 1.0
    assert run.metrics.grounding_rate == 1.0
    assert run.metrics.prohibited_fact_count == 0
    assert run.metrics.latency_p95_seconds < 20.0
    assert run.failures == []
    assert check_promotion_floors(run) is True

    persisted = get_evaluation_run(session, run_context["auth"], run.id)
    assert persisted is not None
    assert persisted.metrics.schema_validity_rate == 1.0

    with engine.connect() as connection:
        leftover = connection.execute(
            text("SELECT count(*) FROM attention_items WHERE workspace_id = :workspace_id"),
            {"workspace_id": run_context["workspace_id"]},
        ).scalar_one()
    assert leftover == 0, "synthetic attention_items must be cleaned up after the run"

    with engine.connect() as connection:
        artifact_count = connection.execute(
            text("SELECT count(*) FROM generated_artifacts WHERE workspace_id = :workspace_id"),
            {"workspace_id": run_context["workspace_id"]},
        ).scalar_one()
    assert artifact_count == 20


def test_run_evaluation_ungrounded_citation_fails_only_grounding_floor(run_context: dict) -> None:
    bad_key = EXAMPLES[3]["key"]
    adapter = _adapter_with_responses(*_flat_responses(bad_citation_key=bad_key))
    with SessionFactory() as session:
        run = run_evaluation(
            TASK_TYPE,
            1,
            _SEEDED_MODEL_ID,
            session=session,
            auth=run_context["auth"],
            ollama_adapter=adapter,
        )

    assert run.metrics.schema_validity_rate == 1.0
    assert run.metrics.grounding_rate == pytest.approx(19 / 20)
    assert run.metrics.prohibited_fact_count == 0
    assert check_promotion_floors(run) is False
    assert any(
        failure["key"] == bad_key and failure["reason"] == "grounding_failed"
        for failure in run.failures
    )
    # The specific ungrounded code(s) are surfaced too, not just the coarse
    # "grounding_failed" reason -- otherwise diagnosing which citation was
    # bad requires the raw response text, which this codebase never logs.
    grounding_failure = next(failure for failure in run.failures if failure["key"] == bad_key)
    assert grounding_failure["ungrounded_codes"] == ["nonexistent_factor_code"]


def test_run_evaluation_prohibited_fact_fails_only_that_floor(run_context: dict) -> None:
    example_with_probe = next(example for example in EXAMPLES if example["must_not_state"])
    adapter = _adapter_with_responses(*_flat_responses(prohibited_key=example_with_probe["key"]))
    with SessionFactory() as session:
        run = run_evaluation(
            TASK_TYPE,
            1,
            _SEEDED_MODEL_ID,
            session=session,
            auth=run_context["auth"],
            ollama_adapter=adapter,
        )

    assert run.metrics.schema_validity_rate == 1.0
    assert run.metrics.grounding_rate == 1.0
    assert run.metrics.prohibited_fact_count >= 1
    assert check_promotion_floors(run) is False
    assert any(failure["reason"] == "prohibited_fact" for failure in run.failures)


def test_run_evaluation_permanently_schema_invalid_fails_only_that_floor(
    run_context: dict,
) -> None:
    invalid_key = EXAMPLES[7]["key"]
    adapter = _adapter_with_responses(*_flat_responses(invalid_key=invalid_key))
    with SessionFactory() as session:
        run = run_evaluation(
            TASK_TYPE,
            1,
            _SEEDED_MODEL_ID,
            session=session,
            auth=run_context["auth"],
            ollama_adapter=adapter,
        )

    assert run.metrics.schema_validity_rate == pytest.approx(19 / 20)
    assert run.metrics.grounding_rate == pytest.approx(19 / 20)
    assert check_promotion_floors(run) is False
    assert any(
        failure["key"] == invalid_key and failure["reason"] == "schema_invalid"
        for failure in run.failures
    )


# ---------------------------------------------------------------------------
# Section 4: run_evaluation's configuration assertions.
# ---------------------------------------------------------------------------


def test_run_evaluation_unknown_task_type_raises_config_error(run_context: dict) -> None:
    with SessionFactory() as session:
        with pytest.raises(EvaluationConfigError) as exc_info:
            run_evaluation(
                "some.other.task",
                1,
                _SEEDED_MODEL_ID,
                session=session,
                auth=run_context["auth"],
                ollama_adapter=_adapter_with_responses("{}"),
            )
    assert exc_info.value.code == "unknown_task_type"


def test_run_evaluation_inactive_prompt_version_raises_config_error(run_context: dict) -> None:
    with SessionFactory() as session:
        with pytest.raises(EvaluationConfigError) as exc_info:
            run_evaluation(
                TASK_TYPE,
                999,
                _SEEDED_MODEL_ID,
                session=session,
                auth=run_context["auth"],
                ollama_adapter=_adapter_with_responses("{}"),
            )
    assert exc_info.value.code == "prompt_version_not_active"


def test_run_evaluation_unregistered_model_raises_config_error(run_context: dict) -> None:
    with SessionFactory() as session:
        with pytest.raises(EvaluationConfigError) as exc_info:
            run_evaluation(
                TASK_TYPE,
                1,
                "not-a-registered-model",
                session=session,
                auth=run_context["auth"],
                ollama_adapter=_adapter_with_responses("{}"),
            )
    assert exc_info.value.code == "model_not_registered"


def test_run_evaluation_no_active_dataset_raises_config_error(run_context: dict) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE evaluation_sets SET status = 'retired' "
                "WHERE task_type = :task_type AND status = 'active'"
            ),
            {"task_type": TASK_TYPE},
        )
    try:
        with SessionFactory() as session:
            with pytest.raises(EvaluationConfigError) as exc_info:
                run_evaluation(
                    TASK_TYPE,
                    1,
                    _SEEDED_MODEL_ID,
                    session=session,
                    auth=run_context["auth"],
                    ollama_adapter=_adapter_with_responses("{}"),
                )
        assert exc_info.value.code == "evaluation_set_not_found"
    finally:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE evaluation_sets SET status = 'active' "
                    "WHERE task_type = :task_type AND version = :version"
                ),
                {"task_type": TASK_TYPE, "version": DATASET_VERSION},
            )


# ---------------------------------------------------------------------------
# Section 5: HTTP surface -- GET /ai/evaluations, POST /ai/evaluations/runs,
# GET /ai/evaluations/runs/{id}.
# ---------------------------------------------------------------------------


def _headers(token: str, key: str | None = None) -> dict[str, str]:
    csrf = new(settings.session_secret.encode(), token.encode(), "sha256").hexdigest()
    headers = {"X-CSRF-Token": csrf, "X-Correlation-ID": str(uuid4())}
    if key is not None:
        headers["Idempotency-Key"] = key
    return headers


@pytest.fixture
def http_client(run_context: dict) -> Iterator[TestClient]:
    adapter = _adapter_with_responses(*_flat_responses())
    app.dependency_overrides[get_ollama_adapter] = lambda: adapter
    client = TestClient(app)
    client.cookies.set("ecc_session", run_context["token"])
    try:
        yield client
    finally:
        client.close()
        app.dependency_overrides.pop(get_ollama_adapter, None)


def test_get_evaluations_lists_the_seeded_dataset(
    run_context: dict, http_client: TestClient
) -> None:
    response = http_client.get("/api/v1/ai/evaluations")
    assert response.status_code == 200
    body = response.json()
    matching = [item for item in body["evaluation_sets"] if item["task_type"] == TASK_TYPE]
    assert matching == [
        {
            "task_type": TASK_TYPE,
            "version": DATASET_VERSION,
            "classification": "labelled",
            "example_count": 20,
            "status": "active",
        }
    ]


def test_post_evaluations_runs_happy_path(run_context: dict, http_client: TestClient) -> None:
    response = http_client.post(
        "/api/v1/ai/evaluations/runs",
        json={"task_type": TASK_TYPE, "prompt_version": 1, "model_id": _SEEDED_MODEL_ID},
        headers=_headers(run_context["token"], key="eval-run-1"),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["passed"] is True
    assert body["metrics"]["schema_validity_rate"] == 1.0
    assert body["metrics"]["grounding_rate"] == 1.0
    assert body["metrics"]["prohibited_fact_count"] == 0

    get_response = http_client.get(f"/api/v1/ai/evaluations/runs/{body['id']}")
    assert get_response.status_code == 200
    assert get_response.json()["id"] == body["id"]


def test_post_evaluations_runs_unknown_model_is_422(
    run_context: dict, http_client: TestClient
) -> None:
    response = http_client.post(
        "/api/v1/ai/evaluations/runs",
        json={"task_type": TASK_TYPE, "prompt_version": 1, "model_id": "not-registered"},
        headers=_headers(run_context["token"], key="eval-run-bad-model"),
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "model_not_registered"


def test_post_evaluations_runs_requires_csrf(run_context: dict, http_client: TestClient) -> None:
    response = http_client.post(
        "/api/v1/ai/evaluations/runs",
        json={"task_type": TASK_TYPE, "prompt_version": 1, "model_id": _SEEDED_MODEL_ID},
        headers={"Idempotency-Key": "no-csrf"},
    )
    assert response.status_code == 403


def test_post_evaluations_runs_requires_authentication(http_client: TestClient) -> None:
    http_client.cookies.clear()
    response = http_client.post(
        "/api/v1/ai/evaluations/runs",
        json={"task_type": TASK_TYPE, "prompt_version": 1, "model_id": _SEEDED_MODEL_ID},
        headers=_headers("irrelevant", key="no-auth"),
    )
    assert response.status_code == 401


def test_post_evaluations_runs_idempotent_replay_returns_identical_response(
    run_context: dict, http_client: TestClient
) -> None:
    headers = _headers(run_context["token"], key="eval-replay-key")
    first = http_client.post(
        "/api/v1/ai/evaluations/runs",
        json={"task_type": TASK_TYPE, "prompt_version": 1, "model_id": _SEEDED_MODEL_ID},
        headers=headers,
    )
    second = http_client.post(
        "/api/v1/ai/evaluations/runs",
        json={"task_type": TASK_TYPE, "prompt_version": 1, "model_id": _SEEDED_MODEL_ID},
        headers=headers,
    )
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["id"] == second.json()["id"]

    with engine.connect() as connection:
        count = connection.execute(
            text("SELECT count(*) FROM evaluation_runs WHERE workspace_id = :workspace_id"),
            {"workspace_id": run_context["workspace_id"]},
        ).scalar_one()
    assert count == 1


def test_get_evaluation_run_cross_workspace_is_404(
    run_context: dict, http_client: TestClient
) -> None:
    other_workspace = uuid4()
    other_user = uuid4()
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, 'Other Eval Workspace', 'UTC', :created_at)"
            ),
            {"id": other_workspace, "created_at": now},
        )
        connection.execute(
            text(
                "INSERT INTO users (id, workspace_id, email, password_hash, created_at) "
                "VALUES (:id, :workspace_id, :email, 'hash', :created_at)"
            ),
            {
                "id": other_user,
                "workspace_id": other_workspace,
                "email": f"{other_user}@example.test",
                "created_at": now,
            },
        )
    try:
        adapter = _adapter_with_responses(*_flat_responses())
        with SessionFactory() as session:
            run = run_evaluation(
                TASK_TYPE,
                1,
                _SEEDED_MODEL_ID,
                session=session,
                auth=AuthContext(workspace_id=other_workspace, user_id=other_user, timezone="UTC"),
                ollama_adapter=adapter,
            )
        response = http_client.get(f"/api/v1/ai/evaluations/runs/{run.id}")
        assert response.status_code == 404
    finally:
        with engine.begin() as connection:
            for table in (
                "generated_artifacts",
                "evaluation_runs",
                "ai_run_steps",
                "ai_runs",
                "event_outbox",
                "audit_events",
                "users",
            ):
                connection.execute(
                    text(f"DELETE FROM {table} WHERE workspace_id = :workspace_id"),  # noqa: S608
                    {"workspace_id": other_workspace},
                )
            connection.execute(
                text("DELETE FROM workspaces WHERE id = :workspace_id"),
                {"workspace_id": other_workspace},
            )


def test_get_evaluation_run_unknown_id_is_404(run_context: dict, http_client: TestClient) -> None:
    response = http_client.get(f"/api/v1/ai/evaluations/runs/{uuid4()}")
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Section 6: the promotion-floor gate wired into
# POST /ai/policies/{id}/activate for attention.explain_item.v1.
# ---------------------------------------------------------------------------


def test_activate_gated_prompt_rejected_with_no_passing_evaluation(
    run_context: dict, http_client: TestClient
) -> None:
    """No `evaluation_runs` row exists yet in this fresh workspace -- the
    gate must reject even a reactivation of the already-active version 1.
    """
    response = http_client.post(
        f"/api/v1/ai/policies/{_SEEDED_PROMPT_ID}/activate",
        json={"version": 1},
        headers=_headers(run_context["token"], key="gate-reject-1"),
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "EVALUATION_FLOORS_NOT_MET"


def test_activate_gated_prompt_rejected_when_evaluation_exists_but_failed(
    run_context: dict, http_client: TestClient
) -> None:
    """An `evaluation_runs` row existing is not sufficient on its own --
    the gate must also consult its actual `passed` verdict, not just its
    presence. Without this test, a broken `_prompt_evaluation_floor_met`
    that returns `True` for *any* existing row (silently dropping the
    `check_promotion_floors` call) would pass both of this gate's other
    two tests -- confirmed by an adversarial review of an earlier version
    of this test suite, which found exactly that gap.
    """
    bad_key = EXAMPLES[3]["key"]
    failing_adapter = _adapter_with_responses(*_flat_responses(bad_citation_key=bad_key))
    app.dependency_overrides[get_ollama_adapter] = lambda: failing_adapter
    try:
        eval_response = http_client.post(
            "/api/v1/ai/evaluations/runs",
            json={"task_type": TASK_TYPE, "prompt_version": 1, "model_id": _SEEDED_MODEL_ID},
            headers=_headers(run_context["token"], key="gate-fail-eval"),
        )
    finally:
        app.dependency_overrides[get_ollama_adapter] = lambda: _adapter_with_responses(
            *_flat_responses()
        )
    assert eval_response.status_code == 200
    assert eval_response.json()["passed"] is False

    activate_response = http_client.post(
        f"/api/v1/ai/policies/{_SEEDED_PROMPT_ID}/activate",
        json={"version": 1},
        headers=_headers(run_context["token"], key="gate-fail-activate"),
    )
    assert activate_response.status_code == 409
    assert activate_response.json()["error"]["code"] == "EVALUATION_FLOORS_NOT_MET"


def test_activate_gated_prompt_allowed_once_evaluation_passes(
    run_context: dict, http_client: TestClient
) -> None:
    eval_response = http_client.post(
        "/api/v1/ai/evaluations/runs",
        json={"task_type": TASK_TYPE, "prompt_version": 1, "model_id": _SEEDED_MODEL_ID},
        headers=_headers(run_context["token"], key="gate-pass-eval"),
    )
    assert eval_response.status_code == 200
    assert eval_response.json()["passed"] is True

    activate_response = http_client.post(
        f"/api/v1/ai/policies/{_SEEDED_PROMPT_ID}/activate",
        json={"version": 1},
        headers=_headers(run_context["token"], key="gate-pass-activate"),
    )
    assert activate_response.status_code == 200
    assert activate_response.json()["active_version"] == 1


def test_activate_tool_definition_is_unaffected_by_the_prompt_gate(
    run_context: dict, http_client: TestClient
) -> None:
    """`activate_tool_version`'s path never consults evaluation_runs at
    all -- confirmed by activating the already-active `attention.get_item`
    tool with zero evaluation_runs rows present in this workspace.
    """
    response = http_client.post(
        "/api/v1/ai/policies/attention.get_item/activate",
        json={"version": 1},
        headers=_headers(run_context["token"], key="gate-tool-unaffected"),
    )
    assert response.status_code == 200
    assert response.json()["kind"] == "tool"
