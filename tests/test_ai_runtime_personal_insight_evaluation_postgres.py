"""`personal.generate_insight`'s evaluation harness (Phase 7 Task 5 part 2)
-- the third task type Phase 4's evaluation harness (`ecc.domains.ai_runtime.
evaluation`) now evaluates, mirroring `test_ai_runtime_meeting_prep_
evaluation_postgres.py`'s coverage shape but scoped to what's genuinely new
here rather than re-testing task-type-agnostic logic (`check_promotion_
floors`, `EvaluationConfigError`'s config-assertion paths, the idempotent-
replay HTTP behavior) already proven generic in `test_ai_runtime_evaluation_
postgres.py`.

Covers:
1. Migration `0058_phase7_insight_eval.py`'s seeded `evaluation_
   sets` version 1 row -- 10 examples, matching `tests/fixtures/
   phase7_evaluation_personal_insight.py` exactly.
2. `run_evaluation` end to end (mocked Ollama transport, no live model) over
   the real 10-example dataset: a fully grounded run passes every floor; an
   ungrounded citation fails only the grounding floor; a `must_not_state`
   violation fails only the prohibited-fact floor; a `high_stakes`-sourced
   example whose response omits `professional_referral_note` fails only the
   grounding floor (`check_personal_insight_grounding`'s second, conditional
   check).
3. Ephemeral synthetic `personal_domains`/`cross_domain_grants`/`domain_
   records` rows are cleaned up after a run, and reusing the same
   `domain_key` across more than one example in the same run (this
   dataset's own `habits` domain, used by four examples) does not collide
   on `personal_domains`' `UNIQUE(workspace_id, owner_id, domain_key)`.
4. `POST /ai/evaluations/runs` accepts `task_type="personal.generate_
   insight"` (the widened `EvaluationRunCreateRequest.task_type` Literal).
5. The promotion-floor gate now also covers `personal.generate_insight.v1`
   (`prompts.py`'s `_GATED_PROMPT_IDS`).

Unlike `meeting.prep_summary`'s dataset, this one's real `domain_records`
ids are random `uuid4()`s assigned at insertion time, not deterministic
salted ids the test can precompute offline (see the fixture module's own
docstring for why) -- the mocked adapter below extracts the real ids to
cite directly out of each request's own rendered prompt text (`id="..."`
occurrences), rather than precomputing a per-example citation list ahead
of time the way `test_ai_runtime_meeting_prep_evaluation_postgres.py`'s
`_all_ids` does.
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
from fixtures.phase7_evaluation_personal_insight import DATASET_VERSION, EXAMPLES, TASK_TYPE
from sqlalchemy import text

from ecc.auth import AuthContext
from ecc.config import get_settings
from ecc.database import SessionFactory, engine
from ecc.domains.ai_runtime.evaluation import (
    _delete_synthetic_personal_insight_sources as delete_synthetic_personal_insight_sources,
)
from ecc.domains.ai_runtime.evaluation import (
    _insert_synthetic_personal_insight_sources as insert_synthetic_personal_insight_sources,
)
from ecc.domains.ai_runtime.evaluation import (
    check_promotion_floors,
    get_evaluation_run,
    run_evaluation,
)
from ecc.domains.ai_runtime.ollama_client import OllamaAdapter
from ecc.domains.ai_runtime.runtime import get_ollama_adapter, reset_circuit_breakers
from ecc.domains.personal.domains import classification_for
from ecc.main import app

settings = get_settings()
pytestmark = pytest.mark.skipif(
    not settings.database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)

_SEEDED_MODEL_ID = "qwen2.5:1.5b-instruct-q4_K_M"
_SEEDED_PROMPT_ID = "personal.generate_insight.v1"

_ID_PATTERN = re.compile(r'id="([0-9a-fA-F-]{36})"')


def _extract_cited_ids(prompt_text: str) -> list[str]:
    return _ID_PATTERN.findall(prompt_text)


def _example_is_high_stakes(example: dict) -> bool:
    return any(
        classification_for(source["domain_key"]) == "high_stakes" for source in example["sources"]
    )


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
                "VALUES (:id, 'Personal Insight Evaluation Test', 'UTC', :created_at)"
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
            "personal_insight_feedback",
            "personal_insights",
            "domain_records",
            "cross_domain_grants",
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
    missing_referral_key: str | None = None,
    captured_prompts: list[str] | None = None,
) -> OllamaAdapter:
    """Builds a response for each call from that call's own rendered prompt
    (extracting real cited ids via `_extract_cited_ids`) rather than a
    precomputed per-example list -- see this module's own docstring for why.
    Calls are assumed to arrive in `EXAMPLES` order, matching `run_
    evaluation`'s own sequential, one-call-per-example loop.
    """
    call_index = {"value": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        prompt = json.loads(request.content)["prompt"]
        if captured_prompts is not None:
            captured_prompts.append(prompt)
        example = EXAMPLES[call_index["value"]]
        call_index["value"] += 1

        cited = _extract_cited_ids(prompt)
        if bad_citation_id is not None and example["key"] == bad_citation_key:
            cited.append(bad_citation_id)

        explanation = example["reference_explanation"]
        if (
            prohibited_key is not None
            and example["key"] == prohibited_key
            and example["must_not_state"]
        ):
            explanation = f"{explanation} Specifically, {example['must_not_state'][0]}."

        referral_note = None
        if _example_is_high_stakes(example) and example["key"] != missing_referral_key:
            referral_note = "Consider discussing this pattern with a qualified professional."

        payload = {
            "kind": "correlation" if len(example["sources"]) > 1 else "trend",
            "title": example["key"],
            "explanation_text": explanation,
            "cited_record_ids": cited,
            "source_period": "the past few weeks",
            "missing_data": "none noted",
            "confidence": "medium",
            "limitations": (
                "Based only on the records shown here; may not reflect the full picture."
            ),
            "professional_referral_note": referral_note,
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
    bad_key = EXAMPLES[2]["key"]
    bad_citation_id = str(uuid4())
    adapter = _adapter_with_grounded_responses(
        bad_citation_key=bad_key, bad_citation_id=bad_citation_id
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
        if failure["key"] == bad_key and failure["reason"] == "grounding_failed"
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


def test_run_evaluation_missing_professional_referral_note_fails_only_grounding_floor(
    run_context: dict,
) -> None:
    """`check_personal_insight_grounding`'s second, conditional check
    (`validator.py`): a `high_stakes`-sourced example (this dataset has
    five) whose response omits `professional_referral_note` must fail
    grounding -- `INSIGHT-CONTRACT.md`'s "`health`/`finance`-classified
    insights require a non-empty `professional_referral_note` field".
    """
    high_stakes_example = next(example for example in EXAMPLES if _example_is_high_stakes(example))
    adapter = _adapter_with_grounded_responses(missing_referral_key=high_stakes_example["key"])
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
    assert run.metrics.prohibited_fact_count == 0
    assert check_promotion_floors(run) is False
    failure = next(
        failure for failure in run.failures if failure["key"] == high_stakes_example["key"]
    )
    assert failure["reason"] == "grounding_failed"


# ---------------------------------------------------------------------------
# Section 3: synthetic-source cleanup, including no cross-example collision
# on personal_domains' UNIQUE(workspace_id, owner_id, domain_key).
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
        for table in ("personal_domains", "cross_domain_grants", "domain_records"):
            count = connection.execute(
                text(f"SELECT count(*) FROM {table} WHERE workspace_id = :workspace_id"),  # noqa: S608
                {"workspace_id": run_context["workspace_id"]},
            ).scalar_one()
            assert count == 0, f"synthetic {table} rows must be cleaned up after the run"


def test_synthetic_sources_reusing_domain_key_does_not_collide(run_context: dict) -> None:
    """Four of this dataset's ten examples use `domain_key='habits'` -- if
    `_delete_synthetic_personal_insight_sources` did not run between
    examples (or ran but left the `personal_domains` row behind), a later
    example reusing the same domain_key would raise `IntegrityError` on
    `personal_domains`' `UNIQUE(workspace_id, owner_id, domain_key)`.
    Exercises the two helpers directly, proving *they* are correct when
    sequenced insert-then-delete-before-the-next-insert, the same
    assurance `test_ai_runtime_meeting_prep_evaluation_postgres.py`'s
    `test_synthetic_meeting_risks_do_not_leak_into_a_later_examples_pack`
    gives for its own per-example cleanup schedule.
    """
    auth = run_context["auth"]
    example_a = next(example for example in EXAMPLES if example["key"] == "habits_streak_trend")
    example_b = next(
        example for example in EXAMPLES if example["key"] == "sparse_single_domain_trend"
    )
    assert example_a["sources"][0]["domain_key"] == "habits"
    assert example_b["sources"][0]["domain_key"] == "habits"
    now = datetime.now(UTC)

    with SessionFactory() as session:
        domain_ids_a, grant_ids_a, record_ids_a = insert_synthetic_personal_insight_sources(
            session, auth, example_a, now=now
        )
        delete_synthetic_personal_insight_sources(
            session, auth, domain_ids_a, grant_ids_a, record_ids_a
        )

        domain_ids_b, grant_ids_b, record_ids_b = insert_synthetic_personal_insight_sources(
            session, auth, example_b, now=now
        )
        try:
            assert len(domain_ids_b) == 1
        finally:
            delete_synthetic_personal_insight_sources(
                session, auth, domain_ids_b, grant_ids_b, record_ids_b
            )


# ---------------------------------------------------------------------------
# Section 4: POST /ai/evaluations/runs accepts personal.generate_insight.
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


def test_post_evaluations_runs_personal_generate_insight_happy_path(
    run_context: dict, http_client: TestClient
) -> None:
    response = http_client.post(
        "/api/v1/ai/evaluations/runs",
        headers=_headers(run_context["token"], "personal-insight-eval-happy-path"),
        json={
            "task_type": "personal.generate_insight",
            "prompt_version": 1,
            "model_id": _SEEDED_MODEL_ID,
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["task_type"] == "personal.generate_insight"
    assert body["passed"] is True
    assert body["metrics"]["total_examples"] == 10


# ---------------------------------------------------------------------------
# Section 5: the promotion-floor gate now also covers personal.generate_
# insight.v1.
# ---------------------------------------------------------------------------


def test_activate_personal_generate_insight_prompt_rejected_with_no_passing_evaluation(
    run_context: dict, http_client: TestClient
) -> None:
    response = http_client.post(
        f"/api/v1/ai/policies/{_SEEDED_PROMPT_ID}/activate",
        json={"version": 1},
        headers=_headers(run_context["token"], key="gate-personal-insight-no-eval"),
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "EVALUATION_FLOORS_NOT_MET"


def test_activate_personal_generate_insight_prompt_allowed_once_evaluation_passes(
    run_context: dict, http_client: TestClient
) -> None:
    eval_response = http_client.post(
        "/api/v1/ai/evaluations/runs",
        json={"task_type": TASK_TYPE, "prompt_version": 1, "model_id": _SEEDED_MODEL_ID},
        headers=_headers(run_context["token"], key="gate-personal-insight-eval"),
    )
    assert eval_response.status_code == 200, eval_response.text
    assert eval_response.json()["passed"] is True

    activate_response = http_client.post(
        f"/api/v1/ai/policies/{_SEEDED_PROMPT_ID}/activate",
        json={"version": 1},
        headers=_headers(run_context["token"], key="gate-personal-insight-activate"),
    )
    assert activate_response.status_code == 200, activate_response.text
    assert activate_response.json()["active_version"] == 1
