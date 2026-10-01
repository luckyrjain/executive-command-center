"""Personal-data writes against a concurrent member removal or role change
(ADR-0014).

`domains/personal/*` does not use `authorize()`: every row is scoped to
the caller's own `workspace_id` + `owner_id`, and no personal endpoint
checks the caller's membership or role at all -- the only thing standing
between a removed member and their personal endpoints is removal revoking
their sessions. A request that authenticated before the removal committed
therefore wrote anyway. Every personal write transaction now takes the
shared membership lock first (`authz.lock_membership_for_write`) with
`role_action="read"`: any active member may write their own personal data
(a demotion to `viewer` does not revoke it), a removed one may not. A
removal holding the lock makes the write wait, and once it commits the
write answers 403 `INSUFFICIENT_ROLE` and writes nothing. See
`membership_lock_race_support` for the harness.

Every seeded row is owned by B, the racing caller.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from membership_lock_race_support import (
    FORBIDDEN,
    Case,
    Change,
    Ids,
    RaceWorld,
    assert_proceeds,
    assert_refused,
    race,
    race_world,
    refusal_params,
    seed,
    seed_nothing,
)
from sqlalchemy import Connection, text

from ecc.config import get_settings
from ecc.domains.engineering.crypto import encrypt_credential
from ecc.domains.personal import ai_insights as ai_insights_module

pytestmark = pytest.mark.skipif(
    not get_settings().database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)

# Children first: the cleanup order for `race_world`.
_SEEDED_TABLES = (
    "deletion_jobs",
    "email_messages",
    "email_threads",
    "connector_accounts",
    "personal_insight_feedback",
    "personal_insights",
    "check_ins",
    "routines",
    "goals",
    "domain_records",
    "domain_sources",
    "cross_domain_grants",
    "domain_consents",
    "personal_domains",
)
_WRITE_TABLES = (
    *_SEEDED_TABLES,
    "audit_events",
    "event_outbox",
    "idempotency_records",
)

# A removed member answers 403 from the in-transaction re-check; a viewer
# still writes their own personal data.
_MEMBER_ONLY: dict[Change, tuple[int, str] | None] = {"demote": None, "remove": FORBIDDEN}


@pytest.fixture
def world() -> Iterator[RaceWorld]:
    with race_world("Personal Membership Race", _SEEDED_TABLES) as w:
        yield w


def _domain(conn: Connection, w: RaceWorld, now: datetime) -> Ids:
    """B's `habits` domain, enabled, with its consent."""
    domain_id, consent_id = uuid4(), uuid4()
    conn.execute(
        text(
            "INSERT INTO personal_domains (id, workspace_id, owner_id, domain_key, "
            "classification, enabled, enabled_at, created_by, updated_by, created_at, "
            "updated_at, version) VALUES (:id, :ws, :b, 'habits', 'standard', true, :now, "
            ":b, :b, :now, :now, 1)"
        ),
        {"id": domain_id, "ws": w.ws, "b": w.b, "now": now},
    )
    conn.execute(
        text(
            "INSERT INTO domain_consents (id, workspace_id, owner_id, domain_key, "
            "granted_at, created_at) VALUES (:id, :ws, :b, 'habits', :now, :now)"
        ),
        {"id": consent_id, "ws": w.ws, "b": w.b, "now": now},
    )
    return {"domain": domain_id, "consent": consent_id}


def _record(conn: Connection, w: RaceWorld, now: datetime) -> Ids:
    ids = _domain(conn, w, now)
    ids["id"] = uuid4()
    conn.execute(
        text(
            "INSERT INTO domain_records (id, workspace_id, owner_id, domain_key, record_type, "
            "payload, effective_at, created_by, updated_by, created_at, updated_at, version) "
            "VALUES (:id, :ws, :b, 'habits', 'habit_summary', CAST('{}' AS jsonb), :now, "
            ":b, :b, :now, :now, 1)"
        ),
        {"id": ids["id"], "ws": w.ws, "b": w.b, "now": now},
    )
    return ids


def _goal(conn: Connection, w: RaceWorld, now: datetime) -> Ids:
    """A goal, so a domain delete has rows to purge."""
    ids = _domain(conn, w, now)
    conn.execute(
        text(
            "INSERT INTO goals (id, workspace_id, owner_id, domain_key, title, created_by, "
            "updated_by, created_at, updated_at) "
            "VALUES (:id, :ws, :b, 'habits', 'Race goal', :b, :b, :now, :now)"
        ),
        {"id": uuid4(), "ws": w.ws, "b": w.b, "now": now},
    )
    return ids


def _grant(conn: Connection, w: RaceWorld, now: datetime) -> Ids:
    ids = _domain(conn, w, now)
    ids["id"] = uuid4()
    conn.execute(
        text(
            "INSERT INTO cross_domain_grants (id, workspace_id, owner_id, source_domain_key, "
            "purpose, granted_categories, granted_at, created_at) VALUES (:id, :ws, :b, "
            "'habits', 'insight_generation', CAST('[\"habit_summary\"]' AS jsonb), :now, :now)"
        ),
        {"id": ids["id"], "ws": w.ws, "b": w.b, "now": now},
    )
    return ids


