"""Versioned, checked-in labelled dataset for `meeting.prep_summary`'s
evaluation harness (design doc Decision 9,
`docs/phases/phase-004/EVALUATION-CONTRACT.md`) -- the second task type this
activation evaluates, mirroring `tests/fixtures/phase4_evaluation_attention_
explain.py`'s convention of a checked-in, versioned, reproducible fixture
module rather than a fixture file loaded from disk at runtime.

**Why 10 examples, not 20.** Decision 9's "3-4 examples per entity type"
rationale for `attention.explain_item` has no direct analog here --
`meeting.prep_summary` has no discrete `entity_type` dimension to cover
exhaustively; its input is a single evidence bundle composed of up to seven
independent sections (participants, timeline, commitments, decisions, notes,
risks, dependencies). This dataset instead covers 10 distinct *pack
composition* scenarios -- which sections are populated, how many rows, what
mix of directions/statuses/roles -- chosen to exercise every section at
least twice across the set and to include both a maximally sparse and a
maximally rich pack. A smaller, deliberately-scoped set for a genuinely
different kind of coverage, not an attempt to match the first dataset's size.

**Every id is a deterministic UUID, not a factor code like `attention.
explain_item`'s dataset uses.** `meeting.prep_summary`'s grounding check
(`validator.py:check_meeting_prep_grounding`) validates `cited_evidence_ids`
against the real row ids the tool call returns (`meeting.get_prep_pack`),
not a stable code vocabulary -- so `evaluation.py`'s synthetic-meeting
insertion helper must insert each section's rows using exactly these same
ids for grounding to ever succeed. Every row's `"id"` is `None` in the
literal below, filled in by `_assign_deterministic_ids` (see that
function's own docstring for why the ids must be a pure function of `(key,
section, index)`, not `uuid4()`). No id is reused across examples,
including `risks`' ids -- `risks` are workspace-wide, not meeting-scoped
(`meeting_prep.py:_fetch_risks`'s own comment), so `evaluation.py` inserts
and deletes each example's synthetic meeting (including its risks) one at a
time, immediately after scoring it, rather than batching all examples' rows
together like `attention.explain_item`'s synthetic `attention_items` does
-- otherwise one example's risks would leak into every subsequent example's
pack in the same run.

**Fields per example**: `key` (stable, human-readable, not persisted --
test/audit readability only), `objective` (the synthetic meeting's
agenda/title), `participants`/`timeline`/`commitments`/`decisions`/`notes`/
`risks`/`dependencies` (each a list of plain dicts shaped for direct
insertion by `evaluation.py`'s synthetic-meeting helper -- see that
function's docstring for the exact column mapping), `must_cite` (which of
the ids above a fully-grounded reference summary should cite -- written as
`"<section>.<index>"` symbolic references, e.g. `"commitments.0"`, rather
than the actual UUIDs those rows get assigned (see `_assign_deterministic_
ids` below), since a UUID would be meaningless to a human reader here;
`evaluation.py`
does not read this field at all, exactly like `attention.explain_item`'s
`must_cite`/`reference_explanation` -- both datasets keep it as
design-time/audit documentation only, never machine-checked), `must_not_state`
(short phrases naming facts absent from this specific pack -- a
hallucination probe, `evaluation.py` flags a scored summary containing any
of these phrases, case-insensitively, as a prohibited-fact violation), and
`reference_summary` (human-readability comparison only, never exact-match
scored, mirroring the first dataset's identical `reference_explanation`
rule).

**Timing fields are relative, not absolute** (`*_hours_ago`/`*_days` instead
of a fixed datetime) -- resolved against `now` at synthesis time by
`evaluation.py`'s insertion helper, so this fixture stays valid indefinitely
rather than accumulating stale absolute timestamps like a snapshot would.

**Development vs. evaluation split** (`EVALUATION-CONTRACT.md`): every
example below is evaluation data. No development/prompt-iteration examples
are checked in anywhere in this repository.
"""

