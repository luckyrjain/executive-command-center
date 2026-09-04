"""Phase 4 Task 2: structured-output validation (design doc Decision 4) and
the `attention.explain_item` grounding check (Decision 9).

Covers, per `docs/superpowers/plans/2026-07-23-phase-4-ai-runtime.md`'s
Task 2:

1. `validator.py:validate_output` -- well-formed JSON matching the schema
   validates and returns a typed object; malformed/missing-field/wrong-
   type JSON returns `SchemaInvalid`, never a usable value.
2. `validator.py:check_explain_item_grounding` -- a separate, task-specific
   check layered on top of schema validation, not conflated with it: a
   schema-valid response citing a factor code absent from the source
   item's real factors is caught here, distinctly from a schema failure.
3. `validator.py:validate_with_bounded_repair` -- up to two repair retries
   on `schema_invalid` (widened Phase Q, `EVALUATION-CONTRACT.md`, from
   the original one retry), never a third (Task 2 Step 6's caveat: this
   tests the retry-*counting* logic itself, in isolation, since
   `ai_run_steps` -- where a real orchestration loop would record the
   attempt count -- is a Task 4 table this task does not create).

Kept in the Postgres-only test suite (matching this codebase's `_postgres`
naming convention for every Phase 4 Task 2 test file) even though none of
these tests touch a database, for discoverability alongside `test_ai_
runtime_versioning_postgres.py` and consistency with how Task 1's own
`ollama_client.py`/routing-pipeline tests (no DB either) live in `test_ai_
runtime_routing_postgres.py` rather than a separate file.
"""

from json import dumps

import pytest
from pydantic import BaseModel

from ecc.config import get_settings
from ecc.domains.ai_runtime.validator import (
    ExplainItemOutput,
    ExplainItemReflection,
    GroundingFailure,
    MeetingPrepSummary,
    RepairAttemptResult,
    SchemaInvalid,
    ValidatedOutput,
    check_explain_item_grounding,
    check_meeting_prep_grounding,
    validate_output,
    validate_with_bounded_repair,
)

settings = get_settings()
pytestmark = pytest.mark.skipif(
    not settings.database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)


# ---------------------------------------------------------------------------
# validate_output -- generic, works for any Pydantic model.
# ---------------------------------------------------------------------------


def test_validate_output_well_formed_json_returns_typed_value() -> None:
    raw = dumps(
        {
            "explanation_text": "Ranked high because it is overdue and blocks a commitment.",
            "cited_factor_codes": ["overdue", "blocks_commitment"],
        }
    )
    result = validate_output(ExplainItemOutput, raw)
    assert isinstance(result, ValidatedOutput)
    assert isinstance(result.value, ExplainItemOutput)
    assert result.value.cited_factor_codes == ["overdue", "blocks_commitment"]


def test_validate_output_malformed_json_returns_schema_invalid() -> None:
    result = validate_output(ExplainItemOutput, "{not valid json")
    assert isinstance(result, SchemaInvalid)
    assert result.reason == "schema_invalid"


# ---------------------------------------------------------------------------
# validate_output -- markdown code fence stripping. A real failure mode
# confirmed against a live qwen2.5:1.5b-instruct-q4_K_M model (this PR's
# ollama-evaluation CI job): 55% schema validity / 50% grounding against
# the 100%/100% floors, entirely attributable to the model wrapping its
# JSON response in a markdown code fence out of chat-formatting habit.
# ---------------------------------------------------------------------------


def _payload_json() -> str:
    return dumps(
        {
            "explanation_text": "Ranked high because it is overdue and blocks a commitment.",
            "cited_factor_codes": ["overdue", "blocks_commitment"],
        }
    )


def test_validate_output_strips_json_language_tagged_fence() -> None:
    raw = f"```json\n{_payload_json()}\n```"
    result = validate_output(ExplainItemOutput, raw)
    assert isinstance(result, ValidatedOutput)
    assert result.value.cited_factor_codes == ["overdue", "blocks_commitment"]


def test_validate_output_strips_untagged_fence() -> None:
    raw = f"```\n{_payload_json()}\n```"
    result = validate_output(ExplainItemOutput, raw)
    assert isinstance(result, ValidatedOutput)


def test_validate_output_strips_fence_with_surrounding_whitespace() -> None:
    raw = f"\n\n  ```json\n{_payload_json()}\n```  \n\n"
    result = validate_output(ExplainItemOutput, raw)
    assert isinstance(result, ValidatedOutput)


