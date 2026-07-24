"""`meeting.prep_summary`'s evaluation harness -- the second task type
Phase 4's evaluation harness (`ecc.domains.ai_runtime.evaluation`) now
evaluates, mirroring `test_ai_runtime_evaluation_postgres.py`'s coverage of
`attention.explain_item` but scoped to what's genuinely new here rather than
re-testing task-type-agnostic logic (`check_promotion_floors`,
`EvaluationConfigError`'s unknown-task-type/inactive-prompt-version/
unregistered-model paths, the idempotent-replay HTTP behavior) already
proven generic there.

Covers:
1. Migration `0035_phase4_meeting_eval.py`'s seeded `evaluation_sets`
   version 1 row -- 10 examples, matching `tests/fixtures/
   phase4_evaluation_meeting_prep.py` exactly.
2. `run_evaluation` end to end (mocked Ollama transport, no live model) over
   the real 10-example dataset: a fully grounded run passes every floor; an
   ungrounded citation fails only the grounding floor; a `must_not_state`
   violation fails only the prohibited-fact floor.
3. Ephemeral synthetic meeting evidence bundles (`meetings`/`pkos_nodes`/
   `meeting_participants`/`timeline_entries`/`commitments`/`notes`/`risks`/
   `waiting_links`) are cleaned up after a run -- and, the property that
   actually motivated per-example (not batched) cleanup: one example's
   workspace-wide `risks` never leak into a later example's pack.
4. `POST /ai/evaluations/runs` accepts `task_type="meeting.prep_summary"`
   (the widened `EvaluationRunCreateRequest.task_type` Literal) end to end.
5. The promotion-floor gate now also covers `meeting.prep_summary.v1`
   (`prompts.py`'s `_GATED_PROMPT_IDS`).
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
from fixtures.phase4_evaluation_meeting_prep import DATASET_VERSION, EXAMPLES, TASK_TYPE
from sqlalchemy import text

from ecc.auth import AuthContext
from ecc.config import get_settings
from ecc.database import SessionFactory, engine
from ecc.domains.ai_runtime.evaluation import (
    _delete_synthetic_meeting as delete_synthetic_meeting,
)
from ecc.domains.ai_runtime.evaluation import (
    _insert_synthetic_meeting as insert_synthetic_meeting,
)
from ecc.domains.ai_runtime.evaluation import (
    check_promotion_floors,
    get_evaluation_run,
    run_evaluation,
)
from ecc.domains.ai_runtime.ollama_client import OllamaAdapter
from ecc.domains.ai_runtime.runtime import get_ollama_adapter, reset_circuit_breakers
from ecc.domains.attention.meeting_prep_tools import get_prep_pack_tool
from ecc.main import app

settings = get_settings()
pytestmark = pytest.mark.skipif(
    not settings.database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)

_SEEDED_MODEL_ID = "qwen2.5:1.5b-instruct-q4_K_M"
_SEEDED_PROMPT_ID = "meeting.prep_summary.v1"

_SECTIONS = (
    "participants",
    "timeline",
    "commitments",
    "decisions",
    "notes",
    "risks",
    "dependencies",
)


def _all_ids(example: dict) -> list[str]:
    return [row["id"] for section in _SECTIONS for row in example[section]]


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
                "VALUES (:id, 'Meeting Prep Evaluation Test', 'UTC', :created_at)"
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
            "meeting_participants",
            "timeline_entries",
            "commitments",
            "notes",
            "risks",
            "waiting_links",
            "pkos_nodes",
            "meetings",
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
    """`test_ai_runtime_evaluation_postgres.py`'s identical helper -- see
    that module's copy for the full rationale.
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
    summary = example["reference_summary"]
    if prohibited and example["must_not_state"]:
        summary = f"{summary} Specifically, {example['must_not_state'][0]}."
    cited = _all_ids(example)
    if extra_citation is not None:
        cited.append(extra_citation)
    return json.dumps({"summary_text": summary, "cited_evidence_ids": cited})


