"""Structured-output validation (design doc Decision 4) and the
`attention.explain_item` grounding check (Decision 9).

**Schema validation (generic).** `validate_output` is a thin, task-agnostic
wrapper over Pydantic `TypeAdapter(...).validate_json(..., strict=True)`
(already RFC-005's Phase 0 baseline, 2.11.7 -- no new dependency): "The AI
Runtime validates every model response with `TypeAdapter(OutputModel).
validate_json(raw_response)` (Pydantic's `strict` mode: no silent type
coercion ...) before the response is visible to any caller. A validation
failure never reaches the domain layer" (Decision 4). It accepts *any*
Pydantic model class as `schema_ref` -- it has no knowledge of `attention.
explain_item` or any other specific task shape, so a later task type (or
any tool's `input_schema`/`output_schema`, Decision 6) reuses it unchanged.

**Grounding (task-specific, layered on top, not folded in).** Decision 9:
"every `cited_factor_codes` entry must appear in the source item's actual
`factors` list ... a hard, programmatic grounding check, not a fuzzy
similarity score." `check_explain_item_grounding` is deliberately a
separate function operating on an already-schema-valid `ExplainItemOutput`
-- schema validity and grounding are two independent properties an output
can satisfy or fail independently (a well-formed JSON object can still cite
a nonexistent factor), and conflating them into one path would make it
impossible to tell, from the result alone, which property actually failed.

**Bounded repair retry (Decision 4/5).** `validate_with_bounded_repair`
implements "one bounded repair retry ... allowed on `schema_invalid`
specifically": exactly one re-attempt, no more, regardless of the second
attempt's outcome. This module does not persist the attempt count anywhere
-- `ai_run_steps` (where Decision 4 says a retry would be "recorded on the
trace") is a Task 4 table (migration `0030_phase4_ai_runs.py`, not yet
created as of Task 2). `RepairAttemptResult.attempts` is returned so Task
4's orchestration loop (`runtime.py`) can wire it into a real `ai_run_steps`
row once that table exists; Task 2 tests this counting/bounding logic in
isolation, against no database at all.
"""

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, field_validator

_MAX_EXPLANATION_WORDS = 60

_MARKDOWN_FENCE_RE = re.compile(r"^```[a-zA-Z0-9_-]*\s*\n(?P<body>.*?)\n?```\s*$", re.DOTALL)


def _strip_markdown_fence(raw_response: str) -> str:
    """Small, instruct-tuned models (this activation's `qwen2.5:1.5b`
    included) commonly wrap JSON output in a markdown code fence
    (` ```json ... ``` ` or ` ``` ... ``` `) out of chat-formatting habit,
    even when the prompt explicitly asks for raw JSON (Decision 4's
    template: "Respond with JSON matching exactly: ..."). A strict JSON
    parse rejects the fence markers outright, misreporting a
    structurally-valid response as `schema_invalid` -- confirmed against a
    real live model in this activation's evaluation harness (`PHASE-004`
    live-Ollama CI job), not a hypothetical.

    Narrowly strips only a well-formed leading/trailing triple-backtick
    fence wrapping the *entire* response -- never touches the JSON content
    itself, and deliberately does not attempt to extract a JSON substring
    from surrounding prose (a materially riskier heuristic that could
    silently accept a genuinely malformed response by grabbing a
    JSON-looking fragment out of it). A response with no fence, or any
    other unrecognized wrapping, passes through unchanged and is judged by
    the strict parse exactly as before.
    """
    stripped = raw_response.strip()
    match = _MARKDOWN_FENCE_RE.match(stripped)
    return match.group("body").strip() if match else stripped


@dataclass(frozen=True, slots=True)
class ValidatedOutput:
    """A model response that validated cleanly against its declared
    schema. `value` is the typed Pydantic instance, never the raw JSON
    text -- callers work with typed fields, not ad hoc dict access.
    """

    value: BaseModel


@dataclass(frozen=True, slots=True)
class SchemaInvalid:
    """A model response that failed schema validation -- `API-SCHEMAS.md`'s
    required `schema_invalid` error code. `detail` is a short, redacted
    summary (field path + Pydantic error type only, e.g.
    `"cited_factor_codes:list_type"`) -- never the raw response text or any
    validated/rejected field *value*, matching this codebase's "raw model
    output ... MUST NOT be logged" discipline (`RFC-005.md`). The raw
    response itself is never attached to this object; a caller that needs
    it for a redacted trace store (Task 4) must retain it separately, at
    that layer's own discretion, not here.
    """

    reason: Literal["schema_invalid"] = "schema_invalid"
    detail: str = ""


def _summarize_validation_error(exc: ValidationError) -> str:
    parts = []
    for error in exc.errors(include_url=False):
        loc = ".".join(str(segment) for segment in error["loc"]) or "<root>"
        parts.append(f"{loc}:{error['type']}")
    return "; ".join(parts) or "validation_failed"