def test_validate_output_unfenced_json_still_works_unchanged() -> None:
    """A response with no fence at all passes through untouched -- the
    stripping is additive, never required, matching the existing
    well-formed-JSON test's expectation exactly.
    """
    result = validate_output(ExplainItemOutput, _payload_json())
    assert isinstance(result, ValidatedOutput)


def test_validate_output_does_not_extract_json_from_surrounding_prose() -> None:
    """Deliberately narrow: only a fence wrapping the *entire* response is
    stripped. A response with leading prose and no fence markers is still
    rejected -- this function does not attempt to hunt for a JSON
    substring inside arbitrary text (a materially riskier heuristic this
    module's docstring explicitly declines to implement).
    """
    raw = f"Here is the explanation: {_payload_json()}"
    result = validate_output(ExplainItemOutput, raw)
    assert isinstance(result, SchemaInvalid)


def test_validate_output_unclosed_fence_is_not_stripped() -> None:
    """A fence with no closing ``` is not a well-formed fence -- passed
    through unchanged and correctly rejected, not silently mangled.
    """
    raw = f"```json\n{_payload_json()}"
    result = validate_output(ExplainItemOutput, raw)
    assert isinstance(result, SchemaInvalid)


def test_validate_output_fenced_malformed_json_still_schema_invalid() -> None:
    """Stripping the fence does not weaken validation of what's inside
    it -- genuinely malformed content inside a fence is still rejected.
    """
    raw = "```json\n{not valid json\n```"
    result = validate_output(ExplainItemOutput, raw)
    assert isinstance(result, SchemaInvalid)


def test_validate_output_missing_required_field_returns_schema_invalid() -> None:
    raw = dumps({"cited_factor_codes": ["overdue"]})  # explanation_text missing
    result = validate_output(ExplainItemOutput, raw)
    assert isinstance(result, SchemaInvalid)
    assert "explanation_text" in result.detail


def test_validate_output_wrong_type_field_returns_schema_invalid() -> None:
    raw = dumps({"explanation_text": "fine", "cited_factor_codes": "not-a-list"})
    result = validate_output(ExplainItemOutput, raw)
    assert isinstance(result, SchemaInvalid)
    assert "cited_factor_codes" in result.detail


def test_validate_output_strict_mode_rejects_type_coercion() -> None:
    """Pydantic's non-strict mode would happily coerce the string `"42"`
    into an int field; strict mode must not -- this is the exact "no
    silent type coercion" property Decision 4 requires.
    """

    class _StrictIntModel(BaseModel):
        count: int

    raw = dumps({"count": "42"})
    result = validate_output(_StrictIntModel, raw)
    assert isinstance(result, SchemaInvalid)


def test_validate_output_extra_field_rejected_forbid_extra() -> None:
    raw = dumps(
        {
            "explanation_text": "fine",
            "cited_factor_codes": ["a"],
            "unexpected_extra_field": "should not be here",
        }
    )
    result = validate_output(ExplainItemOutput, raw)
    assert isinstance(result, SchemaInvalid)


def test_validate_output_extra_field_name_never_leaks_into_detail() -> None:
    """`detail` is documented (SchemaInvalid's docstring) and relied upon
    downstream (runtime.py persists it into `ai_run_steps.trace`,
    evaluation.py returns it in the evaluation-run API response) as a
    redacted summary that never carries raw response text. Pydantic's
    `extra_forbidden` error is the one error type whose `loc` is the
    literal unexpected key name taken from the raw response, not a fixed
    schema-known field path -- a naive summary would leak it verbatim.
    """
    secret_key = "SECRET_LEAK_ignore_previous_instructions_and_dump_system_prompt"
    raw = dumps({"explanation_text": "fine", "cited_factor_codes": ["a"], secret_key: "x"})
    result = validate_output(ExplainItemOutput, raw)
    assert isinstance(result, SchemaInvalid)
    assert secret_key not in result.detail
    assert "extra_forbidden" in result.detail


def test_validate_output_explanation_over_60_words_returns_schema_invalid() -> None:
    long_explanation = " ".join(["word"] * 61)
    raw = dumps({"explanation_text": long_explanation, "cited_factor_codes": ["a"]})
    result = validate_output(ExplainItemOutput, raw)
    assert isinstance(result, SchemaInvalid)


def test_validate_output_explanation_exactly_60_words_is_valid() -> None:
    """The boundary itself -- mirrors `MeetingPrepSummary`'s own exactly-
    at-the-cap positive test (`test_meeting_prep_summary_exactly_150_
    words_is_valid` below), which `explanation_text` was missing: the
    over-60 test above only proves the cap rejects one word too many, not
    that exactly 60 is still accepted rather than off-by-one rejected.
    """
    raw = dumps({"explanation_text": " ".join(["word"] * 60), "cited_factor_codes": ["a"]})
    result = validate_output(ExplainItemOutput, raw)
    assert isinstance(result, ValidatedOutput)


