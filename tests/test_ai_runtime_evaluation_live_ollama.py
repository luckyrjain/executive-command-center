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
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text

from ecc.auth import AuthContext
from ecc.config import get_settings
from ecc.database import SessionFactory, engine
from ecc.domains.ai_runtime.evaluation import check_promotion_floors, run_evaluation
from ecc.domains.ai_runtime.ollama_client import OllamaAdapter

settings = get_settings()
_OLLAMA_BASE_URL = os.environ.get("ECC_OLLAMA_BASE_URL", "http://127.0.0.1:11434")
_MODEL_ID = "qwen2.5:1.5b-instruct-q4_K_M"
_TASK_TYPE = "attention.explain_item"


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
