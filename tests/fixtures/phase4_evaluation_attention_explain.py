"""Versioned, checked-in labelled dataset for Phase 4 Task 5's evaluation
harness (design doc Decision 9, `docs/phases/phase-004/EVALUATION-CONTRACT.md`).

20 hand-labelled examples for `task_type='attention.explain_item'`, 3-4 per
Phase 3 `attention_items.entity_type` (`task`, `commitment`, `risk`,
`waiting_link`, `risk_review`, `meeting`) -- mirroring
`tests/fixtures/phase3_attention_scenarios.py`'s convention of a checked-in,
versioned, reproducible fixture module rather than a fixture file loaded
from disk at runtime.

**Provenance.** `task`/`commitment`/`risk` examples 1-3 in each group are
drawn directly from `phase3_attention_scenarios.py`'s own
`TASK_SCENARIOS`/`COMMITMENT_SCENARIOS`/`RISK_SCENARIOS`/`GOLDEN_SCORES` --
same scenario keys, same `score`/`confidence` values, factor codes matching
`ecc.domains.attention.attention._score_task`/`_score_commitment`/
`_score_risk`'s real emitted codes (`manual_priority`, `importance`, `due_48h`,
`overdue`, `pinned`, `waiting_on`, `blocked`, `stale_7d`, `stale_14d`,
`risk_impact`, `review_overdue`, `review_due_soon`, `recently_created`).
`waiting_link` examples use `_score_waiting`'s real codes
(`waiting_direction`, `overdue`, `due_48h`, `stale_7d`, `stale_14d`) -- no
matching Phase 3 fixture scenario exists for `waiting_link` in
`phase3_attention_scenarios.py` (that module predates Phase 3 Task 2's
`_score_waiting`), so these three are newly authored here, using the same
real factor-code vocabulary.

**`risk_review`/`meeting` are synthetic.** Per
`ecc.domains.attention.attention`'s own comment ("waiting_link/risk_review/
meeting are scored starting Task 2/3/7 (Phase 3)") and a direct code search
of this branch, `_score_risk_review`/`_score_meeting` equivalents do not
exist yet -- `regenerate_attention` only ever scores `task`/`commitment`/
`risk`/`waiting_link` (`attention.py`'s `eligible_entity_types`). `EntityType`
(`attention.py`) already includes `risk_review`/`meeting` as valid
`attention_items.entity_type` values, and `attention_items.entity_type` has
no database CHECK constraint (an intentionally unconstrained `String(32)`
column, per that module's own comment), so an `attention_items` row with
`entity_type='risk_review'`/`'meeting'` is a legitimate row shape today even
though nothing in this codebase currently generates one automatically. This
dataset's `risk_review`/`meeting` examples use plausible, consistently-coded
factors following the same `{code, label, points, source_field}` shape
every real scorer already emits (`review_overdue`/`review_due_soon`/`pinned`
reused from `_score_risk`'s vocabulary for `risk_review`, since a risk
review is conceptually tied to the same risk; new but shape-consistent
codes for `meeting`: `starts_soon`, `missing_agenda`,
`no_preparation_notes`, `recently_created`/`pinned` reused). This is a
deliberate, documented choice, not an oversight -- Decision 9 requires "3-4
examples each" across all six entity types Phase 3 scores in its `EntityType`
literal, and the evaluation harness (`ecc.domains.ai_runtime.evaluation`)
only needs a syntactically valid `attention_items` row to drive
`attention.get_item`/`attention.explain_item` end to end; it has no
dependency on `regenerate_attention` having produced that row.

**Fields per example** (Decision 9): `key` (a stable, human-readable
identifier, not persisted anywhere -- for test/audit readability only),
`entity_type`, `score`/`confidence` (the item's already-computed values,
inserted into `attention_items` verbatim by the evaluation harness),
`factors` (the item's real `{code, label, points, source_field}` list --
what `attention.get_item`/`attention.explain_item`'s prompt actually
renders), `must_cite` (factor codes a fully-grounded reference explanation
should reference), `must_not_state` (short phrases naming facts absent from
this item -- a hallucination probe; `evaluation.py` flags a scored
explanation that contains any of these phrases, case-insensitively, as a
prohibited-fact violation), and `reference_explanation` (for human
readability comparison only, never exact-match scored, per Decision 9).

**Development vs. evaluation split (`EVALUATION-CONTRACT.md`).** Every
example below is evaluation data. No development/prompt-iteration examples
are checked in anywhere in this repository -- there is nothing to
accidentally conflate this dataset with.
"""

