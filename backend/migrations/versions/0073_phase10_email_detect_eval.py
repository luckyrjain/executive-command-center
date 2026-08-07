"""Seed the `email.detect_action` evaluation dataset (Phase 10 Task 5).

`email.detect_action` is the fourth task type Phase 4's evaluation harness
(`ecc.domains.ai_runtime.evaluation`) now evaluates -- `evaluation_sets`'
schema (migration `0031_phase4_evaluation.py`) already supports multiple
`task_type`s, so this migration only inserts a new row.

10 hand-labelled examples, `tests/fixtures/phase10_evaluation_email_detect_
action.py` (that module's own docstring explains the mix): four positive
examples (a task-shaped deadline request, a commitment-shaped promise, a
risk-shaped warning, and a positive example with a longer back-and-forth
thread) and six dedicated negative examples covering every category the
plan's own Task 5 bullet names explicitly -- newsletters, FYI-only mail,
and an already-resolved thread -- plus three further adversarial negatives
(a vague, non-actionable request; a thread whose only "ask" is rhetorical;
an automated no-reply notification). Every negative example's `reference_
has_action` is `False` -- the plan's own "has_action: false as a required-
to-clear-the-floor outcome" -- and is scored by the same structural floors
as a positive example (100% schema validity, 100% grounding, 0 prohibited
facts): `EmailDetectActionOutput._validate_action_shape` (`validator.py`)
requires `cited_message_ids` empty whenever `has_action` is false, so
`check_email_detect_action_grounding` short-circuits to a trivial pass for
these -- a negative determination is never rejected on grounding grounds.

This migration's `_EXAMPLES` literal is a duplicate of that fixture's
`EXAMPLES`, following `0058_phase7_insight_eval.py`'s own established
precedent for keeping the migration's seed data self-contained rather than
importing the fixture module at migration time.

No id-assignment step is needed here -- `email.detect_action`'s grounding
check validates `cited_message_ids` against whatever real `email_messages.
id` values `evaluation.py`'s own `_insert_synthetic_email_thread` helper
assigns at insert time (random `uuid4()`, the same convention `personal.
generate_insight`'s dataset already uses for its own `domain_records`),
not a predetermined id this migration and the fixture module must
independently agree on in advance.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0073_phase10_email_detect_eval"
down_revision = "0072_phase10_email_detect_action"
branch_labels = None
depends_on = None

_TASK_TYPE = "email.detect_action"

_EXAMPLES: list[dict[str, Any]] = [
    {
        "key": "task_contract_deadline",
        "subject": "Signed contract needed by Friday",
        "messages": [
            {
                "sender": "priya@partner-co.test",
                "direction": "inbound",
                "sent_at_days_ago": 1,
                "body": (
                    "Hi -- following up on the vendor agreement we discussed. Could you "
                    "please sign and send the attached contract back to me by end of day "
                    "Friday? We can't move forward on the engagement until we have it on "
                    "file."
                ),
            }
        ],
        "must_cite": ["messages.0"],
        "must_not_state": ["this thread has already been resolved", "no action needed"],
        "reference_has_action": True,
    },
    {
        "key": "commitment_report_promise",
        "subject": "Re: Q3 status",
        "messages": [
            {
                "sender": "jordan@vendor.test",
                "direction": "inbound",
                "sent_at_days_ago": 2,
                "body": (
                    "Thanks for checking in. I'll have the full Q3 performance report "
                    "ready and sent over to your team by next Wednesday. Let me know if "
                    "you need anything sooner."
                ),
            }
        ],
        "must_cite": ["messages.0"],
        "must_not_state": ["this is a newsletter", "no action needed"],
        "reference_has_action": True,
    },
    {
        "key": "risk_vendor_delay_warning",
        "subject": "Heads up: possible shipping delay",
        "messages": [
            {
                "sender": "logistics@supplier.test",
                "direction": "inbound",
                "sent_at_days_ago": 1,
                "body": (
                    "Wanted to flag early: our current freight carrier is reporting "
                    "customs delays on the west coast route, and there's a real chance "
                    "the Q3 shipment you're expecting slips past its committed delivery "
                    "date. Nothing confirmed yet, but wanted you to have visibility now "
                    "rather than finding out at the deadline."
                ),
            }
        ],
        "must_cite": ["messages.0"],
        "must_not_state": ["this delay has already been resolved", "no risk identified"],
        "reference_has_action": True,
    },
    {
        "key": "task_thread_followup_after_silence",
        "subject": "Re: Re: Budget approval",
        "messages": [
            {
                "sender": "morgan@ourcompany.test",
                "direction": "outbound",
                "sent_at_days_ago": 10,
                "body": "Following up on the budget approval -- any update on your end?",
            },
            {
                "sender": "finance@partner-co.test",
                "direction": "inbound",
                "sent_at_days_ago": 9,
                "body": "Still reviewing internally, will get back to you this week.",
            },
            {
                "sender": "finance@partner-co.test",
                "direction": "inbound",
                "sent_at_days_ago": 1,
                "body": (
                    "Apologies for the delay -- the budget is approved on our end. Could "
                    "you please send over the updated purchase order so we can process "
                    "payment before the end of the month?"
                ),
            },
        ],
        "must_cite": ["messages.2"],
        "must_not_state": ["budget was rejected", "no action needed"],
        "reference_has_action": True,
    },
    {
        "key": "newsletter_product_update",
        "subject": "This month in ProductCo: new features and tips",
        "messages": [
            {
                "sender": "newsletter@productco.test",
                "direction": "inbound",
                "sent_at_days_ago": 3,
                "body": (
                    "Here's what's new this month: three new dashboard widgets, a faster "
                    "export flow, and a roundup of customer stories. Read the full "
                    "release notes on our blog. As always, thanks for being a subscriber."
                ),
            }
        ],
        "must_cite": [],
        "must_not_state": ["please respond by", "action required", "you must"],
        "reference_has_action": False,
    },
    {
        "key": "fyi_meeting_notes_shared",
        "subject": "FYI: notes from yesterday's sync",
        "messages": [
            {
                "sender": "casey@ourcompany.test",
                "direction": "inbound",
                "sent_at_days_ago": 1,
                "body": (
                    "Sharing notes from yesterday's team sync for anyone who couldn't "
                    "make it. No action needed from you -- just wanted to keep everyone "
                    "in the loop. We covered the roadmap review and a few minor process "
                    "tweaks."
                ),
            }
        ],
        "must_cite": [],
        "must_not_state": ["please respond by", "deadline", "you are responsible for"],
        "reference_has_action": False,
    },
    {
        "key": "already_resolved_thread",
        "subject": "Re: Invoice #4821 overdue",
        "messages": [
            {
                "sender": "billing@supplier.test",
                "direction": "inbound",
                "sent_at_days_ago": 8,
                "body": "Invoice #4821 is now 15 days overdue. Please remit payment ASAP.",
            },
            {
                "sender": "accounts@ourcompany.test",
                "direction": "outbound",
                "sent_at_days_ago": 6,
                "body": "Apologies -- payment has been sent via wire, confirmation attached.",
            },
            {
                "sender": "billing@supplier.test",
                "direction": "inbound",
                "sent_at_days_ago": 5,
                "body": (
                    "Confirming we've received payment for invoice #4821 in full. Thank "
                    "you, and no further action is needed on this one."
                ),
            },
        ],
        "must_cite": [],
        "must_not_state": ["payment is still outstanding", "please pay", "overdue"],
        "reference_has_action": False,
    },
    {
        "key": "vague_non_actionable_request",
        "subject": "Thoughts?",
        "messages": [
            {
                "sender": "sam@ourcompany.test",
                "direction": "inbound",
                "sent_at_days_ago": 2,
                "body": (
                    "Been thinking about where the team is headed this year. Curious "
                    "what your general take is whenever you have a spare moment, no "
                    "rush at all."
                ),
            }
        ],
        "must_cite": [],
        "must_not_state": ["due by", "deadline", "commitment made"],
        "reference_has_action": False,
    },
    {
        "key": "rhetorical_question_negative",
        "subject": "Re: Friday deploy",
        "messages": [
            {
                "sender": "alex@ourcompany.test",
                "direction": "inbound",
                "sent_at_days_ago": 1,
                "body": (
                    "Can you believe the deploy last Friday actually went smoothly for "
                    "once? First time in months nothing broke. Just had to share."
                ),
            }
        ],
        "must_cite": [],
        "must_not_state": ["deploy failed", "please fix", "action required"],
        "reference_has_action": False,
    },
    {
        "key": "automated_noreply_notification",
        "subject": "Your weekly usage summary",
        "messages": [
            {
                "sender": "no-reply@saastool.test",
                "direction": "inbound",
                "sent_at_days_ago": 1,
                "body": (
                    "This is an automated summary. Your team used 82% of the monthly "
                    "quota this week. This message does not require a response; this "
                    "mailbox is not monitored."
                ),
            }
        ],
        "must_cite": [],
        "must_not_state": ["please respond", "action required", "reply to confirm"],
        "reference_has_action": False,
    },
]

assert len(_EXAMPLES) == 10


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