def _flat_responses(
    *, bad_citation_key: str | None = None, prohibited_key: str | None = None
) -> list[str]:
    """One flat, ordered response list spanning every example in
    `EXAMPLES`, matching `run_evaluation`'s call order (dataset order, one
    `.generate()` call per example -- none of these examples are
    deliberately schema-invalid, so no repair-retry consumption to account
    for, unlike `test_ai_runtime_evaluation_postgres.py`'s equivalent).
    """
    return [
        _valid_response(
            example,
            extra_citation=str(uuid4()) if example["key"] == bad_citation_key else None,
            prohibited=example["key"] == prohibited_key,
        )
        for example in EXAMPLES
    ]


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

    assert run.metrics.total_examples == 10
    assert run.metrics.schema_validity_rate == 1.0
    assert run.metrics.grounding_rate == 1.0
    assert run.metrics.prohibited_fact_count == 0
    assert run.metrics.latency_p95_seconds < 20.0
    assert run.failures == []
    assert check_promotion_floors(run) is True

    persisted = get_evaluation_run(session, run_context["auth"], run.id)
    assert persisted is not None
    assert persisted.metrics.schema_validity_rate == 1.0


def test_run_evaluation_ungrounded_citation_fails_only_grounding_floor(run_context: dict) -> None:
    bad_key = EXAMPLES[2]["key"]
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
    assert run.metrics.grounding_rate == pytest.approx(9 / 10)
    assert run.metrics.prohibited_fact_count == 0
    assert check_promotion_floors(run) is False
    assert any(
        failure["key"] == bad_key and failure["reason"] == "grounding_failed"
        for failure in run.failures
    )


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


# ---------------------------------------------------------------------------
# Section 3: synthetic-meeting cleanup, including no cross-example leakage.
# ---------------------------------------------------------------------------


def test_run_evaluation_cleans_up_every_synthetic_table(run_context: dict) -> None:
    adapter = _adapter_with_responses(*_flat_responses())
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
        for table in (
            "meetings",
            "pkos_nodes",
            "meeting_participants",
            "timeline_entries",
            "commitments",
            "notes",
            "risks",
            "waiting_links",
        ):
            count = connection.execute(
                text(f"SELECT count(*) FROM {table} WHERE workspace_id = :workspace_id"),  # noqa: S608
                {"workspace_id": run_context["workspace_id"]},
            ).scalar_one()
            assert count == 0, f"synthetic {table} rows must be cleaned up after the run"


def test_synthetic_meeting_risks_do_not_leak_into_a_later_examples_pack(
    run_context: dict,
) -> None:
    """The exact property `_insert_synthetic_meeting`/`_delete_synthetic_
    meeting`'s per-example (not batched) cleanup exists to guarantee:
    `risks` are workspace-wide, not meeting-scoped, so if example A's risks
    were still present when example B's pack is generated, B's `meeting.
    get_prep_pack` output would include a risk B's own fixture never
    listed. Exercises the two helpers directly, in the same insert-then-
    delete-before-the-next-insert sequence `run_evaluation`'s loop uses,
    rather than only asserting the end state (Section 2's cleanup test) --
    that end state alone can't distinguish "never leaked" from "leaked,
    then correctly cleaned up eventually".
    """
    auth = run_context["auth"]
    example_with_risk = next(example for example in EXAMPLES if example["risks"])
    example_without_risk = next(example for example in EXAMPLES if not example["risks"])
    now = datetime.now(UTC)

    with SessionFactory() as session:
        meeting_a, nodes_a, risks_a = insert_synthetic_meeting(
            session, auth, example_with_risk, now=now
        )
        assert len(risks_a) >= 1
        delete_synthetic_meeting(session, auth, meeting_a, nodes_a, risks_a)

        meeting_b, nodes_b, risks_b = insert_synthetic_meeting(
            session, auth, example_without_risk, now=now
        )
        try:
            result = get_prep_pack_tool(session, auth, meeting_b)
            assert not isinstance(result, type(None))
            risk_ids_in_pack = {r["id"] for r in result.output["risks"]}  # type: ignore[union-attr]
            assert risk_ids_in_pack.isdisjoint({str(r) for r in risks_a})
            assert risk_ids_in_pack == set()
        finally:
            delete_synthetic_meeting(session, auth, meeting_b, nodes_b, risks_b)