TASK_TYPE = "attention.explain_item"
DATASET_VERSION = 1
CLASSIFICATION = "labelled"

EXAMPLES = [
    # -- task (4) -- codes from attention.py:_score_task -------------------
    {
        "key": "task_overdue_critical_pinned_blocked",
        "entity_type": "task",
        "score": 86,
        "confidence": 0.8,
        "factors": [
            {
                "code": "manual_priority",
                "label": "Manual priority critical",
                "points": 30,
                "source_field": "manual_priority",
            },
            {
                "code": "overdue",
                "label": "Due timing overdue",
                "points": 25,
                "source_field": "due_date,due_at",
            },
            {
                "code": "pinned",
                "label": "Explicitly pinned",
                "points": 15,
                "source_field": "pinned",
            },
            {
                "code": "blocked",
                "label": "Task is blocked",
                "points": 10,
                "source_field": "status",
            },
            {
                "code": "stale_14d",
                "label": "No movement for 14 days",
                "points": 6,
                "source_field": "updated_at",
            },
        ],
        "must_cite": ["manual_priority", "overdue", "pinned"],
        "must_not_state": ["waiting on another person", "due within 48 hours"],
        "reference_explanation": (
            "This critical, pinned task is overdue and currently blocked, with no "
            "progress in two weeks -- it needs attention now."
        ),
    },
    {
        "key": "task_due_48h_medium_waiting",
        "entity_type": "task",
        "score": 38,
        "confidence": 1.0,
        "factors": [
            {
                "code": "manual_priority",
                "label": "Manual priority medium",
                "points": 15,
                "source_field": "manual_priority",
            },
            {
                "code": "due_48h",
                "label": "Due timing due_48h",
                "points": 20,
                "source_field": "due_date,due_at",
            },
            {
                "code": "waiting_on",
                "label": "Waiting on another person",
                "points": 10,
                "source_field": "blocked_on_person_id",
            },
        ],
        "must_cite": ["due_48h", "waiting_on"],
        "must_not_state": ["overdue", "pinned"],
        "reference_explanation": (
            "This medium-priority task is due within two days and is waiting on "
            "someone else to act."
        ),
    },
    {
        "key": "task_no_due_high_stale14d",
        "entity_type": "task",
        "score": 33,
        "confidence": 1.0,
        "factors": [
            {
                "code": "manual_priority",
                "label": "Manual priority high",
                "points": 25,
                "source_field": "manual_priority",
            },
            {
                "code": "stale_14d",
                "label": "No movement for 14 days",
                "points": 6,
                "source_field": "updated_at",
            },
        ],
        "must_cite": ["manual_priority", "stale_14d"],
        "must_not_state": ["overdue", "a specific due date", "pinned"],
        "reference_explanation": (
            "This high-priority task has no due date but hasn't moved in two weeks."
        ),
    },
    {
        "key": "task_recently_created_pinned",
        "entity_type": "task",
        "score": 28,
        "confidence": 1.0,
        "factors": [
            {
                "code": "manual_priority",
                "label": "Manual priority low",
                "points": 5,
                "source_field": "manual_priority",
            },
            {
                "code": "pinned",
                "label": "Explicitly pinned",
                "points": 15,
                "source_field": "pinned",
            },
            {
                "code": "recently_created",
                "label": "Recently created",
                "points": 8,
                "source_field": "created_at",
            },
        ],
        "must_cite": ["pinned", "recently_created"],
        "must_not_state": ["overdue", "blocked", "waiting on another person"],
        "reference_explanation": (
            "This low-priority task was just created and has been manually pinned for visibility."
        ),
    },
    # -- commitment (3) -- codes from attention.py:_score_commitment -------
    {
        "key": "commitment_overdue_critical_pinned_made_to_me",
        "entity_type": "commitment",
        "score": 88,
        "confidence": 0.95,
        "factors": [
            {
                "code": "importance",
                "label": "Importance critical",
                "points": 30,
                "source_field": "importance",
            },
            {
                "code": "overdue",
                "label": "Due timing overdue",
                "points": 25,
                "source_field": "due_date,due_at",
            },
            {
                "code": "waiting_on",
                "label": "Waiting on another person",
                "points": 10,
                "source_field": "direction",
            },
            {
                "code": "pinned",
                "label": "Explicitly pinned",
                "points": 15,
                "source_field": "pinned",
            },
        ],
        "must_cite": ["importance", "overdue", "waiting_on"],
        "must_not_state": ["due within 48 hours", "no movement in over a week"],
        "reference_explanation": (
            "This critical commitment made to you is overdue and pinned for visibility."
        ),
    },
    {
        "key": "commitment_due_48h_high_made_by_me",
        "entity_type": "commitment",
        "score": 33,
        "confidence": 0.6,
        "factors": [
            {
                "code": "importance",
                "label": "Importance high",
                "points": 25,
                "source_field": "importance",
            },
            {
                "code": "due_48h",
                "label": "Due timing due_48h",
                "points": 20,
                "source_field": "due_date,due_at",
            },
        ],
        "must_cite": ["importance", "due_48h"],
        "must_not_state": ["overdue", "waiting on another person", "pinned"],
        "reference_explanation": (
            "This high-importance commitment you made is due within two days."
        ),
    },
    {
        "key": "commitment_no_due_low",
        "entity_type": "commitment",
        "score": 4,
        "confidence": 0.5,
        "factors": [
            {
                "code": "importance",
                "label": "Importance low",
                "points": 5,
                "source_field": "importance",
            },
        ],
        "must_cite": ["importance"],
        "must_not_state": ["overdue", "a due date", "pinned", "waiting on another person"],
        "reference_explanation": "This is a low-importance commitment with no due date.",
    },
    # -- risk (4) -- codes from attention.py:_score_risk --------------------
    {
        "key": "risk_high_impact_review_overdue_pinned",
        "entity_type": "risk",
        "score": 80,
        "confidence": 1.0,
        "factors": [
            {
                "code": "risk_impact",
                "label": "Risk impact 25",
                "points": 30,
                "source_field": "probability,impact",
            },
            {
                "code": "review_overdue",
                "label": "Risk review overdue",
                "points": 20,
                "source_field": "review_at",
            },
            {
                "code": "pinned",
                "label": "Explicitly pinned",
                "points": 15,
                "source_field": "pinned",
            },
        ],
        "must_cite": ["risk_impact", "review_overdue", "pinned"],
        "must_not_state": ["review due within 48 hours"],
        "reference_explanation": (
            "This high-impact risk's review is overdue and it has been pinned for attention."
        ),
    },
    {
        "key": "risk_medium_impact_review_due_soon",
        "entity_type": "risk",
        "score": 30,
        "confidence": 1.0,
        "factors": [
            {
                "code": "risk_impact",
                "label": "Risk impact 12",
                "points": 15,
                "source_field": "probability,impact",
            },
            {
                "code": "review_due_soon",
                "label": "Risk review due within 48 hours",
                "points": 10,
                "source_field": "review_at",
            },
        ],
        "must_cite": ["risk_impact", "review_due_soon"],
        "must_not_state": ["overdue", "pinned"],
        "reference_explanation": ("This medium-impact risk's review is coming up within two days."),
    },
    {
        "key": "risk_low_impact_no_review",
        "entity_type": "risk",
        "score": 8,
        "confidence": 1.0,
        "factors": [
            {
                "code": "risk_impact",
                "label": "Risk impact 6",
                "points": 5,
                "source_field": "probability,impact",
            },
        ],
        "must_cite": ["risk_impact"],
        "must_not_state": ["review overdue", "pinned"],
        "reference_explanation": "This is a low-impact risk with no scheduled review.",
    },
    {
        "key": "risk_zero_impact_pinned",
        "entity_type": "risk",
        "score": 15,
        "confidence": 1.0,
        "factors": [
            {
                "code": "pinned",
                "label": "Explicitly pinned",
                "points": 15,
                "source_field": "pinned",
            },
        ],
        "must_cite": ["pinned"],
        "must_not_state": ["risk impact", "review overdue", "review due soon"],
        "reference_explanation": (
            "This risk currently has no scored impact but has been manually pinned."
        ),
    },
    # -- waiting_link (3) -- codes from attention.py:_score_waiting ---------
    {
        "key": "waiting_link_waiting_on_me_overdue_stale",
        "entity_type": "waiting_link",
        "score": 75,
        "confidence": 1.0,
        "factors": [
            {
                "code": "waiting_direction",
                "label": "Waiting: waiting on me",
                "points": 30,
                "source_field": "direction",
            },
            {
                "code": "overdue",
                "label": "Expected timing passed",
                "points": 25,
                "source_field": "expected_at",
            },
            {
                "code": "stale_14d",
                "label": "Waiting for 14+ days",
                "points": 6,
                "source_field": "since_at",
            },
        ],
        "must_cite": ["waiting_direction", "overdue", "stale_14d"],
        "must_not_state": ["due within 48 hours", "blocked by"],
        "reference_explanation": (
            "This is waiting on you directly, the expected time has passed, and "
            "it's been outstanding for over two weeks."
        ),
    },
    {
        "key": "waiting_link_blocked_by_due_48h",
        "entity_type": "waiting_link",
        "score": 60,
        "confidence": 1.0,
        "factors": [
            {
                "code": "waiting_direction",
                "label": "Waiting: blocked by",
                "points": 30,
                "source_field": "direction",
            },
            {
                "code": "due_48h",
                "label": "Expected within 48 hours",
                "points": 20,
                "source_field": "expected_at",
            },
        ],
        "must_cite": ["waiting_direction", "due_48h"],
        "must_not_state": ["overdue", "no movement in over a week"],
        "reference_explanation": (
            "Your work is blocked by this, and it's expected within the next two days."
        ),
    },
    {
        "key": "waiting_link_waiting_on_them_stale7d",
        "entity_type": "waiting_link",
        "score": 18,
        "confidence": 1.0,
        "factors": [
            {
                "code": "waiting_direction",
                "label": "Waiting: waiting on them",
                "points": 10,
                "source_field": "direction",
            },
            {
                "code": "stale_7d",
                "label": "Waiting for 7+ days",
                "points": 4,
                "source_field": "since_at",
            },
        ],
        "must_cite": ["waiting_direction", "stale_7d"],
        "must_not_state": ["overdue", "blocked by", "waiting on me"],
        "reference_explanation": (
            "You're waiting on someone else for this, and it's been a week with no update."
        ),
    },
    # -- risk_review (3) -- synthetic, reusing _score_risk's vocabulary -----
    {
        "key": "risk_review_overdue_high_impact",
        "entity_type": "risk_review",
        "score": 70,
        "confidence": 1.0,
        "factors": [
            {
                "code": "review_overdue",
                "label": "Risk review overdue",
                "points": 25,
                "source_field": "review_at",
            },
            {
                "code": "risk_impact",
                "label": "Risk impact 20",
                "points": 20,
                "source_field": "probability,impact",
            },
        ],
        "must_cite": ["review_overdue", "risk_impact"],
        "must_not_state": ["review due within 48 hours", "pinned"],
        "reference_explanation": (
            "This risk's scheduled review is overdue and the underlying risk has "
            "significant impact."
        ),
    },
    {
        "key": "risk_review_due_soon_pinned",
        "entity_type": "risk_review",
        "score": 40,
        "confidence": 1.0,
        "factors": [
            {
                "code": "review_due_soon",
                "label": "Risk review due within 48 hours",
                "points": 10,
                "source_field": "review_at",
            },
            {
                "code": "pinned",
                "label": "Explicitly pinned",
                "points": 15,
                "source_field": "pinned",
            },
        ],
        "must_cite": ["review_due_soon", "pinned"],
        "must_not_state": ["overdue"],
        "reference_explanation": "This risk review is due within two days and has been pinned.",
    },
    {
        "key": "risk_review_no_next_review_scheduled",
        "entity_type": "risk_review",
        "score": 12,
        "confidence": 1.0,
        "factors": [
            {
                "code": "no_next_review_scheduled",
                "label": "No follow-up review scheduled",
                "points": 8,
                "source_field": "next_review_at",
            },
        ],
        "must_cite": ["no_next_review_scheduled"],
        "must_not_state": ["review overdue", "review due within 48 hours", "pinned"],
        "reference_explanation": (
            "This risk was reviewed, but no follow-up review has been scheduled."
        ),
    },
    # -- meeting (3) -- synthetic, shape-consistent codes --------------------
    {
        "key": "meeting_starts_soon_missing_agenda",
        "entity_type": "meeting",
        "score": 55,
        "confidence": 1.0,
        "factors": [
            {
                "code": "starts_soon",
                "label": "Meeting starts within 2 hours",
                "points": 25,
                "source_field": "starts_at",
            },
            {
                "code": "missing_agenda",
                "label": "No agenda set",
                "points": 15,
                "source_field": "agenda",
            },
        ],
        "must_cite": ["starts_soon", "missing_agenda"],
        "must_not_state": ["no preparation notes", "pinned"],
        "reference_explanation": "This meeting starts soon and has no agenda set yet.",
    },
    {
        "key": "meeting_no_preparation_notes",
        "entity_type": "meeting",
        "score": 20,
        "confidence": 1.0,
        "factors": [
            {
                "code": "no_preparation_notes",
                "label": "No preparation notes recorded",
                "points": 10,
                "source_field": "preparation",
            },
        ],
        "must_cite": ["no_preparation_notes"],
        "must_not_state": ["starts within 2 hours", "missing agenda"],
        "reference_explanation": (
            "No preparation notes have been recorded for this upcoming meeting."
        ),
    },
    {
        "key": "meeting_pinned_recently_created",
        "entity_type": "meeting",
        "score": 33,
        "confidence": 1.0,
        "factors": [
            {
                "code": "pinned",
                "label": "Explicitly pinned",
                "points": 15,
                "source_field": "pinned",
            },
            {
                "code": "recently_created",
                "label": "Recently created",
                "points": 8,
                "source_field": "created_at",
            },
        ],
        "must_cite": ["pinned", "recently_created"],
        "must_not_state": ["starts soon", "missing agenda"],
        "reference_explanation": (
            "This meeting was just added to your calendar and has been pinned for visibility."
        ),
    },
]

assert len(EXAMPLES) == 20, f"expected 20 hand-labelled examples, got {len(EXAMPLES)}"
assert {example["entity_type"] for example in EXAMPLES} == {
    "task",
    "commitment",
    "risk",
    "waiting_link",
    "risk_review",
    "meeting",
}, "must cover every Phase 3 attention_items.entity_type (design doc Decision 9)"
assert len({example["key"] for example in EXAMPLES}) == 20, "example keys must be unique"
