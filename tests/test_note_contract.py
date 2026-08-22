import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from ecc.domains.knowledge.notes import NoteCreate
from ecc.main import app

client = TestClient(app)


def test_note_routes_require_authentication() -> None:
    assert client.get("/api/v1/notes").status_code == 401
    assert client.get("/api/v1/notes/00000000-0000-0000-0000-000000000001").status_code == 401


def test_note_create_rejects_owner_and_workspace_fields() -> None:
    # An unauthenticated request would 401 before the body is ever parsed
    # against NoteCreate, proving nothing about extra="forbid" -- go
    # straight at the model, matching test_task_contract.py's identical
    # test_task_create_rejects_client_owned_fields.
    with pytest.raises(ValidationError):
        NoteCreate.model_validate(
            {
                "body": "Private note",
                "owner_id": "00000000-0000-0000-0000-000000000001",
                "workspace_id": "00000000-0000-0000-0000-000000000001",
            }
        )


def test_note_body_bounds_are_enforced_by_schema() -> None:
    from pydantic import ValidationError

    from ecc.domains.knowledge.notes import NoteCreate

    try:
        NoteCreate(body="")
    except ValidationError:
        pass
    else:
        raise AssertionError("empty note bodies must be rejected")

    try:
        NoteCreate(body="x" * 100001)
    except ValidationError:
        pass
    else:
        raise AssertionError("oversized note bodies must be rejected")
