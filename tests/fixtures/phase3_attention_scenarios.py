"""Versioned, checked-in regression fixture for Phase 3 Task 1's
attention-policy refactor.

``GOLDEN_SCORES`` was captured by running the *pre-refactor* Phase 1
``_score_task``/``_score_commitment``/``_score_risk`` (as they existed at
commit ``ddc510d``, before this task moved them into
``ecc.domains.attention.attention`` and made them read from
``policy.AttentionPolicy`` instead of inline literals) against the row
fixtures below, not invented. ``NOW``/``TODAY`` are frozen so the fixture is
reproducible; every ``created_at`` is set 30+ days before ``NOW`` so Phase
3's new ``recently_created`` factor (a 24-hour window) never fires here --
this fixture proves the *refactor* is behavior-preserving, before Task 1
adds any new factor. Every row also omits ``prior_deferred_until`` (absent
key, not ``None``), so the new ``previously_deferred`` factor is inert too.
"""

from datetime import UTC, datetime, timedelta

NOW = datetime(2026, 7, 23, 12, 0, 0, tzinfo=UTC)
TODAY = NOW.date()
_OLD = NOW - timedelta(days=30)

TASK_SCENARIOS = {
    "task_overdue_critical_pinned_blocked": {
        "manual_priority": "critical",
        "due_date": TODAY - timedelta(days=2),
        "due_at": None,
        "pinned": True,
        "blocked_on_person_id": None,
        "status": "blocked",
        "updated_at": NOW - timedelta(days=20),
        "created_at": _OLD,
    },
    "task_due_48h_medium_waiting": {
        "manual_priority": "medium",
        "due_date": None,
        "due_at": NOW + timedelta(hours=10),
        "pinned": False,
        "blocked_on_person_id": "11111111-1111-1111-1111-111111111111",
        "status": "planned",
        "updated_at": NOW - timedelta(days=1),
        "created_at": _OLD,
    },
    "task_due_today_low_stale7d": {
        "manual_priority": "low",
        "due_date": TODAY,
        "due_at": None,
        "pinned": False,
        "blocked_on_person_id": None,
        "status": "planned",
        "updated_at": NOW - timedelta(days=8),
        "created_at": _OLD,
    },
    "task_no_due_high_stale14d": {
        "manual_priority": "high",
        "due_date": None,
        "due_at": None,
        "pinned": False,
        "blocked_on_person_id": None,
        "status": "planned",
        "updated_at": NOW - timedelta(days=15),
        "created_at": _OLD,
    },
    "task_no_factors": {
        "manual_priority": "low",
        "due_date": None,
        "due_at": None,
        "pinned": False,
        "blocked_on_person_id": None,
        "status": "planned",
        "updated_at": NOW,
        "created_at": _OLD,
    },
}

COMMITMENT_SCENARIOS = {
    "commitment_overdue_critical_pinned_made_to_me": {
        "importance": "critical",
        "due_date": None,
        "due_at": NOW - timedelta(hours=3),
        "direction": "made_to_me",
        "pinned": True,
        "confidence": 0.95,
        "updated_at": NOW,
        "created_at": _OLD,
    },
    "commitment_due_48h_high_made_by_me": {
        "importance": "high",
        "due_date": None,
        "due_at": NOW + timedelta(hours=20),
        "direction": "made_by_me",
        "pinned": False,
        "confidence": None,
        "updated_at": NOW,
        "created_at": _OLD,
    },
    "commitment_due_today_medium": {
        "importance": "medium",
        "due_date": TODAY,
        "due_at": None,
        "direction": "made_by_me",
        "pinned": False,
        "confidence": 0.7,
        "updated_at": NOW,
        "created_at": _OLD,
    },
    "commitment_no_due_low": {
        "importance": "low",
        "due_date": None,
        "due_at": None,
        "direction": "made_by_me",
        "pinned": False,
        "confidence": 0.5,
        "updated_at": NOW,
        "created_at": _OLD,
    },
}

RISK_SCENARIOS = {
    "risk_high_impact_review_overdue_pinned": {
        "probability": 5,
        "impact": 5,
        "review_at": NOW - timedelta(hours=5),
        "pinned": True,
        "created_at": _OLD,
    },
    "risk_medium_impact_review_due_soon": {
        "probability": 4,
        "impact": 3,
        "review_at": NOW + timedelta(hours=30),
        "pinned": False,
        "created_at": _OLD,
    },
    "risk_low_impact_no_review": {
        "probability": 2,
        "impact": 3,
        "review_at": None,
        "pinned": False,
        "created_at": _OLD,
    },
    "risk_zero_impact": {
        "probability": 1,
        "impact": 1,
        "review_at": None,
        "pinned": False,
        "created_at": _OLD,
    },
}

# Captured from the pre-refactor code (see module docstring). Kept as plain
# literals, not re-derived from the current policy config, so this fixture
# stays a genuine regression check rather than a tautology against itself.
GOLDEN_SCORES = {
    "tasks": {
        "task_overdue_critical_pinned_blocked": {"score": 86, "confidence": 0.8},
        "task_due_48h_medium_waiting": {"score": 38, "confidence": 1.0},
        "task_due_today_low_stale7d": {"score": 34, "confidence": 0.8},
        "task_no_due_high_stale14d": {"score": 33, "confidence": 1.0},
        "task_no_factors": {"score": 5, "confidence": 1.0},
    },
    "commitments": {
        "commitment_overdue_critical_pinned_made_to_me": {"score": 88, "confidence": 0.95},
        "commitment_due_48h_high_made_by_me": {"score": 33, "confidence": 0.6},
        "commitment_due_today_medium": {"score": 35, "confidence": 0.7},
        "commitment_no_due_low": {"score": 4, "confidence": 0.5},
    },
    "risks": {
        "risk_high_impact_review_overdue_pinned": {"score": 80, "confidence": 1.0},
        "risk_medium_impact_review_due_soon": {"score": 30, "confidence": 1.0},
        "risk_low_impact_no_review": {"score": 8, "confidence": 1.0},
        "risk_zero_impact": {"score": 0, "confidence": 1.0},
    },
}