from typing import Any
from uuid import NAMESPACE_OID, uuid5

TASK_TYPE = "meeting.prep_summary"
DATASET_VERSION = 1
CLASSIFICATION = "labelled"

_SECTIONS = (
    "participants",
    "timeline",
    "commitments",
    "decisions",
    "notes",
    "risks",
    "dependencies",
)


def _assign_deterministic_ids(examples: list[dict[str, Any]]) -> None:
    """Every row's `"id"` placeholder (`None` in the literal below) is
    filled in here, deterministically, from `(example["key"], section,
    index)` -- never a random `uuid4()`. `backend/migrations/versions/
    0035_phase4_meeting_eval.py` seeds `evaluation_sets.examples` from an
    independently-typed duplicate of this same literal (following migration
    `0031_phase4_evaluation.py`'s established "self-contained duplicate,
    not an import" convention) -- for that duplication to be safe, both
    copies' ids must land on exactly the same values without depending on
    the two literals being typed in identical order (fragile, unverifiable
    by inspection). `uuid5` is a pure function of its inputs: calling this
    exact same function, verbatim, in both files guarantees identical ids
    keyed only by each row's own already-fixed `(key, section, index)`
    coordinates, which are visibly identical in both literals by
    construction. A real bug this fixes: an earlier version of this
    fixture used `uuid4()` (random) independently in both files, so no
    citation the evaluation harness ever built could match a real row --
    every example failed grounding, caught by `test_ai_runtime_meeting_
    prep_evaluation_postgres.py::test_run_evaluation_fully_grounded_run_
    passes_every_floor` (`grounding_rate` was `0.0`, not `1.0`).
    """
    for example in examples:
        for section in _SECTIONS:
            for index, row in enumerate(example[section]):
                row["id"] = str(
                    uuid5(
                        NAMESPACE_OID,
                        f"phase4-meeting-prep-eval:{example['key']}:{section}:{index}",
                    )
                )