# ---------------------------------------------------------------------------
# Section 4: POST /ai/evaluations/runs accepts meeting.prep_summary.
# ---------------------------------------------------------------------------


def _headers(token: str, key: str | None = None) -> dict[str, str]:
    csrf = new(settings.session_secret.encode(), token.encode(), "sha256").hexdigest()
    headers = {"X-CSRF-Token": csrf, "X-Correlation-ID": str(uuid4())}
    if key is not None:
        headers["Idempotency-Key"] = key
    return headers


@pytest.fixture
def http_client(run_context: dict) -> Iterator[TestClient]:
    """A fully grounded mocked adapter by default (`test_ai_runtime_
    evaluation_postgres.py`'s identical fixture) -- tests that need a
    different transport override `app.dependency_overrides[get_ollama_
    adapter]` themselves and restore it before this fixture tears down.
    """
    adapter = _adapter_with_responses(*_flat_responses())
    app.dependency_overrides[get_ollama_adapter] = lambda: adapter
    client = TestClient(app)
    client.cookies.set("ecc_session", run_context["token"])
    try:
        yield client
    finally:
        client.close()
        app.dependency_overrides.pop(get_ollama_adapter, None)


def test_post_evaluations_runs_meeting_prep_summary_happy_path(
    run_context: dict, http_client: TestClient
) -> None:
    response = http_client.post(
        "/api/v1/ai/evaluations/runs",
        headers=_headers(run_context["token"], "meeting-eval-happy-path"),
        json={
            "task_type": "meeting.prep_summary",
            "prompt_version": 1,
            "model_id": _SEEDED_MODEL_ID,
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["task_type"] == "meeting.prep_summary"
    assert body["passed"] is True
    assert body["metrics"]["total_examples"] == 10


# ---------------------------------------------------------------------------
# Section 5: the promotion-floor gate now also covers meeting.prep_summary.v1.
# ---------------------------------------------------------------------------


def test_activate_meeting_prep_summary_prompt_rejected_with_no_passing_evaluation(
    run_context: dict, http_client: TestClient
) -> None:
    """No `evaluation_runs` row exists yet in this fresh workspace -- the
    gate must reject even a reactivation of the already-active version 1,
    mirroring `test_ai_runtime_evaluation_postgres.py`'s identical test for
    `attention.explain_item.v1`.
    """
    response = http_client.post(
        f"/api/v1/ai/policies/{_SEEDED_PROMPT_ID}/activate",
        json={"version": 1},
        headers=_headers(run_context["token"], key="gate-meeting-prep-no-eval"),
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "EVALUATION_FLOORS_NOT_MET"


def test_activate_meeting_prep_summary_prompt_allowed_once_evaluation_passes(
    run_context: dict, http_client: TestClient
) -> None:
    eval_response = http_client.post(
        "/api/v1/ai/evaluations/runs",
        json={"task_type": TASK_TYPE, "prompt_version": 1, "model_id": _SEEDED_MODEL_ID},
        headers=_headers(run_context["token"], key="gate-meeting-prep-eval"),
    )
    assert eval_response.status_code == 200, eval_response.text
    assert eval_response.json()["passed"] is True

    activate_response = http_client.post(
        f"/api/v1/ai/policies/{_SEEDED_PROMPT_ID}/activate",
        json={"version": 1},
        headers=_headers(run_context["token"], key="gate-meeting-prep-activate"),
    )
    assert activate_response.status_code == 200, activate_response.text
    assert activate_response.json()["active_version"] == 1
