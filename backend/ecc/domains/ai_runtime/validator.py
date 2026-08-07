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
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

_MAX_EXPLANATION_WORDS = 60

# Defense-in-depth character ceiling, independent of the word-count check
# above: `_max_word_count` splits on whitespace, so a single pathological
# "word" with no whitespace at all (e.g. a huge base64 blob or a repeated-
# character run) would satisfy a <=60-word count while still being
# unbounded in raw size. `Field(max_length=...)` on `explanation_text`
# closes that gap the same way the codebase already bounds other
# model-controlled/user-controlled text fields (e.g. `notes.py`'s
# `body: str = Field(min_length=1, max_length=100000)`,
# `risks.py`'s `description: str = Field(min_length=1, max_length=5000)`).
# Sized generously above any legitimate 60-word answer, not as a normal-
# usage limit.
_MAX_EXPLANATION_CHARS = 2000

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
        if error["type"] == "extra_forbidden":
            # Unlike every other Pydantic error type, `extra_forbidden`'s
            # `loc` is not a fixed, schema-known field path -- its final
            # segment is the literal unexpected key name taken verbatim
            # from the raw response (e.g. a model hallucinating an extra
            # JSON field, or attacker-influenced content echoed into a
            # key). Using it here would leak raw model-derived text into a
            # value this class's own docstring (and every caller that
            # persists/returns `detail`) treats as redaction-safe.
            loc = "<extra_field>"
        else:
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

    explanation_text: str = Field(min_length=1, max_length=_MAX_EXPLANATION_CHARS)
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

    `reason` also covers `personal.generate_insight`'s second, distinct
    failure mode (`missing_professional_referral`, see `check_personal_
    insight_grounding` below) -- reusing this one dataclass rather than
    inventing a near-identical type per task for a check that is still
    conceptually "did this output satisfy the structural safety
    requirements it was shown enough to satisfy," just checking a
    different field. `ungrounded_codes` is simply empty (`()`) for that
    other reason; nothing in this dataclass claims the two reasons share a
    meaning beyond both mapping to `execute_run`'s existing `grounding_
    failed` error code.

    `email.detect_action` has no second failure mode of its own here --
    unlike `personal.generate_insight`'s professional-referral check,
    `EmailDetectActionOutput._validate_action_shape`'s own conditional
    `model_validator` already enforces `cited_message_ids` is non-empty
    whenever `has_action` is true, so an unsupported claim citing nothing
    fails schema validation (`schema_invalid`) before `check_email_
    detect_action_grounding` ever runs -- that function only ever
    produces the shared `"ungrounded_citation"` reason (or `None`), the
    same as `check_explain_item_grounding`.
    """

    reason: Literal["ungrounded_citation", "missing_professional_referral"] = "ungrounded_citation"
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


# ---------------------------------------------------------------------------
# attention.explain_item.reflect -- first-slice Reflection Engine
# (`docs/architecture/chapter-03-ai-runtime.md`'s Reflection Engine
# section, narrowed to one bounded additional call on this one task type,
# gated by `budgets.py:reflection_enabled`).
# ---------------------------------------------------------------------------


class ExplainItemReflection(BaseModel):
    """The reflection call's output shape: the model reviews its own
    already-validated, already-grounded `ExplainItemOutput` answer and
    either approves it unchanged or proposes a revision. `extra="forbid"`
    mirrors `ExplainItemOutput`'s strictness.

    Deliberately no word-count (or any other) field validator here, unlike
    `ExplainItemOutput._max_word_count` -- a proposed revision is never
    accepted as-is. `runtime.py:execute_run` re-validates
    `revised_explanation_text`/`revised_cited_factor_codes` by constructing
    a fresh `ExplainItemOutput` from them and running it through
    `validate_output` and `check_explain_item_grounding`, the exact same
    checks the original answer had to pass -- so every schema rule
    (including the 60-word cap) has exactly one owner, `ExplainItemOutput`,
    not a second, independently-typed copy on this class.
    """

    model_config = ConfigDict(extra="forbid")

    approved: bool
    revised_explanation_text: str | None = None
    revised_cited_factor_codes: list[str] | None = None


# ---------------------------------------------------------------------------
# meeting.prep_summary -- Phase 4-consuming wiring of Phase 3's
# `meeting_prep.py` "Optional enrichment" (`MEETING-PREP-CONTRACT.md`),
# feature-flagged off until now via `config.py:meeting_prep_ai_enrichment_
# enabled`. Second task type this activation registers, structurally
# different from `attention.explain_item`: no single scalar "item", a
# multi-section evidence bundle (participants/timeline/commitments/
# decisions/notes/risks/dependencies) already fetched deterministically by
# `meeting_prep.py:_generate_pack` before the AI ever sees it.
# ---------------------------------------------------------------------------

