"""Seed the `meeting.prep_summary` evaluation dataset.

`meeting.prep_summary` is the second task type Phase 4's evaluation harness
(`ecc.domains.ai_runtime.evaluation`) now evaluates -- `evaluation_sets`'
schema (migration `0031_phase4_evaluation.py`) already supports multiple
`task_type`s (`uq_evaluation_sets_active_per_task_type` is a partial unique
index scoped by `task_type`, not a single-row table), so this migration only
inserts a new row rather than altering anything.

10 hand-labelled examples, `tests/fixtures/phase4_evaluation_meeting_prep.py`
(that module's own docstring explains why 10, not the first dataset's 20 --
a genuinely different kind of coverage, not an attempt to match its size).
This migration's `_EXAMPLES` literal is a duplicate of that fixture's
`EXAMPLES`, following `0031_phase4_evaluation.py`'s own established
precedent for keeping the migration's seed data self-contained rather than
importing the fixture module at migration time. Every row's `"id"` is
deterministic (`_assign_deterministic_ids` below, a verbatim copy of the
fixture's identical function), not `uuid4()` -- required for this
self-contained-duplicate approach to be safe when a dataset's citations
must actually match real row ids, not just stable string codes (see that
function's own docstring).
"""

from __future__ import annotations

from typing import Any
from uuid import NAMESPACE_OID, uuid4, uuid5

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0035_phase4_meeting_eval"
down_revision = "0034_phase4_meeting_prep"
branch_labels = None
depends_on = None

_TASK_TYPE = "meeting.prep_summary"

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
    """Verbatim copy of `tests/fixtures/phase4_evaluation_meeting_prep.py`'s
    identical function -- see that copy's docstring for why every row's
    `"id"` placeholder (`None` in the literal below) must be filled in this
    way, as a pure function of `(example["key"], section, index)`, rather
    than an independently-called `uuid4()` in each of the two files (a real
    bug an earlier version of this migration had: every citation the
    evaluation harness built against the fixture's ids failed to match this
    migration's own, independently-random, seeded ids -- `grounding_rate`
    was `0.0` for every run, caught by `test_ai_runtime_meeting_prep_
    evaluation_postgres.py`).
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


_EXAMPLES: list[dict[str, Any]] = [
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
            {
                "id": None,
                "title": "Chose Acme as primary vendor",
                "body": "Board approved 3-1.",
            },
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
            {
                "id": None,
                "title": "Adopt new rollout checklist",
                "body": "Effective immediately.",
            },
        ],
        "notes": [
            {
                "id": None,
                "title": "Launch metrics",
                "body": "Conversion up 4% week over week.",
            },
            {
                "id": None,
                "title": "Support load",
                "body": "Ticket volume within normal range.",
            },
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
    {
        "key": "minimal_two_section_pack",
        "objective": "Governance check-in",
        "participants": [],
        "timeline": [],
        "commitments": [],
        "decisions": [
            {
                "id": None,
                "title": "Retire legacy reporting tool",
                "body": "Sunset by year end.",
            },
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

assert len(_EXAMPLES) == 10
_assign_deterministic_ids(_EXAMPLES)


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)

    evaluation_sets = sa.table(
        "evaluation_sets",
        sa.column("id", uuid),
        sa.column("task_type", sa.String()),
        sa.column("version", sa.Integer()),
        sa.column("classification", sa.String()),
        sa.column("example_count", sa.Integer()),
        sa.column("examples", postgresql.JSONB()),
        sa.column("status", sa.String()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    op.execute(
        evaluation_sets.insert().values(
            id=uuid4(),
            task_type=_TASK_TYPE,
            version=1,
            classification="labelled",
            example_count=len(_EXAMPLES),
            examples=_EXAMPLES,
            status="active",
            created_at=sa.func.now(),
            updated_at=sa.func.now(),
        )
    )


def downgrade() -> None:
    evaluation_sets = sa.table("evaluation_sets", sa.column("task_type", sa.String()))
    op.execute(evaluation_sets.delete().where(evaluation_sets.c.task_type == _TASK_TYPE))