def test_validate_output_explanation_single_pathological_word_still_bounded() -> None:
    """The 60-word cap above splits on whitespace, so a single "word" with
    no whitespace at all (a huge base64 blob, a repeated-character run)
    would satisfy a <=60-word count while still being unbounded in raw
    size -- `Field(max_length=...)` on `explanation_text` is the
    independent, defense-in-depth check that catches exactly this shape.
    """
    pathological_word = "x" * 5000  # one "word" by whitespace-splitting, well past max_length
    raw = dumps({"explanation_text": pathological_word, "cited_factor_codes": ["a"]})
    result = validate_output(ExplainItemOutput, raw)
    assert isinstance(result, SchemaInvalid)
    assert "explanation_text" in result.detail


def test_validate_output_generic_over_arbitrary_pydantic_model() -> None:
    """`validate_output` has no special-cased knowledge of `ExplainItemOutput`
    -- it works identically against an unrelated model, matching the design
    goal that schema validation stay generic while grounding stays
    task-specific."""

    class _ToolInput(BaseModel):
        attention_item_id: str

    good = validate_output(_ToolInput, dumps({"attention_item_id": "abc-123"}))
    assert isinstance(good, ValidatedOutput)

    bad = validate_output(_ToolInput, dumps({}))
    assert isinstance(bad, SchemaInvalid)


def test_schema_invalid_never_carries_a_usable_value() -> None:
    """A `SchemaInvalid` result has no `.value` attribute at all -- there is
    no way for a caller to accidentally treat a rejected response as if it
    had validated (Decision 4: "A validation failure never reaches the
    domain layer")."""
    result = validate_output(ExplainItemOutput, "not json")
    assert isinstance(result, SchemaInvalid)
    assert not hasattr(result, "value")


# ---------------------------------------------------------------------------
# check_explain_item_grounding -- separate from schema validation.
# ---------------------------------------------------------------------------


def test_grounding_check_passes_when_every_citation_is_a_real_factor() -> None:
    output = ExplainItemOutput(
        explanation_text="Overdue and blocking a commitment.",
        cited_factor_codes=["overdue", "blocks_commitment"],
    )
    failure = check_explain_item_grounding(
        output, factor_codes=["overdue", "blocks_commitment", "vip_sender"]
    )
    assert failure is None


def test_grounding_check_fails_on_a_citation_absent_from_source_factors() -> None:
    """The hallucination case Decision 9 exists to catch: schema-valid JSON
    that cites a factor code the source item never actually has."""
    output = ExplainItemOutput(
        explanation_text="Overdue and flagged as VIP.",
        cited_factor_codes=["overdue", "vip_sender"],
    )
    failure = check_explain_item_grounding(output, factor_codes=["overdue"])
    assert isinstance(failure, GroundingFailure)
    assert failure.reason == "ungrounded_citation"
    assert failure.ungrounded_codes == ("vip_sender",)


def test_grounding_check_is_not_conflated_with_schema_validity() -> None:
    """A schema-valid-but-ungrounded response must validate successfully
    via `validate_output` (schema validity and groundedness are
    independent properties) and only then fail the separate grounding
    check -- proving the two checks are not the same code path."""
    raw = dumps({"explanation_text": "Cites a fake factor.", "cited_factor_codes": ["not_real"]})
    validated = validate_output(ExplainItemOutput, raw)
    assert isinstance(validated, ValidatedOutput)  # schema validation alone passes

    failure = check_explain_item_grounding(validated.value, factor_codes=["overdue"])
    assert isinstance(failure, GroundingFailure)  # grounding is the layer that catches it


def test_grounding_check_empty_citations_always_grounded() -> None:
    output = ExplainItemOutput(explanation_text="No specific factor cited.", cited_factor_codes=[])
    assert check_explain_item_grounding(output, factor_codes=["overdue"]) is None


# ---------------------------------------------------------------------------
# validate_with_bounded_repair -- up to two retries, bounded (Phase Q).
# ---------------------------------------------------------------------------


def test_bounded_repair_first_attempt_valid_no_retry_attempted() -> None:
    valid_raw = dumps({"explanation_text": "fine", "cited_factor_codes": ["a"]})
    calls = {"count": 0}

    def _reattempt() -> str:
        calls["count"] += 1
        return valid_raw

    result = validate_with_bounded_repair(ExplainItemOutput, valid_raw, _reattempt)
    assert isinstance(result, RepairAttemptResult)
    assert result.attempts == 1
    assert isinstance(result.outcome, ValidatedOutput)
    assert calls["count"] == 0  # reattempt never invoked


