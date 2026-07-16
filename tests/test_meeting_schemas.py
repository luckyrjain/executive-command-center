from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from ecc.domains.scheduling.meetings import (
    MeetingPatch,
    _validate_standalone_patch_timing,
)


def test_meeting_patch_accepts_complete_standalone_timing() -> None:
    starts_at = datetime(2026, 7, 20, 9, tzinfo=UTC)
    patch = MeetingPatch(
        expected_version=2,
        starts_at=starts_at,
        ends_at=starts_at + timedelta(hours=1),
        timezone="Asia/Kolkata",
    )

    assert patch.starts_at == starts_at
    assert patch.timezone == "Asia/Kolkata"


def test_meeting_patch_preserves_raw_timing_presence_for_state_aware_validation() -> None:
    for field, value in (
        ("starts_at", "not-a-datetime"),
        ("ends_at", None),
        ("timezone", {"invalid": "shape"}),
    ):
        patch = MeetingPatch(expected_version=2, **{field: value})
        assert field in patch.model_fields_set
        assert getattr(patch, field) == value


@pytest.mark.parametrize(
    "timing",
    [
        {"starts_at": "2026-07-20T09:00:00Z"},
        {
            "starts_at": "2026-07-20T09:00:00Z",
            "ends_at": "2026-07-20T10:00:00Z",
        },
        {
            "starts_at": "2026-07-20T09:00:00",
            "ends_at": "2026-07-20T10:00:00Z",
            "timezone": "UTC",
        },
        {
            "starts_at": "2026-07-20T10:00:00Z",
            "ends_at": "2026-07-20T09:00:00Z",
            "timezone": "UTC",
        },
        {
            "starts_at": "2026-07-20T09:00:00Z",
            "ends_at": "2026-07-20T10:00:00Z",
            "timezone": "Mars/Olympus_Mons",
        },
    ],
)
def test_meeting_patch_rejects_incoherent_standalone_timing(
    timing: dict[str, object],
) -> None:
    patch = MeetingPatch(expected_version=2, **timing)
    with pytest.raises(ValidationError):
        _validate_standalone_patch_timing(patch)
