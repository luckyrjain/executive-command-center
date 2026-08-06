"""Versioned, checked-in labelled dataset for `email.detect_action`'s
evaluation harness (Phase 10 Task 5, `docs/superpowers/plans/2026-08-04-
phase-10-gmail-connector.md`) -- the fourth task type this activation
evaluates and the first Gmail Connector one, mirroring `tests/fixtures/
phase7_evaluation_personal_insight.py`'s convention of a checked-in,
versioned, reproducible fixture module rather than a fixture file loaded
from disk at runtime.

**Why 10 examples, and why this particular mix.** The plan's own Task 5
bullet requires "positive examples (clear task/commitment language) and
negative examples (newsletters, FYI-only mail, already-resolved threads,
`has_action: false` as a required-to-clear-the-floor outcome)". This
dataset covers: four positive examples (a task-shaped deadline request, a
commitment-shaped promise, a risk-shaped warning, and a longer multi-
message back-and-forth thread whose action only appears in the final
message) and six negative examples -- the three categories the plan names
explicitly (newsletter, FYI-only, already-resolved) plus three further
adversarial negatives distinguishing "sounds action-adjacent but isn't"
from "obviously not actionable" (a vague non-actionable request, a
rhetorical question, an automated no-reply notification).

This module's own `EXAMPLES` literal is a duplicate of migration
`0073_phase10_email_detect_eval.py`'s `_EXAMPLES` -- following
`0058_phase7_insight_eval.py`'s own established precedent for keeping the
migration's seed data self-contained rather than importing this fixture
module at migration time. `test_seeded_evaluation_set_matches_the_checked_
in_fixture` (this dataset's own test file) asserts the two stay in sync.
"""

from __future__ import annotations

from typing import Any

TASK_TYPE = "email.detect_action"
DATASET_VERSION = 1

EXAMPLES: list[dict[str, Any]] = [
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

assert len(EXAMPLES) == 10