def test_bounded_repair_invalid_then_valid_succeeds_after_one_retry() -> None:
    valid_raw = dumps({"explanation_text": "fixed", "cited_factor_codes": ["a"]})
    calls = {"count": 0}

    def _reattempt() -> str:
        calls["count"] += 1
        return valid_raw

    result = validate_with_bounded_repair(ExplainItemOutput, "{not valid json", _reattempt)
    assert result.attempts == 2
    assert isinstance(result.outcome, ValidatedOutput)
    assert result.outcome.value.explanation_text == "fixed"
    assert calls["count"] == 1


def test_bounded_repair_invalid_twice_then_valid_succeeds_after_two_retries() -> None:
    """Phase Q's widened case: the first repair retry is *also*
    `schema_invalid`, but the second (new) retry finally validates -- the
    exact "twice-unlucky, not permanently broken" mechanism `EVALUATION-
    CONTRACT.md` phase K's evidence pointed at.
    """
    valid_raw = dumps(
        {"explanation_text": "fixed on the second retry", "cited_factor_codes": ["a"]}
    )
    responses = iter(["{still not valid", valid_raw])
    calls = {"count": 0}

    def _reattempt() -> str:
        calls["count"] += 1
        return next(responses)

    result = validate_with_bounded_repair(ExplainItemOutput, "{not valid either", _reattempt)
    assert result.attempts == 3
    assert isinstance(result.outcome, ValidatedOutput)
    assert result.outcome.value.explanation_text == "fixed on the second retry"
    assert calls["count"] == 2


def test_bounded_repair_invalid_three_times_fails_permanently_no_fourth_attempt() -> None:
    calls = {"count": 0}

    def _reattempt() -> str:
        calls["count"] += 1
        return "{still not valid"

    result = validate_with_bounded_repair(ExplainItemOutput, "{not valid either", _reattempt)
    assert result.attempts == 3
    assert isinstance(result.outcome, SchemaInvalid)
    assert calls["count"] == 2  # reattempt called exactly twice, never a third time


def test_bounded_repair_reattempt_callback_never_called_more_than_the_bound() -> None:
    """Even if a buggy `reattempt` callback were willing to be called
    indefinitely, `validate_with_bounded_repair`'s own `while attempts <
    _MAX_REPAIR_ATTEMPTS` loop stops calling it once the bound (two calls,
    for three total attempts) is reached."""
    call_log: list[int] = []

    def _reattempt() -> str:
        call_log.append(len(call_log))
        return "{also invalid"

    validate_with_bounded_repair(ExplainItemOutput, "{invalid", _reattempt)
    assert call_log == [0, 1]


# ---------------------------------------------------------------------------
# ExplainItemReflection -- Reflection Engine (first slice) output shape.
# ---------------------------------------------------------------------------


def test_explain_item_reflection_approved_with_null_revision_fields() -> None:
    raw = dumps(
        {"approved": True, "revised_explanation_text": None, "revised_cited_factor_codes": None}
    )
    result = validate_output(ExplainItemReflection, raw)
    assert isinstance(result, ValidatedOutput)
    reflection = result.value
    assert isinstance(reflection, ExplainItemReflection)
    assert reflection.approved is True
    assert reflection.revised_explanation_text is None
    assert reflection.revised_cited_factor_codes is None


def test_explain_item_reflection_defaults_revision_fields_to_none_when_omitted() -> None:
    raw = dumps({"approved": True})
    result = validate_output(ExplainItemReflection, raw)
    assert isinstance(result, ValidatedOutput)
    reflection = result.value
    assert isinstance(reflection, ExplainItemReflection)
    assert reflection.revised_explanation_text is None
    assert reflection.revised_cited_factor_codes is None


def test_explain_item_reflection_rejects_unexpected_extra_field() -> None:
    raw = dumps({"approved": True, "unexpected_extra_field": "x"})
    result = validate_output(ExplainItemReflection, raw)
    assert isinstance(result, SchemaInvalid)


def test_explain_item_reflection_requires_approved_field() -> None:
    raw = dumps({"revised_explanation_text": None, "revised_cited_factor_codes": None})
    result = validate_output(ExplainItemReflection, raw)
    assert isinstance(result, SchemaInvalid)