EXAMPLES: list[dict[str, Any]] = [
    # -- rich_all_sections: every section populated, the "full" case --------
    {
        "key": "rich_all_sections",
        "objective": "Quarterly partnership review with Acme Corp",
        "participants": [
            {"id": None, "entity_name": "Jordan Lee", "role": "organizer"},
            {"id": None, "entity_name": "Casey Morgan", "role": "attendee"},
        ],
        "timeline": [
            {
                "id": None,
                "effective_at_hours_ago": 48,
                "event_type": "note_created",
                "summary": "Kickoff call held, scope agreed",
            },
            {
                "id": None,
                "effective_at_hours_ago": 12,
                "event_type": "commitment_created",
                "summary": "Pricing proposal requested",
            },
        ],
        "commitments": [
            {
                "id": None,
                "direction": "made_to_me",
                "summary": "Send updated pricing proposal",
                "status": "active",
                "due_at_days": 2,
                "counterparty_index": 0,
            },
        ],
        "decisions": [
            {"id": None, "title": "Chose Acme as primary vendor", "body": "Board approved 3-1."},
        ],
        "notes": [
            {"id": None, "title": "Background", "body": "Acme has served us since 2023."},
        ],
        "risks": [
            {
                "id": None,
                "description": "Vendor concentration risk with Acme",
                "probability": 3,
                "impact": 4,
                "status": "monitoring",
                "review_at_days": 5,
            },
        ],
        "dependencies": [
            {
                "id": None,
                "direction": "waiting_on_them",
                "note": "Waiting on Acme's signed contract addendum",
                "expected_at_days": 3,
                "counterparty_index": 1,
            },
        ],
        "must_cite": ["participants.0", "commitments.0", "risks.0"],
        "must_not_state": [
            "the contract has already been signed",
            "the vendor relationship is being terminated",
        ],
        "reference_summary": (
            "This is a quarterly review with Acme Corp. Jordan Lee is organizing; Casey Morgan "
            "is attending. A pricing proposal is due from Acme in two days, and there is an "
            "open vendor-concentration risk under monitoring."
        ),
    },
    # -- sparse_pack: minimal evidence, only one participant -----------------
    {
        "key": "sparse_pack",
        "objective": "Quick sync",
        "participants": [
            {"id": None, "entity_name": "Taylor Kim", "role": "attendee"},
        ],
        "timeline": [],
        "commitments": [],
        "decisions": [],
        "notes": [],
        "risks": [],
        "dependencies": [],
        "must_cite": ["participants.0"],
        "must_not_state": [
            "open commitments",
            "active risks",
            "prior decisions",
            "outstanding dependencies",
        ],
        "reference_summary": (
            "This is a short sync with Taylor Kim; there is no other recorded context."
        ),
    },
    # -- participants_and_commitments_heavy ----------------------------------
    {
        "key": "participants_and_commitments_heavy",
        "objective": "Contract renewal negotiation",
        "participants": [
            {"id": None, "entity_name": "Morgan Reyes", "role": "organizer"},
            {"id": None, "entity_name": "Sam Patel", "role": "attendee"},
            {"id": None, "entity_name": "Riley Chen", "role": "optional"},
        ],
        "timeline": [],
        "commitments": [
            {
                "id": None,
                "direction": "made_by_me",
                "summary": "Draft revised terms",
                "status": "active",
                "due_at_days": 1,
                "counterparty_index": 0,
            },
            {
                "id": None,
                "direction": "made_to_me",
                "summary": "Return legal review notes",
                "status": "active",
                "due_at_days": 4,
                "counterparty_index": 1,
            },
            {
                "id": None,
                "direction": "made_by_me",
                "summary": "Schedule signing call",
                "status": "confirmed",
                "due_at_days": 7,
                "counterparty_index": 2,
            },
        ],
        "decisions": [],
        "notes": [],
        "risks": [],
        "dependencies": [],
        "must_cite": ["commitments.0", "commitments.1", "commitments.2"],
        "must_not_state": ["a decision has been finalized", "any active risk"],
        "reference_summary": (
            "This is a contract renewal negotiation with three open commitments spanning "
            "drafting revised terms, returning legal review notes, and scheduling a signing call."
        ),
    },
    # -- timeline_heavy: recent history dominates ----------------------------
    {
        "key": "timeline_heavy",
        "objective": "Follow-up on recent activity",
        "participants": [
            {"id": None, "entity_name": "Avery Brooks", "role": "attendee"},
        ],
        "timeline": [
            {
                "id": None,
                "effective_at_hours_ago": 6,
                "event_type": "note_created",
                "summary": "Support ticket resolved",
            },
            {
                "id": None,
                "effective_at_hours_ago": 30,
                "event_type": "note_created",
                "summary": "Escalation raised to engineering",
            },
            {
                "id": None,
                "effective_at_hours_ago": 54,
                "event_type": "commitment_created",
                "summary": "Root cause investigation started",
            },
            {
                "id": None,
                "effective_at_hours_ago": 96,
                "event_type": "note_created",
                "summary": "Initial report filed",
            },
        ],
        "commitments": [],
        "decisions": [],
        "notes": [],
        "risks": [],
        "dependencies": [],
        "must_cite": ["timeline.0", "timeline.1"],
        "must_not_state": ["a commitment is overdue", "a decision was made"],
        "reference_summary": (
            "Recent history shows an initial report, an escalation to engineering, and the "
            "support ticket being resolved."
        ),
    },
    # -- decisions_and_notes_heavy --------------------------------------------
    {
        "key": "decisions_and_notes_heavy",
        "objective": "Retrospective on Q2 launch",
        "participants": [
            {"id": None, "entity_name": "Drew Ellis", "role": "organizer"},
        ],
        "timeline": [],
        "commitments": [],
        "decisions": [
            {"id": None, "title": "Postpone regional rollout", "body": "Delayed to Q3."},
            {"id": None, "title": "Adopt new rollout checklist", "body": "Effective immediately."},
        ],
        "notes": [
            {"id": None, "title": "Launch metrics", "body": "Conversion up 4% week over week."},
            {"id": None, "title": "Support load", "body": "Ticket volume within normal range."},
        ],
        "risks": [],
        "dependencies": [],
        "must_cite": ["decisions.0", "decisions.1"],
        "must_not_state": ["an active risk", "an overdue commitment"],
        "reference_summary": (
            "This retrospective covers two decisions from the Q2 launch: postponing the "
            "regional rollout to Q3 and adopting a new rollout checklist."
        ),
    },
    # -- risks_present: workspace risks visible, no commitments --------------
    {
        "key": "risks_present",
        "objective": "Risk review before board update",
        "participants": [
            {"id": None, "entity_name": "Jamie Ortiz", "role": "attendee"},
        ],
        "timeline": [],
        "commitments": [],
        "decisions": [],
        "notes": [],
        "risks": [
            {
                "id": None,
                "description": "Single point of failure in payment processing",
                "probability": 2,
                "impact": 5,
                "status": "monitoring",
                "review_at_days": 1,
            },
            {
                "id": None,
                "description": "Key personnel dependency on one engineer",
                "probability": 4,
                "impact": 3,
                "status": "identified",
                "review_at_days": 10,
            },
        ],
        "dependencies": [],
        "must_cite": ["risks.0", "risks.1"],
        "must_not_state": ["a commitment is due", "a decision was reached"],
        "reference_summary": (
            "Two active risks are up for review: a payment-processing single point of "
            "failure and a key-personnel dependency on one engineer."
        ),
    },
    # -- dependencies_present: waiting_links, mixed direction -----------------
    {
        "key": "dependencies_present",
        "objective": "Cross-team blocker check-in",
        "participants": [
            {"id": None, "entity_name": "Nico Alvarez", "role": "attendee"},
            {"id": None, "entity_name": "Skyler Wu", "role": "attendee"},
        ],
        "timeline": [],
        "commitments": [],
        "decisions": [],
        "notes": [],
        "risks": [],
        "dependencies": [
            {
                "id": None,
                "direction": "waiting_on_them",
                "note": "Waiting on API credentials from the platform team",
                "expected_at_days": 2,
                "counterparty_index": 0,
            },
            {
                "id": None,
                "direction": "blocked_by",
                "note": "Blocked by pending security review",
                "expected_at_days": 5,
                "counterparty_index": 1,
            },
        ],
        "must_cite": ["dependencies.0", "dependencies.1"],
        "must_not_state": ["all blockers have been cleared", "a risk review is scheduled"],
        "reference_summary": (
            "Two dependencies are open: waiting on API credentials from the platform team, "
            "and work blocked by a pending security review."
        ),
    },
    # -- multi_participant_mixed_roles ----------------------------------------
    {
        "key": "multi_participant_mixed_roles",
        "objective": "Hiring committee sync",
        "participants": [
            {"id": None, "entity_name": "Harper Singh", "role": "organizer"},
            {"id": None, "entity_name": "Quinn Delgado", "role": "attendee"},
            {"id": None, "entity_name": "Reese Okafor", "role": "optional"},
        ],
        "timeline": [],
        "commitments": [
            {
                "id": None,
                "direction": "made_to_me",
                "summary": "Send candidate scorecards",
                "status": "active",
                "due_at_days": 1,
                "counterparty_index": 1,
            },
        ],
        "decisions": [],
        "notes": [],
        "risks": [],
        "dependencies": [],
        "must_cite": ["participants.0", "participants.1", "participants.2", "commitments.0"],
        "must_not_state": ["a candidate has been rejected", "an offer has been made"],
        "reference_summary": (
            "This hiring committee sync includes Harper Singh organizing, Quinn Delgado "
            "attending, and Reese Okafor as optional; candidate scorecards are due from "
            "Quinn within a day."
        ),
    },
    # -- commitments_mixed_direction_and_status -------------------------------
    {
        "key": "commitments_mixed_direction_and_status",
        "objective": "Weekly commitments review",
        "participants": [
            {"id": None, "entity_name": "Blair Nakamura", "role": "attendee"},
        ],
        "timeline": [],
        "commitments": [
            {
                "id": None,
                "direction": "made_by_me",
                "summary": "Deliver revised budget",
                "status": "active",
                "due_at_days": 0.5,
                "counterparty_index": 0,
            },
            {
                "id": None,
                "direction": "made_to_me",
                "summary": "Approve vendor invoice",
                "status": "confirmed",
                "due_at_days": None,
                "counterparty_index": 0,
            },
        ],
        "decisions": [],
        "notes": [],
        "risks": [],
        "dependencies": [],
        "must_cite": ["commitments.0", "commitments.1"],
        "must_not_state": ["a decision has been made", "an active risk"],
        "reference_summary": (
            "Two commitments with Blair Nakamura are open: delivering the revised budget "
            "soon, and an invoice awaiting approval with no fixed due date."
        ),
    },
    # -- minimal_two_section_pack: only decisions + risks, nothing else -------
    {
        "key": "minimal_two_section_pack",
        "objective": "Governance check-in",
        "participants": [],
        "timeline": [],
        "commitments": [],
        "decisions": [
            {"id": None, "title": "Retire legacy reporting tool", "body": "Sunset by year end."},
        ],
        "notes": [],
        "risks": [
            {
                "id": None,
                "description": "Compliance gap in data retention policy",
                "probability": 3,
                "impact": 3,
                "status": "monitoring",
                "review_at_days": 6,
            },
        ],
        "dependencies": [],
        "must_cite": ["decisions.0", "risks.0"],
        "must_not_state": ["any participant", "an open commitment", "a dependency"],
        "reference_summary": (
            "This governance check-in covers one decision (retiring the legacy reporting "
            "tool by year end) and one monitored compliance risk in data retention."
        ),
    },
]

