from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from ecc.domains.communication.commitments import (
    CommitmentAction,
    CommitmentCreate,
    CommitmentPatch,
    router,
)


def test_commitment_create_defaults() -> None:
    commitment = CommitmentCreate(
        summary="Send revised operating plan",
        direction="made_by_me",
    )

    assert commitment.status == "confirmed"
    assert commitment.importance == "medium"


def test_detected_commitment_requires_evidence() -> None:
    with pytest.raises(ValidationError):
        CommitmentCreate(
            summary="Potential promise",
            direction="made_to_me",
            status="detected",
        )


def test_due_precision_is_mutually_exclusive() -> None:
    with pytest.raises(ValidationError):
        CommitmentCreate(
            summary="Follow up",
            direction="made_by_me",
            due_date=date(2026, 7, 15),
            due_at=datetime(2026, 7, 15, 9, 0, tzinfo=UTC),
        )


def test_due_at_requires_timezone() -> None:
    with pytest.raises(ValidationError):
        CommitmentPatch(
            expected_version=1,
            due_at=datetime(2026, 7, 15, 9, 0),
        )


def test_action_requires_positive_version() -> None:
    with pytest.raises(ValidationError):
        CommitmentAction(expected_version=0)


def test_commitment_routes_match_contract() -> None:
    routes = {(route.path, method) for route in router.routes for method in route.methods or set()}
    expected = {
        ("/api/v1/commitments", "POST"),
        ("/api/v1/commitments", "GET"),
        ("/api/v1/commitments/{commitment_id}", "GET"),
        ("/api/v1/commitments/{commitment_id}", "PATCH"),
        ("/api/v1/commitments/{commitment_id}/confirm", "POST"),
        ("/api/v1/commitments/{commitment_id}/fulfil", "POST"),
        ("/api/v1/commitments/{commitment_id}/cancel", "POST"),
        ("/api/v1/commitments/{commitment_id}/archive", "POST"),
        ("/api/v1/commitments/{commitment_id}/restore", "POST"),
    }
    assert expected <= routes
