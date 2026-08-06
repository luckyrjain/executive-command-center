"""`email.detect_action`'s evaluation harness (Phase 10 Task 5) -- the
fourth task type Phase 4's evaluation harness (`ecc.domains.ai_runtime.
evaluation`) now evaluates, mirroring `test_ai_runtime_personal_insight_
evaluation_postgres.py`'s coverage shape but scoped to what's genuinely
new here rather than re-testing task-type-agnostic logic (`check_
promotion_floors`, `EvaluationConfigError`'s config-assertion paths, the
idempotent-replay HTTP behavior) already proven generic in `test_ai_
runtime_evaluation_postgres.py`.

Covers:
1. The active `evaluation_sets` row for this task type -- 10 examples,
   matching `tests/fixtures/phase10_evaluation_email_detect_action.py`
   exactly.
2. `run_evaluation` end to end (mocked Ollama transport, no live model)
   over the real 10-example dataset: a fully grounded run passes every
   floor (including the six negative, `has_action: false` examples, whose
   trivially-empty citation list must not itself fail grounding); an
   ungrounded citation on a positive example fails only the grounding
   floor; a `must_not_state` violation fails only the prohibited-fact
   floor; a positive example whose mocked response claims `has_action:
   true` with an empty `cited_message_ids` fails schema validity (not
   grounding) -- `validator.py:EmailDetectActionOutput._validate_action_
   shape`'s own conditional-shape enforcement, not a post-schema check.
3. Ephemeral synthetic `connector_accounts`/`email_threads`/`email_
   messages` rows are cleaned up after a run.
4. `POST /ai/evaluations/runs` accepts `task_type="email.detect_action"`
   (the widened `EvaluationRunCreateRequest.task_type` Literal).
5. The promotion-floor gate now also covers `email.detect_action.v1`
   (`prompts.py`'s `_GATED_PROMPT_IDS`).

Real `email_messages` ids are random `uuid4()`s assigned at insertion
time, not deterministic -- the mocked adapter below extracts the real ids
to cite directly out of each request's own rendered prompt text
(`id="..."` occurrences), the same technique `test_ai_runtime_personal_
insight_evaluation_postgres.py`'s own mocked adapter uses for its
`domain_records` ids.
"""

import json
import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from hmac import new
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from fixtures.phase10_evaluation_email_detect_action import DATASET_VERSION, EXAMPLES, TASK_TYPE
from identity_fixtures import create_identity
from sqlalchemy import text

