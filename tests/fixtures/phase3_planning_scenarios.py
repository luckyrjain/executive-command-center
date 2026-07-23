"""Versioned, checked-in scenario fixtures for propose_plan's seven-step
deterministic order (PLANNING-CONTRACT.md), mirroring
tests/fixtures/phase2_resolution_dataset.py's convention of a plain,
importable dataset module rather than generating scenarios ad hoc inline.
"""

from datetime import date, datetime, timedelta
from uuid import UUID

from ecc.domains.attention.planning import (
    CandidateItemInput,
    CapacityDayInput,
    DeadlineConstraintInput,
    ReservedBlockInput,
)

TIMEZONE = "Asia/Kolkata"
MONDAY = date(2026, 7, 20)  # a real Monday, used across scenarios needing a fixed anchor.

FULL_WEEK_CAPACITY = [
    CapacityDayInput(weekday=weekday, available_minutes=480) for weekday in range(7)
]
WEEKDAY_ONLY_CAPACITY = [
    CapacityDayInput(weekday=weekday, available_minutes=480) for weekday in range(5)
]
NO_CAPACITY: list[CapacityDayInput] = []


def candidate(
    label: str,
    score: int,
    *,
    pinned: bool = False,
    due_at: datetime | None = None,
    effort: int | None = None,
) -> CandidateItemInput:
    return CandidateItemInput(
        entity_type="task",
        entity_id=UUID(int=abs(hash(label)) % (2**64)),
        label=label,
        score=score,
        pinned=pinned,
        due_at=due_at,
        effort_minutes=effort,
    )


def deadline(label: str, due_at: datetime, priority: int = 50) -> DeadlineConstraintInput:
    return DeadlineConstraintInput(
        source_id=UUID(int=abs(hash(label)) % (2**64)),
        label=label,
        due_at=due_at,
        priority=priority,
    )


def reserved(
    label: str, starts_at: datetime, ends_at: datetime, *, source_type: str = "calendar_event"
) -> ReservedBlockInput:
    return ReservedBlockInput(
        source_type=source_type,  # type: ignore[arg-type]
        source_id=UUID(int=abs(hash(label)) % (2**64)),
        label=label,
        starts_at=starts_at,
        ends_at=ends_at,
    )


def monday_local(hour: int, minute: int = 0) -> datetime:
    from zoneinfo import ZoneInfo

    return datetime(MONDAY.year, MONDAY.month, MONDAY.day, hour, minute, tzinfo=ZoneInfo(TIMEZONE))


# "Full calendar" -- a single day whose entire workable window is booked out.
FULL_CALENDAR_RESERVATION = reserved("All-day offsite", monday_local(9), monday_local(17))

# "Equal scores" -- three candidates tied on score, distinguished only by
# entity_id, proving the tie-break is stable across repeated calls.
TIED_CANDIDATES = [candidate(f"Tied {i}", score=50) for i in range(3)]

# "Overdue work" -- a deadline constraint whose due_at has already passed.
OVERDUE_DEADLINE = deadline("Overdue report", monday_local(9) - timedelta(days=1))
