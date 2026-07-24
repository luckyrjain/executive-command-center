"""Phase 4 Task 5 Step 6: the live-Ollama evaluation floor check (design
doc Test strategy section, `ADR-0012`'s Risks).

This is the one test file in this repository's `ai_runtime` suite that
requires a genuine Ollama server producing real tokens from
`qwen2.5:1.5b-instruct-q4_K_M` -- every other Phase 4 test (Tasks 1-5) runs
against a mocked HTTP transport standing in for Ollama's REST API, per the
design doc's Test strategy section: "What genuinely requires a live Ollama
server ... requires a dedicated CI job". `.github/workflows/
ollama-evaluation.yml` provisions the official `ollama/ollama` container
image, pulls the model, and runs this file specifically.

**Skips automatically, rather than failing, whenever no Ollama server is
reachable** at `ECC_OLLAMA_BASE_URL` (default `http://127.0.0.1:11434`) --
which is every environment except that dedicated CI job. This development
sandbox has no outbound network access to `ollama.com` and cannot run the
Ollama server binary at all (confirmed, design doc's Test strategy
section), so this file is *expected* to always skip here. **A skip here is
not evidence the floors pass** -- `EVALUATION-CONTRACT.md`'s Sandbox
constraint section is explicit that these floors are "unverified, not
passing by assumption" until `ollama-evaluation.yml`'s first real run in
actual CI exercises this file end to end against a real model. Do not
report this test as "passing" or "green" from any environment where it was
skipped.
"""

import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from json import dumps
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text

from ecc.auth import AuthContext
from ecc.config import get_settings
from ecc.database import SessionFactory, engine
from ecc.domains.ai_runtime.evaluation import check_promotion_floors, run_evaluation
from ecc.domains.ai_runtime.ollama_client import OllamaAdapter
from ecc.domains.ai_runtime.runtime import execute_run

settings = get_settings()
_OLLAMA_BASE_URL = os.environ.get("ECC_OLLAMA_BASE_URL", "http://127.0.0.1:11434")
_MODEL_ID = "qwen2.5:1.5b-instruct-q4_K_M"
_SECOND_MODEL_ID = "qwen2.5:3b-instruct-q4_K_M"
_TASK_TYPE = "attention.explain_item"
_SMOKE_TEST_ATTEMPTS = 3


def _ollama_reachable() -> bool:
    try:
        response = httpx.get(_OLLAMA_BASE_URL, timeout=2.0)
        return response.status_code == 200
    except httpx.HTTPError:
        return False


pytestmark = [
    pytest.mark.skipif(
        not settings.database_url.startswith("postgresql"),
        reason="PostgreSQL integration test",
    ),
    pytest.mark.skipif(
        not _ollama_reachable(),
        reason=(
            f"no live Ollama server reachable at {_OLLAMA_BASE_URL} -- expected in every "
            "environment except .github/workflows/ollama-evaluation.yml's dedicated CI job "
            "(see this module's docstring: a skip here is not evidence the floors pass)"
        ),
    ),
]


