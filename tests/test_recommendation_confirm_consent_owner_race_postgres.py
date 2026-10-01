"""Confirming an `email_action_detected` recommendation re-checks its owner's
`email` consent. That check has to run BEFORE the recommendation row lock (it
takes the domain/connector key-share locks, whose order against the
revocation cascade forbids taking them after it), so it reads the owner from
an unlocked read. With `ECC_PERSONAL_DATA_ISOLATION` off such a row can be
transferred, and a transfer (`authz_grants` locks the row, rewrites
`owner_id`, does not bump `version`) that commits while the confirm waits on
the row lock left it confirming a row whose current owner's consent was never
checked.

Now the owner whose consent was checked is compared with the owner on the
locked row; a mismatch is a retryable 409 `RECOMMENDATION_OWNER_CHANGED`
(nothing written), and the retry checks the new owner. Concurrency is real:
see `lock_race_support`. Consent itself is stubbed (it has its own tests in
`test_gmail_consent_race_postgres`); what is under test is which owner the
confirm checks it for.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from lock_race_support import RaceWorld, headers, race, race_world
from sqlalchemy import text

from ecc.config import get_settings
from ecc.database import engine
from ecc.main import app
from ecc.platform.connector_security import EmailConsentInactiveError

pytestmark = pytest.mark.skipif(
    not get_settings().database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)


@pytest.fixture
def world() -> Iterator[RaceWorld]:
    with race_world(
        "Confirm Consent Owner Race",
        ("recommendation_feedback", "recommendations", "tasks"),
    ) as w:
        yield w


@pytest.fixture
def consent_checks(world: RaceWorld, monkeypatch: pytest.MonkeyPatch) -> list[UUID]:
    """Stands in for the Gmail consent guard: records the owner it is asked
    about, active for everyone but C (who never enabled email)."""
    asked: list[UUID] = []

    def fake(
        session: Any, *, workspace_id: UUID, owner_id: UUID, connector_account_id: Any
    ) -> None:
        asked.append(owner_id)
        if owner_id == world.c:
            raise EmailConsentInactiveError

    monkeypatch.setattr("ecc.domains.personal.gmail_shared.require_email_consent_locked", fake)
    return asked


def _seed(w: RaceWorld, *, recommendation_type: str) -> UUID:
    """A pending recommendation owned by B that sets one of B's tasks to
    `in_progress`. Workspace-visible, so B (an admin) can still confirm it
    after it is transferred to C."""
    task_id, recommendation_id = uuid4(), uuid4()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tasks (id, workspace_id, owner_id, title, created_by, updated_by, "
                "created_at, updated_at, visibility) "
                "VALUES (:id, :ws, :b, 'Race task', :b, :b, now(), now(), 'workspace')"
            ),
            {"id": task_id, "ws": w.ws, "b": w.b},
        )
        conn.execute(
            text(
                "INSERT INTO recommendations (id, workspace_id, recommendation_type, "
                "target_type, target_id, proposed_action, rationale, confidence, status, "
                "evidence_ids, source, created_by, updated_by, created_at, updated_at, "
                "version, expected_version, owner_id, visibility) "
                "VALUES (:id, :ws, :type, 'task', :task, "
                'CAST(\'{"operation": "set_status", "value": "in_progress"}\' AS jsonb), '
                "'Race rationale', 0.9, 'pending_confirmation', ARRAY[]::uuid[], 'rule', "
                ":b, :b, now(), now(), 1, 1, :b, 'workspace')"
            ),
            {
                "id": recommendation_id,
                "ws": w.ws,
                "type": recommendation_type,
                "task": task_id,
                "b": w.b,
            },
        )
    return recommendation_id


def _status(recommendation_id: UUID) -> str:
    with engine.connect() as conn:
        return str(
            conn.execute(
                text("SELECT status FROM recommendations WHERE id = :id"),
                {"id": recommendation_id},
            ).scalar_one()
        )


def _task_statuses(w: RaceWorld) -> list[str]:
    with engine.connect() as conn:
        return list(
            conn.execute(
                text("SELECT status FROM tasks WHERE workspace_id = :ws"), {"ws": w.ws}
            ).scalars()
        )


def _confirm(w: RaceWorld, recommendation_id: UUID) -> Callable[[TestClient], httpx.Response]:
    def send(client: TestClient) -> httpx.Response:
        return client.post(
            f"/api/v1/recommendations/{recommendation_id}/confirm",
            headers=headers(w.b_token),
            json={"expected_version": 1, "target_expected_version": 1},
        )

    return send


@dataclass(frozen=True)
class Outcome:
    response: httpx.Response
    asked: list[UUID]


def _race(w: RaceWorld, recommendation_id: UUID, asked: list[UUID], *, transfer: bool) -> Outcome:
    response, _before = race(
        w,
        table="recommendations",
        row_id=recommendation_id,
        send=_confirm(w, recommendation_id),
        transfer=transfer,
    )
    return Outcome(response, list(asked))


def test_confirm_refuses_when_the_owner_changed_after_the_consent_check(
    world: RaceWorld, consent_checks: list[UUID]
) -> None:
    recommendation_id = _seed(world, recommendation_type="email_action_detected")

    outcome = _race(world, recommendation_id, consent_checks, transfer=True)

    assert outcome.response.status_code == 409, outcome.response.text
    assert outcome.response.json()["error"]["code"] == "RECOMMENDATION_OWNER_CHANGED"
    assert outcome.asked == [world.b]  # the consent was checked for the old owner only
    assert _status(recommendation_id) == "pending_confirmation"
    assert _task_statuses(world) == ["captured"]


def test_retry_after_the_owner_changed_checks_the_new_owners_consent(
    world: RaceWorld, consent_checks: list[UUID]
) -> None:
    recommendation_id = _seed(world, recommendation_type="email_action_detected")
    _race(world, recommendation_id, consent_checks, transfer=True)

    client = TestClient(app)
    client.cookies.set("ecc_session", world.b_token)
    try:
        retry = _confirm(world, recommendation_id)(client)
    finally:
        client.close()

    assert retry.status_code == 403, retry.text
    assert retry.json()["error"]["code"] == EmailConsentInactiveError.code
    assert consent_checks == [world.b, world.c]
    assert _status(recommendation_id) == "pending_confirmation"
    assert _task_statuses(world) == ["captured"]


def test_confirm_without_a_transfer_still_proceeds(
    world: RaceWorld, consent_checks: list[UUID]
) -> None:
    """Control: the same lock wait with no ownership change confirms."""
    recommendation_id = _seed(world, recommendation_type="email_action_detected")

    outcome = _race(world, recommendation_id, consent_checks, transfer=False)

    assert outcome.response.status_code == 200, outcome.response.text
    assert outcome.asked == [world.b]
    assert _status(recommendation_id) == "executed"
    assert _task_statuses(world) == ["in_progress"]


def test_confirm_of_a_non_email_recommendation_ignores_an_owner_change(
    world: RaceWorld, consent_checks: list[UUID]
) -> None:
    """No consent was checked for it, so there is nothing to have gone stale."""
    recommendation_id = _seed(world, recommendation_type="task_priority")

    outcome = _race(world, recommendation_id, consent_checks, transfer=True)

    assert outcome.response.status_code == 200, outcome.response.text
    assert outcome.asked == []
    assert _status(recommendation_id) == "executed"
