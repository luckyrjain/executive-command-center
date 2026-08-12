"""`POST /api/v1/personal/insights/generate` (Phase 7 Task 5 part 2): the
production call site for the `personal.generate_insight` AI task type --
`trend`/`correlation` insights drawn from one or more domains the caller
has already granted (`grants.py`'s `cross_domain_grants`, Task 5 part 1).

Fail-open by construction, mirroring `ecc.domains.attention.meeting_prep.
_compute_enrichment`'s own fail-open discipline exactly: any non-`completed`
run (feature disabled, no active grant for a requested domain, no eligible
model, timeout, budget exceeded, a grounding/safety failure, ...) surfaces
as `available=False` carrying that run's own `error_code` -- this endpoint
never turns an AI failure into an HTTP error for a genuinely well-formed
request. `personal.get_insight_sources` (the task's own Step 1 tool) is
what actually enforces "an active grant covering every source" -- this
endpoint does not duplicate that check, it only threads the caller's
requested `source_domain_keys` through to `execute_run` and reports what
came back.

**Why a dedicated endpoint, not folded into `habits.py:list_insights_
endpoint`.** That endpoint already recomputes every *deterministic*
insight on every `GET` -- cheap, no model call. Doing the same for an
AI-generated insight would mean a real model call on every list-view
fetch, an unacceptable latency/cost regression this activation does not
accept implicitly. Generation is instead an explicit, opt-in action the
caller requests once per desired insight, exactly like `POST /ai/runs`
already is for `attention.explain_item` (Task 4) -- not wired through that
generic endpoint itself, matching `meeting.prep_summary`'s own established
precedent of a second task type invoked directly via `execute_run(...)`
from its own dedicated call site rather than through `AiRunCreateRequest`'s
closed `task: Literal[...]`.

**Why `source_period_start`/`source_period_end` are computed here, not
taken from the model's own output.** `validator.PersonalInsightOutput.
source_period` is a free-text field (e.g. "the past three weeks") --
useful for the model to reason in, but not a value this activation trusts
to populate `personal_insights.source_period_start`/`_end` (`NOT NULL`
`DateTime` columns) without risking a hallucinated date range reaching a
structural field. This module instead computes the real bounds directly
from the `effective_at` of the `domain_records` rows the output actually
cited (`_cited_record_period` below) -- grounded in data this activation
already knows exists, never in what the model merely said.

**Which `domain_key` a cross-domain insight is recorded under.**
`personal_insights.domain_key` is a single column (a real foreign key into
`personal_domains`), but this task type's own insights can span more than
one source domain. This activation records the *first* requested
`source_domain_keys` entry as the row's `domain_key` -- a deliberate
simplification, not a claim that the insight is single-domain; the
genuinely cross-domain evidence is preserved in `evidence` (every cited
record id plus every source domain key), not lost by this column's own
single-value shape.
"""

from datetime import UTC, datetime
from json import dumps
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ecc.auth import AuthContext, AuthDep, CsrfDep
from ecc.config import get_settings
from ecc.domains.ai_runtime.runtime import OllamaAdapterDep, execute_run, held_idempotency_lock

from .domains import (
    DomainKey,
    IdempotencyHeader,
    SessionDep,
    classification_for,
    load_cached,
    request_hash,
    store_idempotency,
)
from .habits import InsightResponse

router = APIRouter(prefix="/api/v1/personal", tags=["personal"])


class InsightGenerateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_domain_keys: list[DomainKey] = Field(min_length=1)


class InsightGenerateResponse(BaseModel):
    available: bool
    insight: InsightResponse | None
    error_code: str | None


def _cited_record_period(
    session: Session, auth: AuthContext, cited_ids: list[str], *, now: datetime
) -> tuple[datetime, datetime]:
    """The real `(min, max)` `effective_at` across the `domain_records` the
    output actually cited -- see this module's own docstring for why this,
    not the model's own free-text `source_period`, populates the DB's
    `source_period_start`/`_end` columns. Falls back to `(now, now)` for an
    empty citation list (schema-valid but unusual: an insight whose
    `explanation_text` cites nothing) rather than raising -- fail-open,
    matching this module's overall discipline.
    """
    if not cited_ids:
        return now, now
    try:
        uuids = [UUID(id_) for id_ in cited_ids]
    except ValueError:
        return now, now
    row = (
        session.execute(
            text(
                "SELECT MIN(effective_at) AS period_start, MAX(effective_at) AS period_end "
                "FROM domain_records WHERE workspace_id = :workspace_id AND owner_id = :owner_id "
                "AND id = ANY(:ids)"
            ),
            {"workspace_id": auth.workspace_id, "owner_id": auth.user_id, "ids": uuids},
        )
        .mappings()
        .one()
    )
    period_start = row["period_start"] or now
    period_end = row["period_end"] or now
    return period_start, period_end