from ecc.auth import AuthContext
from ecc.config import get_settings
from ecc.database import SessionFactory, engine
from ecc.domains.ai_runtime.evaluation import (
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
_SEEDED_PROMPT_ID = "email.detect_action.v1"

_ID_PATTERN = re.compile(r'id="([0-9a-fA-F-]{36})"')


def _extract_cited_ids(prompt_text: str) -> list[str]:
    return _ID_PATTERN.findall(prompt_text)


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
                "VALUES (:id, 'Email Detect Action Evaluation Test', 'UTC', :created_at)"
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
            "recommendations",
            "email_messages",
            "email_threads",
            "connector_accounts",
            "personal_domains",
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


def _adapter_with_grounded_responses(
    *,
    bad_citation_key: str | None = None,
    bad_citation_id: str | None = None,
    prohibited_key: str | None = None,
    missing_citation_key: str | None = None,
    captured_prompts: list[str] | None = None,
) -> OllamaAdapter:
    """Builds a response for each call from that call's own rendered
    prompt (extracting real cited ids via `_extract_cited_ids`) rather
    than a precomputed per-example list -- see this module's own
    docstring for why. Calls are assumed to arrive in `EXAMPLES` order,
    matching `run_evaluation`'s own sequential, one-call-per-example loop.
    """
    call_index = {"value": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        prompt = json.loads(request.content)["prompt"]
        if captured_prompts is not None:
            captured_prompts.append(prompt)
        # A schema-invalid first response triggers `validate_with_bounded_
        # repair`'s one bounded retry (`runtime.py:reattempt`), which
        # re-sends this same example's prompt with the repair instruction
        # appended -- detected here by that instruction's own distinctive
        # opening text, so the retry re-scores the *same* example (not the
        # next one in `EXAMPLES`) and this handler's own `call_index`
        # tracking stays in sync with `run_evaluation`'s one-call-per-
        # example loop across the retry.
        is_repair_retry = "Your previous output did not match the required schema" in prompt
        if is_repair_retry:
            example = EXAMPLES[call_index["value"] - 1]
        else:
            example = EXAMPLES[call_index["value"]]
            call_index["value"] += 1

        has_action = example["reference_has_action"]
        rationale = f"Reviewed the thread for {example['key']}."
        if (
            prohibited_key is not None
            and example["key"] == prohibited_key
            and example["must_not_state"]
        ):
            rationale = f"{rationale} Specifically, {example['must_not_state'][0]}."

        if not has_action:
            payload = {
                "has_action": False,
                "target_type": None,
                "operation": None,
                "proposed_fields": None,
                "rationale": rationale,
                "confidence": 0.9,
                "cited_message_ids": [],
            }
        else:
            cited = _extract_cited_ids(prompt)
            if bad_citation_id is not None and example["key"] == bad_citation_key:
                cited.append(bad_citation_id)
            if missing_citation_key is not None and example["key"] == missing_citation_key:
                cited = []
            payload = {
                "has_action": True,
                "target_type": "task",
                "operation": "create",
                "proposed_fields": {
                    "title": example["key"],
                    "description": rationale,
                },
                "rationale": rationale,
                "confidence": 0.8,
                "cited_message_ids": cited,
            }

        body = (
            json.dumps(
                {
                    "model": "m",
                    "created_at": "now",
                    "response": json.dumps(payload),
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


# ---------------------------------------------------------------------------
# Section 1: the seeded evaluation_sets row.
# ---------------------------------------------------------------------------


def test_seeded_evaluation_set_matches_the_checked_in_fixture() -> None:
    with SessionFactory() as session:
        row = (
            session.execute(
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
    assert row["example_count"] == len(EXAMPLES) == 10
    assert {example["key"] for example in row["examples"]} == {e["key"] for e in EXAMPLES}


# ---------------------------------------------------------------------------
# Section 2: run_evaluation end to end, over the real 10-example dataset.
# ---------------------------------------------------------------------------


def test_run_evaluation_fully_grounded_run_passes_every_floor(run_context: dict) -> None:
    adapter = _adapter_with_grounded_responses()
    with SessionFactory() as session:
        run = run_evaluation(
            TASK_TYPE,
            1,
            _SEEDED_MODEL_ID,
            session=session,
            auth=run_context["auth"],
            ollama_adapter=adapter,
        )

    assert run.metrics.total_examples == 10
    assert run.metrics.schema_validity_rate == 1.0
    assert run.metrics.grounding_rate == 1.0
    assert run.metrics.prohibited_fact_count == 0
    assert run.failures == []
    assert check_promotion_floors(run) is True

    persisted = get_evaluation_run(session, run_context["auth"], run.id)
    assert persisted is not None
    assert persisted.metrics.schema_validity_rate == 1.0


def test_run_evaluation_ungrounded_citation_fails_only_grounding_floor(run_context: dict) -> None:
    positive_example = next(example for example in EXAMPLES if example["reference_has_action"])
    bad_citation_id = str(uuid4())
    adapter = _adapter_with_grounded_responses(
        bad_citation_key=positive_example["key"], bad_citation_id=bad_citation_id
    )
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
    assert run.metrics.grounding_rate == pytest.approx(9 / 10)
    assert run.metrics.prohibited_fact_count == 0
    assert check_promotion_floors(run) is False
    grounding_failure = next(
        failure
        for failure in run.failures
        if failure["key"] == positive_example["key"] and failure["reason"] == "grounding_failed"
    )
    assert grounding_failure["ungrounded_codes"] == [bad_citation_id]


def test_run_evaluation_prohibited_fact_fails_only_that_floor(run_context: dict) -> None:
    example_with_probe = next(example for example in EXAMPLES if example["must_not_state"])
    adapter = _adapter_with_grounded_responses(prohibited_key=example_with_probe["key"])
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


def test_run_evaluation_has_action_true_with_no_citations_fails_schema_validity(
    run_context: dict,
) -> None:
    """`EmailDetectActionOutput._validate_action_shape` (`validator.py`)
    requires `cited_message_ids` non-empty whenever `has_action` is true
    -- a schema-level (not grounding-level) requirement, since an
    unsupported claim citing nothing would otherwise trivially pass
    `check_email_detect_action_grounding` (whose `ungrounded` set is
    computed by intersecting an *empty* cited list against the valid
    ids). This must surface as `schema_invalid`, not `grounding_failed`.
    """
    positive_example = next(example for example in EXAMPLES if example["reference_has_action"])
    adapter = _adapter_with_grounded_responses(missing_citation_key=positive_example["key"])
    with SessionFactory() as session:
        run = run_evaluation(
            TASK_TYPE,
            1,
            _SEEDED_MODEL_ID,
            session=session,
            auth=run_context["auth"],
            ollama_adapter=adapter,
        )

    assert run.metrics.schema_validity_rate == pytest.approx(9 / 10)
    assert check_promotion_floors(run) is False
    failure = next(failure for failure in run.failures if failure["key"] == positive_example["key"])
    assert failure["reason"] == "schema_invalid"


# ---------------------------------------------------------------------------
# Section 3: synthetic-source cleanup.
# ---------------------------------------------------------------------------


def test_run_evaluation_cleans_up_every_synthetic_table(run_context: dict) -> None:
    adapter = _adapter_with_grounded_responses()
    with SessionFactory() as session:
        run_evaluation(
            TASK_TYPE,
            1,
            _SEEDED_MODEL_ID,
            session=session,
            auth=run_context["auth"],
            ollama_adapter=adapter,
        )

    with engine.connect() as connection:
        for table in ("connector_accounts", "email_threads", "email_messages", "personal_domains"):
            count = connection.execute(
                text(f"SELECT count(*) FROM {table} WHERE workspace_id = :workspace_id"),  # noqa: S608
                {"workspace_id": run_context["workspace_id"]},
            ).scalar_one()
            assert count == 0, f"synthetic {table} rows must be cleaned up after the run"


# ---------------------------------------------------------------------------
# Section 4: POST /ai/evaluations/runs accepts email.detect_action.
# ---------------------------------------------------------------------------


def _headers(token: str, key: str | None = None) -> dict[str, str]:
    csrf = new(settings.session_secret.encode(), token.encode(), "sha256").hexdigest()
    headers = {"X-CSRF-Token": csrf, "X-Correlation-ID": str(uuid4())}
    if key is not None:
        headers["Idempotency-Key"] = key
    return headers


@pytest.fixture
def http_client(run_context: dict) -> Iterator[TestClient]:
    adapter = _adapter_with_grounded_responses()
    app.dependency_overrides[get_ollama_adapter] = lambda: adapter
    client = TestClient(app)
    client.cookies.set("ecc_session", run_context["token"])
    try:
        yield client
    finally:
        client.close()
        app.dependency_overrides.pop(get_ollama_adapter, None)


def test_post_evaluations_runs_email_detect_action_happy_path(
    run_context: dict, http_client: TestClient
) -> None:
    response = http_client.post(
        "/api/v1/ai/evaluations/runs",
        headers=_headers(run_context["token"], "email-detect-action-eval-happy-path"),
        json={
            "task_type": "email.detect_action",
            "prompt_version": 1,
            "model_id": _SEEDED_MODEL_ID,
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["task_type"] == "email.detect_action"
    assert body["passed"] is True
    assert body["metrics"]["total_examples"] == 10


# ---------------------------------------------------------------------------
# Section 5: the promotion-floor gate now also covers email.detect_action.v1.
# ---------------------------------------------------------------------------


def test_activate_email_detect_action_prompt_rejected_with_no_passing_evaluation(
    run_context: dict, http_client: TestClient
) -> None:
    response = http_client.post(
        f"/api/v1/ai/policies/{_SEEDED_PROMPT_ID}/activate",
        json={"version": 1},
        headers=_headers(run_context["token"], key="gate-email-detect-action-no-eval"),
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "EVALUATION_FLOORS_NOT_MET"


def test_activate_email_detect_action_prompt_allowed_once_evaluation_passes(
    run_context: dict, http_client: TestClient
) -> None:
    eval_response = http_client.post(
        "/api/v1/ai/evaluations/runs",
        json={"task_type": TASK_TYPE, "prompt_version": 1, "model_id": _SEEDED_MODEL_ID},
        headers=_headers(run_context["token"], key="gate-email-detect-action-eval"),
    )
    assert eval_response.status_code == 200, eval_response.text
    assert eval_response.json()["passed"] is True

    activate_response = http_client.post(
        f"/api/v1/ai/policies/{_SEEDED_PROMPT_ID}/activate",
        json={"version": 1},
        headers=_headers(run_context["token"], key="gate-email-detect-action-activate"),
    )
    assert activate_response.status_code == 200, activate_response.text