_MAX_SUMMARY_WORDS = 150

# Same defense-in-depth rationale as `_MAX_EXPLANATION_CHARS` above, scaled
# to this field's higher word cap.
_MAX_SUMMARY_CHARS = 5000


class MeetingPrepSummary(BaseModel):
    """`{summary_text, cited_evidence_ids}` -- mirrors `ExplainItemOutput`'s
    shape exactly (one bounded text field, one citation list), just with a
    higher word cap: a meeting pack's evidence bundle is richer than one
    attention item's factor list, so a useful summary needs more room.
    `extra="forbid"` and the word-count field validator both mirror
    `ExplainItemOutput` for the same reasons stated there.
    """

    model_config = ConfigDict(extra="forbid")

    summary_text: str = Field(min_length=1, max_length=_MAX_SUMMARY_CHARS)
    cited_evidence_ids: list[str] = Field(default_factory=list)

    @field_validator("summary_text")
    @classmethod
    def _max_word_count(cls, value: str) -> str:
        word_count = len(value.split())
        if word_count > _MAX_SUMMARY_WORDS:
            raise ValueError(f"summary_text exceeds {_MAX_SUMMARY_WORDS} words (got {word_count})")
        return value


def check_meeting_prep_grounding(
    output: MeetingPrepSummary, evidence_ids: Iterable[str]
) -> GroundingFailure | None:
    """`check_explain_item_grounding`'s exact pattern, for `meeting.
    prep_summary`'s citation list instead of `cited_factor_codes` --
    reuses `GroundingFailure` unchanged (`ungrounded_codes` is already
    generic over "some cited identifier", not `attention.explain_item`-
    specific despite that function's docstring; only this call site's
    meaning of "identifier" differs: a participant/timeline/commitment/
    decision/note/risk/dependency row id here, a factor code there).

    `evidence_ids` is every id actually present in the pack content the
    model was shown -- supplied by the caller (`runtime.py`), never
    re-derived here, matching `check_explain_item_grounding`'s same
    no-database-access discipline. Deliberately excludes `evidence_gaps`
    ids (`meeting_prep.py:build_pack`): a gap represents the *absence* of
    available evidence, not a citable fact, so a summary citing a gap's id
    as if it supported a claim is exactly the kind of ungrounded citation
    this check exists to catch, not a legitimate one to allow.
    """
    valid_ids = set(evidence_ids)
    ungrounded = tuple(id_ for id_ in output.cited_evidence_ids if id_ not in valid_ids)
    if ungrounded:
        return GroundingFailure(ungrounded_codes=ungrounded)
    return None


# ---------------------------------------------------------------------------
# personal.generate_insight -- Phase 7 Task 5 part 2's `trend`/`correlation`
# AI-generated insights (`docs/phases/phase-007/INSIGHT-CONTRACT.md`'s
# safety rubric), the third task type this activation registers and the
# first with a *conditional* structural requirement layered on top of
# ordinary citation grounding: `professional_referral_note` must be
# non-empty specifically when any source domain this insight actually drew
# from is `high_stakes`-classified (`health`/`finance`).
# ---------------------------------------------------------------------------

_MAX_INSIGHT_EXPLANATION_WORDS = 120

# Same defense-in-depth rationale as `_MAX_EXPLANATION_CHARS` above, scaled
# to this schema's several free-text fields (a richer shape than either
# prior task type's single scalar item/evidence-bundle explanation).
_MAX_INSIGHT_TITLE_CHARS = 200
_MAX_INSIGHT_EXPLANATION_CHARS = 3000
_MAX_INSIGHT_SOURCE_PERIOD_CHARS = 200
_MAX_INSIGHT_MISSING_DATA_CHARS = 500
_MAX_INSIGHT_LIMITATIONS_CHARS = 1000
_MAX_INSIGHT_REFERRAL_NOTE_CHARS = 500