@pytest.fixture
def run_context() -> Iterator[dict]:
    workspace_id = uuid4()
    user_id = uuid4()
    now = datetime.now(UTC)

    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, 'AI Runtime Live Ollama Test', 'UTC', :created_at)"
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

    yield {"auth": AuthContext(workspace_id=workspace_id, user_id=user_id, timezone="UTC")}

    with engine.begin() as connection:
        for table in (
            "generated_artifacts",
            "evaluation_runs",
            "ai_run_steps",
            "ai_runs",
            "event_outbox",
            "audit_events",
            "attention_items",
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


def test_attention_explain_item_passes_every_evaluation_floor_against_real_model(
    run_context: dict,
) -> None:
    """The real acceptance check design doc Decision 9 /
    `EVALUATION-CONTRACT.md` require before any promotion decision is
    trusted: 100% schema validity, 100% grounding, zero prohibited-fact
    occurrences, p95 latency under 20s -- against genuine
    `qwen2.5:1.5b-instruct-q4_K_M` output, not a mocked transport.
    """
    with SessionFactory() as session:
        run = run_evaluation(
            _TASK_TYPE,
            1,
            _MODEL_ID,
            session=session,
            auth=run_context["auth"],
            ollama_adapter=OllamaAdapter(host=_OLLAMA_BASE_URL),
        )

    assert run.metrics.total_examples == 20
    assert check_promotion_floors(run) is True, (
        f"real-model evaluation floors not met: {run.metrics!r}; failures={run.failures!r}"
    )


def test_second_registered_model_produces_a_valid_completed_run_against_real_ollama(
    run_context: dict,
) -> None:
    """Migration `0032_phase4_second_model.py` registered a second real
    candidate, `qwen2.5:3b-instruct-q4_K_M` -- proving it is actually
    invokable end to end (correct tag, produces schema-valid,
    grounded output, not just "present in the registry") requires the
    same real Ollama server this file's other test already needs, so it
    belongs here rather than in the mocked-transport test suite.

    Deliberately a single-item smoke test through `execute_run` directly,
    not the full 20-example `run_evaluation` floor check the first model
    gets -- promoting/evaluating the second model as a routing default is
    a separate decision from confirming it works at all; this test is
    the latter.

    Retries up to `_SMOKE_TEST_ATTEMPTS` times, accepting the first
    `completed` run. `execute_run`'s bounded repair retry (Decision 4/5)
    only covers `schema_invalid` -- a `grounding_failed` outcome (a real
    model citing a factor code absent from the item's real factors, e.g.
    an abbreviated/paraphrased code) is never retried inside a single
    `execute_run` call, by design. The first model's own full 20-example
    evaluation floor check tolerates exactly this kind of small-model
    noise via averaging (it does not require literally every example to
    pass on the first CI run); a single-shot, single-item smoke test has
    no such averaging to fall back on, so it needs its own bounded
    retry to avoid being flakier than the property it is actually
    trying to prove ("this model is invokable and can produce a valid,
    grounded response" -- not "this model never has an off run").

    The first model is temporarily marked `disabled` for this test's
    duration so `route()`'s eligibility pipeline has exactly one
    candidate left -- deterministic, not relying on winning a preference
    tie-break -- and restored in the `finally` block regardless of outcome.
    """
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE model_definitions SET status = 'disabled' "
                "WHERE provider = 'ollama' AND model_id = :model_id"
            ),
            {"model_id": _MODEL_ID},
        )
    try:
        item_id = uuid4()
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
                        :id, :workspace_id, 'task', :entity_id, 1, 62, 0.900,
                        CAST(:factors AS jsonb), 'because reasons', :now, :expires_at, false, 1
                    )
                    """
                ),
                {
                    "id": item_id,
                    "workspace_id": run_context["auth"].workspace_id,
                    "entity_id": uuid4(),
                    "factors": dumps(
                        [
                            {
                                "code": "overdue",
                                "label": "Due timing overdue",
                                "points": 25,
                                "source_field": "due_date,due_at",
                            }
                        ]
                    ),
                    "now": now,
                    "expires_at": now + timedelta(days=1),
                },
            )

        runs = []
        for _attempt in range(_SMOKE_TEST_ATTEMPTS):
            with SessionFactory() as session:
                run = execute_run(
                    _TASK_TYPE,
                    "sensitive",
                    {"attention_item_id": str(item_id)},
                    session=session,
                    auth=run_context["auth"],
                    ollama_adapter=OllamaAdapter(host=_OLLAMA_BASE_URL),
                )
            runs.append(run)
            if run.status == "completed":
                break

        assert run.status == "completed", (
            f"model never produced a completed run in {_SMOKE_TEST_ATTEMPTS} attempts: "
            f"error_codes={[r.error_code for r in runs]!r}"
        )
        assert run.model_id == _SECOND_MODEL_ID
        assert run.output is not None
        # A "completed" run already implies grounding passed (execute_run's
        # own check_explain_item_grounding gate) -- every cited code must
        # be a subset of the item's one real factor. Not asserting exact
        # equality: a real model may legitimately cite it, cite it plus
        # nothing else, or cite nothing at all (grounding is vacuously true
        # for an empty citation list) -- all are valid completed outcomes.
        assert set(run.output["cited_factor_codes"]) <= {"overdue"}
    finally:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE model_definitions SET status = 'active' "
                    "WHERE provider = 'ollama' AND model_id = :model_id"
                ),
                {"model_id": _MODEL_ID},
            )