def validate_output(
    schema_ref: type[BaseModel], raw_response: str
) -> ValidatedOutput | SchemaInvalid:
    """Validate `raw_response` (raw JSON text from a model or tool call)
    against `schema_ref` in Pydantic strict mode. Generic over any
    `BaseModel` subclass -- `schema_ref` plays the role `output_schema_ref`/
    `output_schema` play in `prompt_versions`/`tool_definitions` (Decision
    3/4): a pointer to the schema to enforce, resolved by the caller (this
    activation resolves it via a small in-code table, e.g. `ExplainItemOutput`
    below, matching `router.py`'s `TASK_REQUIREMENTS` fixed-table
    precedent -- not a database lookup, since task shapes are application
    code, not configurable data, in this activation).

    Malformed JSON and schema-shape mismatches are both surfaced as
    `SchemaInvalid` -- `TypeAdapter.validate_json` raises the same
    `pydantic.ValidationError` for either (a `json_invalid` error type for
    the former), so there is exactly one failure path here, matching
    Decision 4's "A validation failure never reaches the domain layer" (no
    special-cased partial-success state exists).

    `raw_response` is passed through `_strip_markdown_fence` first -- see
    that function's docstring for why (a real, observed failure mode
    against a live model, not speculative hardening).
    """
    try:
        value = TypeAdapter(schema_ref).validate_json(
            _strip_markdown_fence(raw_response), strict=True
        )
    except ValidationError as exc:
        return SchemaInvalid(detail=_summarize_validation_error(exc))
    return ValidatedOutput(value=value)


@dataclass(frozen=True, slots=True)
class RepairAttemptResult:
    """`outcome` is the final validation result after at most one repair
    retry; `attempts` (1 or 2) is how many raw responses were actually
    validated. Task 4's orchestration loop is expected to record `attempts`
    on the eventual `ai_run_steps` trace row (see this module's docstring).
    """

    outcome: ValidatedOutput | SchemaInvalid
    attempts: int


def validate_with_bounded_repair(
    schema_ref: type[BaseModel],
    first_raw_response: str,
    reattempt: Callable[[], str],
) -> RepairAttemptResult:
    """Validate `first_raw_response`; if (and only if) it is
    `schema_invalid`, call `reattempt()` exactly once for a second raw
    response and validate that instead. `reattempt` is never called a
    second time regardless of the second attempt's own outcome -- Decision
    4's "one bounded repair retry", not an open-ended retry loop. A caller
    supplies `reattempt` as a closure over "re-prompt with the validation
    error appended" (Decision 4); this function has no opinion on how the
    second raw response is produced, only that it is requested at most
    once.
    """
    first_result = validate_output(schema_ref, first_raw_response)
    if not isinstance(first_result, SchemaInvalid):
        return RepairAttemptResult(outcome=first_result, attempts=1)

    second_raw_response = reattempt()
    second_result = validate_output(schema_ref, second_raw_response)
    return RepairAttemptResult(outcome=second_result, attempts=2)


# ---------------------------------------------------------------------------
# attention.explain_item -- the first evaluated task type (Decision 9).
# ---------------------------------------------------------------------------


class ExplainItemOutput(BaseModel):
    """`{explanation_text, cited_factor_codes}` -- Decision 9's output
    shape for `attention.explain_item`. `extra="forbid"` matches strict-
    mode's intent: a model response with unexpected extra keys is exactly
    the kind of "loosely-typed JSON" Decision 4 says must not "slide
    through". The 60-word cap is part of the schema contract itself
    (Decision 9: "explanation_text: str (<=60 words)"), not a separate
    business-rule check layered on afterward.
    """

    model_config = ConfigDict(extra="forbid")

    explanation_text: str = Field(min_length=1)
    cited_factor_codes: list[str] = Field(default_factory=list)

    @field_validator("explanation_text")
    @classmethod
    def _max_word_count(cls, value: str) -> str:
        word_count = len(value.split())
        if word_count > _MAX_EXPLANATION_WORDS:
            raise ValueError(
                f"explanation_text exceeds {_MAX_EXPLANATION_WORDS} words (got {word_count})"
            )
        return value


@dataclass(frozen=True, slots=True)
class GroundingFailure:
    """`ungrounded_codes` names exactly which cited codes are not in the
    source item's real `factors` list -- never the explanation text itself
    (which may reference invented facts unrelated to any factor code; this
    check only covers the structurally-checkable citation list, matching
    Decision 9's own scoping: "checkable structurally, not just by human/
    LLM judgment").
    """

    reason: Literal["ungrounded_citation"] = "ungrounded_citation"
    ungrounded_codes: tuple[str, ...] = ()


def check_explain_item_grounding(
    output: ExplainItemOutput, factor_codes: Iterable[str]
) -> GroundingFailure | None:
    """Decision 9's hard, programmatic grounding check, specific to
    `attention.explain_item`'s output shape -- deliberately **not** part of
    `validate_output`'s generic path (which has no notion of "factor
    codes" or any other task-specific concept). Callers run this only
    *after* `validate_output`/`validate_with_bounded_repair` has already
    returned a `ValidatedOutput[ExplainItemOutput]` -- grounding is
    meaningless to check against a response that was not even schema-valid
    in the first place.

    `factor_codes` is the source item's actual factor codes, as returned by
    the `attention.get_item` tool (Decision 6) -- supplied by the caller,
    never re-derived here (this module has no database access and no
    knowledge of `attention_items`).
    """
    valid_codes = set(factor_codes)
    ungrounded = tuple(code for code in output.cited_factor_codes if code not in valid_codes)
    if ungrounded:
        return GroundingFailure(ungrounded_codes=ungrounded)
    return None