class PersonalInsightOutput(BaseModel):
    """`{kind, title, explanation_text, cited_record_ids, source_period,
    missing_data, confidence, limitations, professional_referral_note}` --
    `INSIGHT-CONTRACT.md`'s safety rubric: "source period/missing
    data/confidence/limitations are Pydantic-required fields on the
    insight output schema, not a prompt-only instruction ... an insight
    response omitting one fails schema validation and is never returned".
    `extra="forbid"` matches every other task type's strict-mode intent.

    `kind` is a closed **two**-value enum (`trend`/`correlation` only),
    narrower than `personal_insights.kind`'s own five-value CHECK
    constraint (`observation`/`reminder`/`planning_suggestion` are
    deterministic-only kinds this task type never produces -- migration
    `0054_phase7_personal_domains.py`'s Task 1 status: "trend/correlation
    kinds (the only ones a model generates) are deferred..."). Deliberately
    **no** numeric score/ranking/leaderboard field anywhere on this model
    -- `INSIGHT-CONTRACT.md`'s "No insight kind computes or surfaces a
    numeric relationship-health score, ranking or frequency leaderboard"
    is enforced structurally, by this schema simply never having declared
    such a field, not by a runtime check a future edit could bypass.

    `missing_data` is a required non-empty string here, unlike the
    deterministic `habits.py:InsightResponse.missing_data` (`str | None`,
    genuinely absent for a computation with no gaps) -- this is a *model*
    output schema, and asking a small instruction-following model to
    correctly emit JSON `null` for "nothing missing" is a real, avoidable
    reliability risk this schema sidesteps by requiring an explicit
    statement either way (e.g. "none noted").

    `professional_referral_note` is optional at the schema level (`None`
    for a `standard`/`sensitive`-only insight, matching `INSIGHT-
    CONTRACT.md`'s "absent on other domains' insights") -- the
    *conditional* requirement ("non-empty specifically when any source
    domain is `high_stakes`-classified") is enforced by `check_personal_
    insight_grounding` below, after schema validation, since that
    condition depends on which domains actually contributed to this
    insight -- information only the caller (`runtime.py`, from the `personal.
    get_insight_sources` tool's own output) has, not this schema.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["trend", "correlation"]
    title: str = Field(min_length=1, max_length=_MAX_INSIGHT_TITLE_CHARS)
    explanation_text: str = Field(min_length=1, max_length=_MAX_INSIGHT_EXPLANATION_CHARS)
    cited_record_ids: list[str] = Field(default_factory=list)
    source_period: str = Field(min_length=1, max_length=_MAX_INSIGHT_SOURCE_PERIOD_CHARS)
    missing_data: str = Field(min_length=1, max_length=_MAX_INSIGHT_MISSING_DATA_CHARS)
    confidence: Literal["low", "medium", "high"]
    limitations: str = Field(min_length=1, max_length=_MAX_INSIGHT_LIMITATIONS_CHARS)
    professional_referral_note: str | None = Field(
        default=None, max_length=_MAX_INSIGHT_REFERRAL_NOTE_CHARS
    )

    @field_validator("explanation_text")
    @classmethod
    def _max_word_count(cls, value: str) -> str:
        word_count = len(value.split())
        if word_count > _MAX_INSIGHT_EXPLANATION_WORDS:
            raise ValueError(
                f"explanation_text exceeds {_MAX_INSIGHT_EXPLANATION_WORDS} words "
                f"(got {word_count})"
            )
        return value


def check_personal_insight_grounding(
    output: PersonalInsightOutput,
    record_ids: Iterable[str],
    *,
    requires_professional_referral: bool = False,
) -> GroundingFailure | None:
    """`check_explain_item_grounding`'s exact citation-grounding pattern
    (`cited_record_ids` against every `domain_records` id the `personal.
    get_insight_sources` tool actually showed the model), plus one second,
    independent check specific to this task type: `INSIGHT-CONTRACT.md`'s
    "`health`/`finance`-classified insights require a non-empty
    `professional_referral_note` field". `requires_professional_referral`
    is supplied by the caller (`runtime.py`, computed from whether any
    source domain the tool returned is `high_stakes`-classified) -- this
    function has no database access and cannot re-derive it itself,
    matching `check_explain_item_grounding`/`check_meeting_prep_
    grounding`'s identical no-database-access discipline.

    Citation grounding is checked first and returned immediately if it
    fails -- an output that already cites a nonexistent record is rejected
    on that basis alone, regardless of whether it also happens to include
    a referral note.
    """
    valid_ids = set(record_ids)
    ungrounded = tuple(code for code in output.cited_record_ids if code not in valid_ids)
    if ungrounded:
        return GroundingFailure(ungrounded_codes=ungrounded)
    if requires_professional_referral and not (output.professional_referral_note or "").strip():
        return GroundingFailure(reason="missing_professional_referral")
    return None


# ---------------------------------------------------------------------------
# email.detect_action -- Phase 10 Task 5's proactive Gmail action-detection
# tool (`docs/superpowers/plans/2026-08-04-phase-10-gmail-connector.md`
# Task 5). The fourth task type this module registers, and the first whose
# output is never persisted directly: a `has_action: true` result becomes a
# `source="ai"` `recommendations` row via `governance.recommendation_
# mutations.create_recommendation` (Task 4's create-path), never a direct
# `tasks`/`commitments`/`risks` write -- this module has no opinion on that;
# it only validates the model's own output shape and grounding.
# ---------------------------------------------------------------------------

_MAX_ACTION_RATIONALE_WORDS = 100

# Same defense-in-depth rationale as `_MAX_EXPLANATION_CHARS` above.
_MAX_ACTION_RATIONALE_CHARS = 2000


class EmailDetectActionOutput(BaseModel):
    """`{has_action, target_type, operation, proposed_fields, rationale,
    confidence, cited_message_ids}` -- `has_action: false` (the required-
    to-clear-the-evaluation-floor negative outcome for newsletters/FYI-only
    mail/already-resolved threads, per the plan) is a first-class, equally
    valid answer, not an error: every other field is `None`/empty in that
    case, enforced below, so a negative determination can never carry
    stray create-payload data alongside it.

    `proposed_fields` is deliberately `dict[str, Any]`, not one of
    `TaskCreate`/`CommitmentCreate`/`RiskCreate` directly -- this module has
    no dependency on the `planning`/`communication`/`governance` domains
    (matching every other task type's schema here, none of which imports
    another domain's models), and the real, authoritative validation
    against the right `*Create` model already happens exactly once, at
    `RecommendationCreate`'s own `model_validator` (`governance.
    recommendation_models`), the instant this output becomes a
    recommendation -- re-declaring that validation here would be a second,
    driftable copy of the same rule Task 4 already owns.

    `operation` is always `"create"` when `has_action` is true (Task 5
    only ever detects a brand-new task/commitment/risk, never proposes an
    update to an existing one -- there is no "existing target" a proactive
    email scan could safely identify without a human already having
    linked that email to one). It is still a real field, not a hardcoded
    constant, because `RecommendationCreate.proposed_action` needs the
    exact `{"operation": "create", "value": None}` shape verbatim
    (`recommendation_targets.py`'s `_ALLOWED[*]["create"] = {None}`
    sentinel) -- keeping it here makes that shape traceable to the model's
    own output rather than synthesized silently downstream.

    `cited_message_ids` is required non-empty when `has_action` is true
    (enforced below, not just by `check_email_detect_action_grounding`):
    an action claimed with zero citations would trivially pass a grounding
    check whose `ungrounded` set is computed by intersecting an *empty*
    cited list against the valid ids, i.e. an unsupported claim that
    accidentally looks "grounded" because it cites nothing at all. This
    schema-level requirement closes that gap structurally; `check_email_
    detect_action_grounding` still separately verifies every id actually
    cited is real (the model could cite a plausible-looking but invented
    id, which an empty-list check alone would never catch).
    """

    model_config = ConfigDict(extra="forbid")

    has_action: bool
    target_type: Literal["task", "commitment", "risk"] | None = None
    operation: Literal["create"] | None = None
    proposed_fields: dict[str, Any] | None = None
    rationale: str = Field(min_length=1, max_length=_MAX_ACTION_RATIONALE_CHARS)
    confidence: float = Field(ge=0, le=1)
    cited_message_ids: list[str] = Field(default_factory=list)

    @field_validator("rationale")
    @classmethod
    def _max_word_count(cls, value: str) -> str:
        word_count = len(value.split())
        if word_count > _MAX_ACTION_RATIONALE_WORDS:
            raise ValueError(
                f"rationale exceeds {_MAX_ACTION_RATIONALE_WORDS} words (got {word_count})"
            )
        return value

    @model_validator(mode="after")
    def _validate_action_shape(self) -> EmailDetectActionOutput:
        if self.has_action:
            if self.target_type is None:
                raise ValueError("target_type is required when has_action is true")
            if self.operation is None:
                raise ValueError("operation is required when has_action is true")
            if not self.proposed_fields:
                raise ValueError("proposed_fields is required when has_action is true")
            if not self.cited_message_ids:
                raise ValueError("cited_message_ids is required when has_action is true")
        else:
            if self.target_type is not None:
                raise ValueError("target_type must be omitted when has_action is false")
            if self.operation is not None:
                raise ValueError("operation must be omitted when has_action is false")
            if self.proposed_fields is not None:
                raise ValueError("proposed_fields must be omitted when has_action is false")
            if self.cited_message_ids:
                raise ValueError("cited_message_ids must be empty when has_action is false")
        return self


def check_email_detect_action_grounding(
    output: EmailDetectActionOutput, message_ids: Iterable[str]
) -> GroundingFailure | None:
    """`check_explain_item_grounding`'s exact citation-grounding pattern,
    against the source Gmail thread's own `email_messages.id`s (as
    returned by the `email.get_thread_content` tool) instead of factor
    codes. Returns `None` immediately (no citations to check) for a
    `has_action: false` output -- `EmailDetectActionOutput._validate_
    action_shape` already guarantees `cited_message_ids` is empty in that
    case, so there is nothing this check could meaningfully validate; a
    negative determination is never rejected on grounding grounds.
    """
    if not output.has_action:
        return None
    valid_ids = set(message_ids)
    ungrounded = tuple(
        message_id for message_id in output.cited_message_ids if message_id not in valid_ids
    )
    if ungrounded:
        return GroundingFailure(ungrounded_codes=ungrounded)
    return None