assert len(EXAMPLES) == 10, f"expected 10 hand-labelled examples, got {len(EXAMPLES)}"
assert len({example["key"] for example in EXAMPLES}) == 10, "example keys must be unique"
_assign_deterministic_ids(EXAMPLES)
for _example in EXAMPLES:
    for _section in (
        "participants",
        "timeline",
        "commitments",
        "decisions",
        "notes",
        "risks",
        "dependencies",
    ):
        assert _section in _example, f"{_example['key']!r} missing {_section!r} section"
_all_sections_covered = {
    section
    for example in EXAMPLES
    for section in (
        "participants",
        "timeline",
        "commitments",
        "decisions",
        "notes",
        "risks",
        "dependencies",
    )
    if example[section]
}
assert _all_sections_covered == {
    "participants",
    "timeline",
    "commitments",
    "decisions",
    "notes",
    "risks",
    "dependencies",
}, "every pack section must appear populated in at least one example"
for _example in EXAMPLES:
    # `evaluation.py:_insert_synthetic_meeting` indexes into the per-
    # example participant node list (`node_ids[0]` for `timeline`,
    # `node_ids[commitment["counterparty_index"]]` for `commitments`/
    # `dependencies`) with no bounds check -- a non-empty one of these
    # sections paired with an empty `participants` list would raise
    # `IndexError` at insertion time. Enforced here so a future example
    # fails fast, at import time, with a clear message, not a cryptic
    # `IndexError` deep in a synthetic-meeting insert.
    if _example["participants"]:
        continue
    for _section in ("timeline", "commitments", "dependencies"):
        assert not _example[_section], (
            f"{_example['key']!r} has an empty participants section but a "
            f"non-empty {_section!r} section -- {_section} rows index into "
            "participant node ids at insertion time"
        )