def _routine(conn: Connection, w: RaceWorld, now: datetime) -> Ids:
    """Created 10 days ago with no check-in: `GET /insights` upserts a gap
    insight for it."""
    ids = _domain(conn, w, now)
    ids["id"] = uuid4()
    conn.execute(
        text(
            "INSERT INTO routines (id, workspace_id, owner_id, domain_key, title, cadence, "
            "created_by, updated_by, created_at, updated_at) VALUES (:id, :ws, :b, 'habits', "
            "'Race routine', 'daily', :b, :b, :then, :then)"
        ),
        {"id": ids["id"], "ws": w.ws, "b": w.b, "then": now - timedelta(days=10)},
    )
    return ids


def _insight(conn: Connection, w: RaceWorld, now: datetime) -> Ids:
    ids = _domain(conn, w, now)
    ids["id"] = uuid4()
    conn.execute(
        text(
            "INSERT INTO personal_insights (id, workspace_id, owner_id, domain_key, "
            "insight_key, kind, title, evidence, source_period_start, source_period_end, "
            "confidence, limitations, computed_at) VALUES (:id, :ws, :b, 'habits', "
            ":key, 'observation', 'Race insight', CAST('{}' AS jsonb), :now, :now, 'high', "
            "'none', :now)"
        ),
        {"id": ids["id"], "ws": w.ws, "b": w.b, "key": f"race:{ids['id']}", "now": now},
    )
    return ids


def _thread(conn: Connection, w: RaceWorld, now: datetime) -> Ids:
    """B's Gmail connector, one thread and one message with a snippet
    (`forget` clears it)."""
    account_id, thread_id = uuid4(), uuid4()
    conn.execute(
        text(
            "INSERT INTO personal_domains (id, workspace_id, owner_id, domain_key, "
            "classification, enabled, enabled_at, created_by, updated_by, created_at, "
            "updated_at, version) VALUES (:id, :ws, :b, 'email', 'high_stakes', true, :now, "
            ":b, :b, :now, :now, 1)"
        ),
        {"id": uuid4(), "ws": w.ws, "b": w.b, "now": now},
    )
    conn.execute(
        text(
            "INSERT INTO connector_accounts (id, workspace_id, provider, external_account_id, "
            "display_name, granted_scopes, encrypted_credentials, status, version, created_by, "
            "updated_by, created_at, updated_at, owner_id, visibility) VALUES (:id, :ws, "
            "'gmail', :external, 'Race account', "
            "ARRAY['https://www.googleapis.com/auth/gmail.readonly'], :cred, 'active', 1, "
            ":b, :b, :now, :now, :b, 'private')"
        ),
        {
            "id": account_id,
            "ws": w.ws,
            "external": f"{uuid4()}@example.test",
            "cred": encrypt_credential("race-credential"),
            "b": w.b,
            "now": now,
        },
    )
    conn.execute(
        text(
            "INSERT INTO email_threads (id, workspace_id, owner_id, domain_key, "
            "connector_account_id, external_thread_id, subject, last_message_at, created_at, "
            "updated_at) VALUES (:id, :ws, :b, 'email', :account, :external, NULL, :now, "
            ":now, :now)"
        ),
        {
            "id": thread_id,
            "ws": w.ws,
            "b": w.b,
            "account": account_id,
            "external": f"race-{thread_id}",
            "now": now,
        },
    )
    conn.execute(
        text(
            "INSERT INTO email_messages (id, workspace_id, owner_id, thread_id, "
            "external_message_id, sender, recipients, sent_at, direction, snippet, "
            "created_at, updated_at) VALUES (:id, :ws, :b, :thread, :external, "
            "'someone@example.test', ARRAY['b@example.test'], :now, 'inbound', 'hello', "
            ":now, :now)"
        ),
        {
            "id": uuid4(),
            "ws": w.ws,
            "b": w.b,
            "thread": thread_id,
            "external": f"race-msg-{thread_id}",
            "now": now,
        },
    )
    return {"id": thread_id}


def _empty(_: Ids) -> dict[str, Any]:
    return {}


