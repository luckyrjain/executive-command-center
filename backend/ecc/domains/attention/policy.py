"""Versioned attention scoring policy.

Policy v1 reproduces Phase 1's exact pre-Phase-3 point values byte-for-byte
(see tests/fixtures/phase3_attention_scenarios.py's regression-equivalence
fixture) -- no existing ranking changes the day this ships. New Phase 3
factors join the same v1 config additively rather than a v2 bump, per the
repository owner's approved decision (docs/phases/phase-003/ATTENTION-MODEL.md).

``dependency`` and ``meeting`` are reserved weight slots: their weights are
set here but nothing consumes them yet. They stay at zero contribution for
task/commitment/risk scoring until Task 2 (``waiting.py``'s ``_score_waiting``)
and Task 7 (``meeting_prep.py``'s ``_score_meeting``) give them real inputs to
score against. Declaring them now, instead of inventing them ad hoc in a
later task, keeps every entity_type's scorer reading from one shared config.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType


@dataclass(frozen=True)
class AttentionPolicy:
    """``@dataclass(frozen=True)`` only stops *rebinding* a field -- it does
    nothing to stop mutating a mutable object a field points to. The two
    dict-typed fields below are typed as ``Mapping`` and always constructed
    wrapped in ``MappingProxyType`` (see ``POLICY_V1`` and
    ``_frozen_points`` below) specifically so a caller can't do
    ``policy.task_priority_points["low"] = 999`` in place, and so
    ``dataclasses.replace(policy, ...)`` can't produce two "frozen"
    instances that silently share (and can cross-mutate through) the same
    underlying dict object (finding #13).
    """

    version: int

    task_priority_points: Mapping[str, int]
    commitment_importance_points: Mapping[str, int]

    due_overdue_points: int
    due_48h_points: int
    due_today_points: int

    pinned_points: int
    waiting_on_points: int
    blocked_points: int
    stale_7d_points: int
    stale_14d_points: int

    risk_impact_high_points: int
    risk_impact_medium_points: int
    risk_impact_low_points: int
    risk_impact_high_threshold: int
    risk_impact_medium_threshold: int
    risk_impact_low_threshold: int
    review_overdue_points: int
    review_due_soon_points: int

    cap_pinned: int
    cap_unpinned: int

    task_confidence_with_due_date: float
    task_confidence_without_due_date: float
    commitment_confidence_default: float
    commitment_confidence_due_date_cap: float
    risk_confidence: float

    recently_created_points: int
    recently_created_window_hours: int

    previously_deferred_penalty: int

    dependency_weight_cap: int = field(default=15)
    meeting_weight_cap: int = field(default=15)
    importance_weight_cap: int = field(default=20)


def _frozen_points(points: dict[str, int]) -> Mapping[str, int]:
    """Wrap a points table in a read-only view -- see ``AttentionPolicy``'s
    docstring (finding #13). Use this (not a bare dict literal) at every
    ``AttentionPolicy(...)``/``dataclasses.replace(...)`` call site that
    sets one of these two fields, including any future policy version.
    """
    return MappingProxyType(dict(points))


POLICY_V1 = AttentionPolicy(
    version=1,
    task_priority_points=_frozen_points({"critical": 35, "high": 25, "medium": 15, "low": 5}),
    commitment_importance_points=_frozen_points(
        {"critical": 25, "high": 18, "medium": 10, "low": 4}
    ),
    due_overdue_points=35,
    due_48h_points=15,
    due_today_points=25,
    pinned_points=20,
    waiting_on_points=8,
    blocked_points=-12,
    stale_7d_points=4,
    stale_14d_points=8,
    risk_impact_high_points=25,
    risk_impact_medium_points=15,
    risk_impact_low_points=8,
    risk_impact_high_threshold=20,
    risk_impact_medium_threshold=12,
    risk_impact_low_threshold=6,
    review_overdue_points=35,
    review_due_soon_points=15,
    cap_pinned=100,
    cap_unpinned=95,
    task_confidence_with_due_date=0.8,
    task_confidence_without_due_date=1.0,
    commitment_confidence_default=0.6,
    commitment_confidence_due_date_cap=0.8,
    risk_confidence=1.0,
    # New in Phase 3, additive to v1: a small, capped boost so a genuinely
    # new item doesn't get buried under older high-scored items, and a
    # small, capped penalty so an item doesn't snap right back to full
    # urgency the instant a defer expires. Both bounded per
    # ATTENTION-MODEL.md's "bounded_recency"/"bounded_deferral_penalty"
    # terms -- neither can swing a score by more than a few points.
    recently_created_points=3,
    recently_created_window_hours=24,
    previously_deferred_penalty=-5,
)

_POLICIES: dict[int, AttentionPolicy] = {1: POLICY_V1}


def get_active_policy(version: int = 1) -> AttentionPolicy:
    try:
        return _POLICIES[version]
    except KeyError:
        raise ValueError(f"unknown attention policy version: {version}") from None