@router.post("/insights/generate", response_model=InsightGenerateResponse)
def generate_insight_endpoint(
    payload: InsightGenerateRequest,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
    idempotency_key: IdempotencyHeader,
    adapter: OllamaAdapterDep,
) -> InsightGenerateResponse:
    """Three-phase shape, `execute_run` called bare between separate
    `with session.begin():` blocks -- exactly `meeting_prep.py:create_
    prep`'s enrichment-enabled path (and `ai_runtime/runtime.py:create_
    run`'s own identical three-phase precedent). Calling `execute_run`
    from *inside* a still-open `session.begin()` block closes that
    transaction out from under the caller the instant `execute_run`'s own
    internal commit runs (`_persist_terminal`'s docstring) -- confirmed by
    a real `sqlalchemy.exc.InvalidRequestError: Can't operate on closed
    transaction` this endpoint's own first draft hit in CI, not a
    hypothetical. `held_idempotency_lock` (session-scoped, not tied to
    any one transaction) is what actually serializes two concurrent
    requests sharing the same Idempotency-Key across the whole body,
    including the model call.
    """
    req_hash = request_hash(payload, "generate_insight")
    now = datetime.now(UTC)

    with held_idempotency_lock(auth, idempotency_key):
        with session.begin():
            cached = load_cached(
                session, auth, idempotency_key, req_hash, domain="personal_insight"
            )
        if cached is not None:
            return InsightGenerateResponse.model_validate(cached)

        if not get_settings().personal_ai_insight_generation_enabled:
            response = InsightGenerateResponse(
                available=False, insight=None, error_code="feature_disabled"
            )
            try:
                with session.begin():
                    store_idempotency(
                        session,
                        auth,
                        idempotency_key,
                        req_hash,
                        response.model_dump(mode="json"),
                        now,
                    )
            except SQLAlchemyError:
                pass
            return response

        # `data_class` (Decision 2's model-eligibility routing input, not a
        # per-record classification): `restricted` whenever any requested
        # source is `high_stakes`, `sensitive` otherwise -- a stricter
        # classification than `_EVALUATION_DATA_CLASS`'s own flat
        # `"sensitive"` (evaluation.py), which never touches a real,
        # potentially `high_stakes` workspace domain the way this
        # production call site can.
        data_class = (
            "restricted"
            if any(
                classification_for(domain_key) == "high_stakes"
                for domain_key in payload.source_domain_keys
            )
            else "sensitive"
        )
        run = execute_run(
            "personal.generate_insight",
            data_class,
            {"source_domain_keys": list(payload.source_domain_keys)},
            session=session,
            auth=auth,
            ollama_adapter=adapter,
        )
        if run.status != "completed" or run.output is None:
            response = InsightGenerateResponse(
                available=False, insight=None, error_code=run.error_code
            )
            try:
                with session.begin():
                    store_idempotency(
                        session,
                        auth,
                        idempotency_key,
                        req_hash,
                        response.model_dump(mode="json"),
                        now,
                    )
            except SQLAlchemyError:
                pass
            return response

        output = run.output
        cited_record_ids: list[str] = list(output.get("cited_record_ids", []))
        with session.begin():
            period_start, period_end = _cited_record_period(
                session, auth, cited_record_ids, now=now
            )
            evidence: dict[str, Any] = {
                "cited_record_ids": cited_record_ids,
                "source_domain_keys": list(payload.source_domain_keys),
                "model_source_period": output.get("source_period"),
            }

            insight_id = uuid4()
            session.execute(
                text(
                    """
                    INSERT INTO personal_insights (
                        id, workspace_id, owner_id, domain_key, insight_key, kind, title,
                        evidence, source_period_start, source_period_end, missing_data,
                        confidence, limitations, professional_referral_note, computed_at
                    ) VALUES (
                        :id, :workspace_id, :owner_id, :domain_key, :insight_key, :kind, :title,
                        CAST(:evidence AS jsonb), :source_period_start, :source_period_end,
                        :missing_data, :confidence, :limitations, :professional_referral_note, :now
                    )
                    """
                ),
                {
                    "id": insight_id,
                    "workspace_id": auth.workspace_id,
                    "owner_id": auth.user_id,
                    "domain_key": payload.source_domain_keys[0],
                    "insight_key": f"ai_insight:{run.id}",
                    "kind": output["kind"],
                    "title": output["title"],
                    "evidence": dumps(evidence),
                    "source_period_start": period_start,
                    "source_period_end": period_end,
                    "missing_data": output["missing_data"],
                    "confidence": output["confidence"],
                    "limitations": output["limitations"],
                    "professional_referral_note": output.get("professional_referral_note"),
                    "now": now,
                },
            )
            row = (
                session.execute(
                    text(
                        "SELECT id, domain_key, kind, title, evidence, source_period_start, "
                        "source_period_end, missing_data, confidence, limitations, "
                        "professional_referral_note, computed_at, dismissed_at "
                        "FROM personal_insights WHERE id = :id"
                    ),
                    {"id": insight_id},
                )
                .mappings()
                .one()
            )
            response = InsightGenerateResponse(
                available=True, insight=InsightResponse(**dict(row)), error_code=None
            )

        # A separate transaction, deliberately not sharing the block above:
        # the `personal_insights` row is already committed by this point --
        # losing only the idempotency bookkeeping record on a transient
        # failure here must not unwind an already-successful generation the
        # caller already paid a real model call for. Mirrors `ai_runtime/
        # runtime.py:create_run`'s identical `try`/`except SQLAlchemyError`
        # shape and its own documented residual risk (a same-key retry
        # after this exact failure won't find a cached response and will
        # re-invoke `execute_run`, a second real model call -- narrower than
        # discarding a response, and insight, the caller already has).
        try:
            with session.begin():
                store_idempotency(
                    session, auth, idempotency_key, req_hash, response.model_dump(mode="json"), now
                )
        except SQLAlchemyError:
            pass
        return response