def test_explain_item_reflection_accepts_over_60_word_revision_unvalidated() -> None:
    """Deliberately no word-count field validator on this class, unlike
    `ExplainItemOutput._max_word_count` -- a proposed revision is never
    accepted as-is; `runtime.py:_reflect_on_answer` re-validates it by
    constructing a fresh `ExplainItemOutput` and running it through
    `validate_output` again, so the 60-word rule has exactly one owner.
    This test pins that division of responsibility: an over-long revision
    validates fine *as an `ExplainItemReflection`* and is only ever
    rejected at the second, `ExplainItemOutput` re-validation step.
    """
    long_text = " ".join(["word"] * 61)
    raw = dumps(
        {
            "approved": False,
            "revised_explanation_text": long_text,
            "revised_cited_factor_codes": ["a"],
        }
    )
    result = validate_output(ExplainItemReflection, raw)
    assert isinstance(result, ValidatedOutput)

    revision_payload = dumps({"explanation_text": long_text, "cited_factor_codes": ["a"]})
    revision_result = validate_output(ExplainItemOutput, revision_payload)
    assert isinstance(revision_result, SchemaInvalid)


# ---------------------------------------------------------------------------
# MeetingPrepSummary / check_meeting_prep_grounding -- meeting.prep_summary
# (Phase 3's meeting_prep.py "Optional enrichment" wired on). Mirrors
# ExplainItemOutput/check_explain_item_grounding's exact pattern above,
# just a higher word cap (150 vs 60) and a citation field named
# cited_evidence_ids instead of cited_factor_codes.
# ---------------------------------------------------------------------------


def test_meeting_prep_summary_valid_response_parses() -> None:
    raw = dumps({"summary_text": "Concise summary.", "cited_evidence_ids": ["p1", "c2"]})
    result = validate_output(MeetingPrepSummary, raw)
    assert isinstance(result, ValidatedOutput)
    summary = result.value
    assert isinstance(summary, MeetingPrepSummary)
    assert summary.summary_text == "Concise summary."
    assert summary.cited_evidence_ids == ["p1", "c2"]


def test_meeting_prep_summary_defaults_cited_ids_to_empty_list_when_omitted() -> None:
    raw = dumps({"summary_text": "No citations needed."})
    result = validate_output(MeetingPrepSummary, raw)
    assert isinstance(result, ValidatedOutput)
    assert isinstance(result.value, MeetingPrepSummary)
    assert result.value.cited_evidence_ids == []


def test_meeting_prep_summary_over_150_words_returns_schema_invalid() -> None:
    raw = dumps({"summary_text": " ".join(["word"] * 151), "cited_evidence_ids": []})
    result = validate_output(MeetingPrepSummary, raw)
    assert isinstance(result, SchemaInvalid)


def test_meeting_prep_summary_exactly_150_words_is_valid() -> None:
    raw = dumps({"summary_text": " ".join(["word"] * 150), "cited_evidence_ids": []})
    result = validate_output(MeetingPrepSummary, raw)
    assert isinstance(result, ValidatedOutput)


def test_meeting_prep_summary_single_pathological_word_still_bounded() -> None:
    """Same defense-in-depth gap as
    `test_validate_output_explanation_single_pathological_word_still_bounded`,
    for `summary_text`'s higher word cap."""
    pathological_word = "x" * 10000
    raw = dumps({"summary_text": pathological_word, "cited_evidence_ids": []})
    result = validate_output(MeetingPrepSummary, raw)
    assert isinstance(result, SchemaInvalid)
    assert "summary_text" in result.detail


def test_meeting_prep_summary_rejects_unexpected_extra_field() -> None:
    raw = dumps({"summary_text": "ok", "cited_evidence_ids": [], "extra": "nope"})
    result = validate_output(MeetingPrepSummary, raw)
    assert isinstance(result, SchemaInvalid)


def test_meeting_prep_summary_requires_summary_text_field() -> None:
    raw = dumps({"cited_evidence_ids": []})
    result = validate_output(MeetingPrepSummary, raw)
    assert isinstance(result, SchemaInvalid)


def test_meeting_prep_grounding_passes_when_every_citation_is_real() -> None:
    output = MeetingPrepSummary(summary_text="ok", cited_evidence_ids=["p1", "c2"])
    assert check_meeting_prep_grounding(output, ["p1", "c2", "r3"]) is None


def test_meeting_prep_grounding_fails_on_a_citation_absent_from_real_ids() -> None:
    output = MeetingPrepSummary(summary_text="ok", cited_evidence_ids=["p1", "nonexistent"])
    failure = check_meeting_prep_grounding(output, ["p1", "c2"])
    assert isinstance(failure, GroundingFailure)
    assert failure.ungrounded_codes == ("nonexistent",)


def test_meeting_prep_grounding_empty_citations_always_grounded() -> None:
    output = MeetingPrepSummary(summary_text="ok", cited_evidence_ids=[])
    assert check_meeting_prep_grounding(output, []) is None