CASES: dict[str, Case] = {
    "domain_enable": Case(
        seed_nothing,
        "POST",
        "/api/v1/personal/domains",
        lambda _: {"domain_key": "habits"},
        201,
        _MEMBER_ONLY,
    ),
    "domain_disable": Case(
        _domain, "POST", "/api/v1/personal/domains/habits/disable", _empty, 200, _MEMBER_ONLY
    ),
    "consent_revoke": Case(
        _domain,
        "POST",
        "/api/v1/personal/consents/{consent}/revoke",
        _empty,
        200,
        _MEMBER_ONLY,
    ),
    "domain_delete": Case(
        _goal, "POST", "/api/v1/personal/domains/habits/delete", _empty, 200, _MEMBER_ONLY
    ),
    "record_create": Case(
        _domain,
        "POST",
        "/api/v1/personal/records",
        lambda _: {"domain_key": "habits", "record_type": "habit_summary", "payload": {"a": 1}},
        201,
        _MEMBER_ONLY,
    ),
    "record_update": Case(
        _record,
        "PATCH",
        "/api/v1/personal/records/{id}",
        lambda _: {"expected_version": 1, "payload": {"a": 2}},
        200,
        _MEMBER_ONLY,
    ),
    "grant_create": Case(
        _domain,
        "POST",
        "/api/v1/personal/grants",
        lambda _: {
            "source_domain_key": "habits",
            "purpose": "insight_generation",
            "granted_categories": ["habit_summary"],
        },
        201,
        _MEMBER_ONLY,
    ),
    "grant_revoke": Case(
        _grant, "POST", "/api/v1/personal/grants/{id}/revoke", _empty, 200, _MEMBER_ONLY
    ),
    "goal_create": Case(
        _domain,
        "POST",
        "/api/v1/personal/goals",
        lambda _: {"domain_key": "habits", "title": "Race goal"},
        201,
        _MEMBER_ONLY,
    ),
    "routine_create": Case(
        _domain,
        "POST",
        "/api/v1/personal/routines",
        lambda _: {"domain_key": "habits", "title": "Race routine", "cadence": "daily"},
        201,
        _MEMBER_ONLY,
    ),
    "check_in_create": Case(
        _routine,
        "POST",
        "/api/v1/personal/check-ins",
        lambda ids: {"routine_id": str(ids["id"])},
        201,
        _MEMBER_ONLY,
    ),
    "insights_list": Case(
        _routine, "GET", "/api/v1/personal/insights", lambda _: None, 200, _MEMBER_ONLY
    ),
    "insight_dismiss": Case(
        _insight, "POST", "/api/v1/personal/insights/{id}/dismiss", _empty, 200, _MEMBER_ONLY
    ),
    "insight_feedback": Case(
        _insight,
        "POST",
        "/api/v1/personal/insights/{id}/feedback",
        lambda _: {"useful": True},
        201,
        _MEMBER_ONLY,
    ),
    "thread_forget": Case(
        _thread, "POST", "/api/v1/personal/gmail/threads/{id}/forget", _empty, 200, _MEMBER_ONLY
    ),
    # The model call is stubbed (`_stub_model` below); only the final
    # write transaction, after it, takes the lock.
    "insight_generate": Case(
        _record,
        "POST",
        "/api/v1/personal/insights/generate",
        lambda _: {"source_domain_keys": ["habits"]},
        200,
        _MEMBER_ONLY,
    ),
}


@pytest.fixture(autouse=True)
def _stub_model(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """`insight_generate` with generation on and `execute_run` replaced by a
    completed run, so no model is called and no `ai_runs` row is written
    (that persist belongs to `ai_runtime`, outside this domain)."""
    monkeypatch.setenv("ECC_PERSONAL_AI_INSIGHT_GENERATION_ENABLED", "true")
    get_settings.cache_clear()

    def completed_run(*_args: Any, **_kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(
            id=uuid4(),
            status="completed",
            error_code=None,
            output={
                "kind": "trend",
                "title": "Race trend",
                "cited_record_ids": [],
                "source_period": "the past week",
                "missing_data": None,
                "confidence": "medium",
                "limitations": "Stubbed.",
                "professional_referral_note": None,
            },
        )

    monkeypatch.setattr(ai_insights_module, "execute_run", completed_run)
    try:
        yield
    finally:
        monkeypatch.delenv("ECC_PERSONAL_AI_INSIGHT_GENERATION_ENABLED")
        get_settings.cache_clear()


@pytest.mark.parametrize(("name", "change"), refusal_params(CASES))
def test_personal_write_waiting_on_membership_lock_rechecks_membership(
    world: RaceWorld, name: str, change: Change
) -> None:
    case = CASES[name]
    assert_refused(world, case, seed(world, case), change, _WRITE_TABLES)


@pytest.mark.parametrize("name", list(CASES))
def test_personal_write_waiting_on_membership_lock_without_change_still_proceeds(
    world: RaceWorld, name: str
) -> None:
    case = CASES[name]
    assert_proceeds(world, case, seed(world, case))


@pytest.mark.parametrize("name", list(CASES))
def test_viewer_demotion_does_not_block_own_personal_write(world: RaceWorld, name: str) -> None:
    """`role_action="read"`: personal data is the caller's own, so a viewer
    may still write it; the write still waits for the demotion."""
    case = CASES[name]
    response, blocked = race(world, case, seed(world, case), change="demote")

    assert response.status_code == case.ok_status, response.text
    assert blocked
