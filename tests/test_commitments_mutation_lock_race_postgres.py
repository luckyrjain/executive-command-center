"""Lock-before-authorize sweep for commitments: the PATCH path
(`_mutate_commitment`) and every lifecycle transition (`lifecycle_write`,
also reached from recommendation confirmation) authorized the caller
*before* taking `SELECT ... FOR UPDATE` on the row they then write. An
ownership transfer (locks the row, rewrites `owner_id`, does not bump
`version`) that committed while the request waited on that row lock left the
request writing to a row the caller could no longer see.

Now the row is locked first and authorization is evaluated afterwards, on
the committed post-transfer row, so the waiting request answers 404 and
writes nothing. See `lock_race_support` for the harness.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from lock_race_support import RaceWorld, headers, race, race_world, row_snapshot
from sqlalchemy import Connection, text

from ecc.config import get_settings
from ecc.database import engine

pytestmark = pytest.mark.skipif(
    not get_settings().database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)


@pytest.fixture
def world() -> Iterator[RaceWorld]:
    with race_world("Commitments Lock Race", ("commitments",)) as w:
        yield w


@dataclass(frozen=True)
class Case:
    status: str
    archived: bool
    method: str
    path: str
    body: dict[str, Any]

    def seed(self, conn: Connection, w: RaceWorld, now: datetime) -> UUID:
        commitment_id = uuid4()
        conn.execute(
            text(
                "INSERT INTO commitments (id, workspace_id, owner_id, summary, direction, "
                "status, created_by, updated_by, created_at, updated_at, visibility, "
                "archived_at, pre_archive_status) "
                "VALUES (:id, :ws, :b, 'Race commitment', 'made_by_me', :status, :b, :b, "
                ":now, :now, 'private', :archived_at, :pre_archive_status)"
            ),
            {
                "id": commitment_id,
                "ws": w.ws,
                "b": w.b,
                "now": now,
                "status": self.status,
                "archived_at": now if self.archived else None,
                "pre_archive_status": self.status if self.archived else None,
            },
        )
        return commitment_id


_V1 = {"expected_version": 1}
_PATH = "/api/v1/commitments/{id}"

CASES: dict[str, Case] = {
    "patch": Case(
        "confirmed", False, "PATCH", _PATH, {"expected_version": 1, "summary": "race probe"}
    ),
    "confirm": Case("detected", False, "POST", _PATH + "/confirm", _V1),
    "fulfil": Case("confirmed", False, "POST", _PATH + "/fulfil", _V1),
    "cancel": Case("confirmed", False, "POST", _PATH + "/cancel", _V1),
    "break": Case("confirmed", False, "POST", _PATH + "/break", _V1),
    "archive": Case("confirmed", False, "POST", _PATH + "/archive", _V1),
    "restore": Case("confirmed", True, "POST", _PATH + "/restore", _V1),
}


def _side_effect_counts(ws: UUID) -> dict[str, int]:
    with engine.connect() as connection:
        return {
            table: int(
                connection.execute(
                    text(f"SELECT count(*) FROM {table} WHERE workspace_id = :ws"),  # noqa: S608
                    {"ws": ws},
                ).scalar_one()
            )
            for table in ("audit_events", "event_outbox", "idempotency_records", "commitments")
        }


def _run(w: RaceWorld, case: Case, row_id: UUID, *, transfer: bool) -> tuple[Any, Any]:
    return race(
        w,
        table="commitments",
        row_id=row_id,
        send=lambda client: client.request(
            case.method,
            case.path.format(id=row_id),
            headers=headers(w.b_token),
            json=case.body,
        ),
        transfer=transfer,
    )


@pytest.mark.parametrize("name", list(CASES))
def test_mutation_waiting_on_row_lock_rechecks_authorization(world: RaceWorld, name: str) -> None:
    case = CASES[name]
    with engine.begin() as connection:
        row_id = case.seed(connection, world, datetime.now(UTC))
    counts_before = _side_effect_counts(world.ws)

    response, before = _run(world, case, row_id, transfer=True)

    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "COMMITMENT_NOT_FOUND"
    assert row_snapshot("commitments", row_id) == {**before, "owner_id": world.c}
    assert _side_effect_counts(world.ws) == counts_before


@pytest.mark.parametrize("name", list(CASES))
def test_mutation_waiting_on_row_lock_without_transfer_still_proceeds(
    world: RaceWorld, name: str
) -> None:
    """Control: the same lock wait with no ownership change succeeds -- the
    404 above comes from the transfer, not from the wait itself."""
    case = CASES[name]
    with engine.begin() as connection:
        row_id = case.seed(connection, world, datetime.now(UTC))

    response, before = _run(world, case, row_id, transfer=False)

    assert response.status_code == 200, response.text
    assert row_snapshot("commitments", row_id)["version"] == before["version"] + 1
