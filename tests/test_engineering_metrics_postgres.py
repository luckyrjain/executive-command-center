"""Phase 6 Engineering Workspace Task 5: delivery and reliability metrics
(`docs/superpowers/plans/2026-07-27-phase-6-engineering-workspace.md`,
`backend/ecc/domains/engineering/metrics.py`).

Covers, per this task's own scope (see `metrics.py`'s own module
docstring for the full "four of seven metrics are real as of Task 6"
design):

1. `_coverage_for`: insufficient (no connected account), insufficient
   (no source resource type at all -- empty `providers`), partial (one
   of two accounts freshly synced), complete (all fresh).
2. `work_ageing`/`blocked_work`: correct population (open items only,
   closed items excluded), correct bucketing/median age, blocked-status
   heuristic, missing-`provider_created_at` items excluded from the
   age calculation but still counted in `population`.
3. `review_latency`: correct median (requested_at to first submitted_at
   per change), a change with no `requested_at` excluded, a merge
   outside the 30-day window excluded.
4. `delivery_frequency`/`lead_time_for_changes`/`change_failure_rate`:
   always `insufficient_coverage`, `value=None` -- no deployment sync
   exists yet. `time_to_restore` (Task 6) is real -- see the dedicated
   `time_to_restore` section below, not always `insufficient_coverage`.
5. `compute_and_store_metrics`: writes seven immutable rows every call
   (insert-only -- calling it twice produces fourteen rows, never
   updates the first seven).
6. End-to-end through the real `GET /engineering/metrics` endpoint.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from hmac import new as hmac_new
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from ecc.config import get_settings
from ecc.database import SessionFactory, engine
from ecc.domains.engineering.metrics import (
    _bucket_ages_days,
    _compute_review_latency,
    _compute_time_to_restore,
    compute_and_store_metrics,
    compute_metrics,
)
from ecc.main import app

settings = get_settings()
pytestmark = pytest.mark.skipif(
    not settings.database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)


@pytest.fixture
def workspace() -> Iterator[UUID]:
    workspace_id = uuid4()
    user_id = uuid4()
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, 'Metrics Unit Test', 'UTC', :now)"
            ),
            {"id": workspace_id, "now": now},
        )
        connection.execute(
            text(
                "INSERT INTO users (id, workspace_id, email, password_hash, created_at) "
                "VALUES (:id, :workspace_id, :email, 'test-password-hash', :now)"
            ),
            {
                "id": user_id,
                "workspace_id": workspace_id,
                "email": f"{user_id}@example.test",
                "now": now,
            },
        )
    try:
        yield workspace_id
    finally:
        with engine.begin() as connection:
            for table in (
                "delivery_metric_snapshots",
                "incident_changes",
                "incidents",
                "reviews",
                "changes",
                "engineering_work_items",
                "repositories",
                "sync_cursors",
                "connector_accounts",
                "users",
            ):
                connection.execute(
                    text(f"DELETE FROM {table} WHERE workspace_id = :workspace_id"),  # noqa: S608
                    {"workspace_id": workspace_id},
                )
            connection.execute(
                text("DELETE FROM workspaces WHERE id = :workspace_id"),
                {"workspace_id": workspace_id},
            )


def _insert_connector_account(workspace_id: UUID, *, provider: str, status: str = "active") -> UUID:
    account_id = uuid4()
    now = datetime.now(UTC)
    with engine.begin() as connection:
        user_id = connection.execute(
            text("SELECT id FROM users WHERE workspace_id = :ws"), {"ws": workspace_id}
        ).scalar_one()
        connection.execute(
            text(
                """
                INSERT INTO connector_accounts (
                    id, workspace_id, provider, external_account_id, display_name,
                    granted_scopes, encrypted_credentials, status, version,
                    created_by, updated_by, created_at, updated_at
                ) VALUES (
                    :id, :workspace_id, :provider, :ext, 'Fixture account',
                    ARRAY[]::text[], :cred, :status, 1, :actor, :actor, :now, :now
                )
                """
            ),
            {
                "id": account_id,
                "workspace_id": workspace_id,
                "provider": provider,
                "ext": f"{provider}-acct-{account_id}",
                "cred": b"fake",
                "status": status,
                "actor": user_id,
                "now": now,
            },
        )
    return account_id


def _insert_cursor(
    workspace_id: UUID, account_id: UUID, *, resource_type: str, updated_at: datetime
) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO sync_cursors (id, workspace_id, connector_account_id, "
                "resource_type, cursor_value, updated_at) "
                "VALUES (:id, :ws, :acct, :rt, 'x', :updated_at)"
            ),
            {
                "id": uuid4(),
                "ws": workspace_id,
                "acct": account_id,
                "rt": resource_type,
                "updated_at": updated_at,
            },
        )


def _insert_work_item(
    workspace_id: UUID,
    account_id: UUID,
    *,
    external_id: str,
    status: str,
    provider_created_at: datetime | None,
) -> None:
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO engineering_work_items (
                    id, workspace_id, connector_account_id, provider, external_id,
                    title, source_url, item_type, status, permission_state, freshness_state,
                    provider_created_at, observed_at, created_at, updated_at
                ) VALUES (
                    :id, :ws, :acct, 'jira', :ext, :title, 'https://x', 'Bug', :status,
                    'active', 'fresh', :created, :now, :now, :now
                )
                """
            ),
            {
                "id": uuid4(),
                "ws": workspace_id,
                "acct": account_id,
                "ext": external_id,
                "title": external_id,
                "status": status,
                "created": provider_created_at,
                "now": now,
            },
        )


def _insert_repo(workspace_id: UUID, account_id: UUID, *, external_id: str) -> UUID:
    repository_id = uuid4()
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO repositories (
                    id, workspace_id, connector_account_id, provider, external_id,
                    name, source_url, default_branch, permission_state, freshness_state,
                    observed_at, created_at, updated_at
                ) VALUES (
                    :id, :ws, :acct, 'github', :ext, :name, 'https://x', 'main',
                    'active', 'fresh', :now, :now, :now
                )
                """
            ),
            {
                "id": repository_id,
                "ws": workspace_id,
                "acct": account_id,
                "ext": external_id,
                "name": f"acme/{external_id}",
                "now": now,
            },
        )
    return repository_id


def _insert_change(
    workspace_id: UUID,
    account_id: UUID,
    repository_id: UUID,
    *,
    external_id: str,
    merged_at: datetime,
) -> UUID:
    change_id = uuid4()
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO changes (
                    id, workspace_id, connector_account_id, repository_id, provider,
                    external_id, provider_number, title, source_url, status, merged_at,
                    permission_state, freshness_state, observed_at, created_at, updated_at
                ) VALUES (
                    :id, :ws, :acct, :repo, 'github', :ext, :num, 'A change', 'https://x',
                    'closed', :merged_at, 'active', 'fresh', :now, :now, :now
                )
                """
            ),
            {
                "id": change_id,
                "ws": workspace_id,
                "acct": account_id,
                "repo": repository_id,
                "ext": external_id,
                "num": external_id,
                "merged_at": merged_at,
                "now": now,
            },
        )
    return change_id


def _insert_review(
    workspace_id: UUID,
    account_id: UUID,
    change_id: UUID,
    *,
    external_id: str,
    requested_at: datetime | None,
    submitted_at: datetime,
) -> None:
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO reviews (
                    id, workspace_id, connector_account_id, change_id, provider,
                    external_id, source_url, reviewer_external_id, review_state,
                    requested_at, submitted_at, permission_state, freshness_state,
                    observed_at, created_at, updated_at
                ) VALUES (
                    :id, :ws, :acct, :change, 'github', :ext, 'https://x', '1', 'approved',
                    :requested_at, :submitted_at, 'active', 'fresh', :now, :now, :now
                )
                """
            ),
            {
                "id": uuid4(),
                "ws": workspace_id,
                "acct": account_id,
                "change": change_id,
                "ext": external_id,
                "requested_at": requested_at,
                "submitted_at": submitted_at,
                "now": now,
            },
        )


# --- coverage --------------------------------------------------------------


def test_coverage_insufficient_when_no_connected_account(workspace: UUID) -> None:
    with SessionFactory() as session:
        snapshots = compute_metrics(session, workspace_id=workspace)
    work_ageing = next(s for s in snapshots if s.metric_key == "work_ageing")
    assert work_ageing.coverage_status == "insufficient_coverage"
    assert work_ageing.coverage_percentage == 0.0
    assert work_ageing.coverage_gap_description is not None


def test_coverage_complete_when_cursor_fresh(workspace: UUID) -> None:
    account_id = _insert_connector_account(workspace, provider="jira")
    _insert_cursor(workspace, account_id, resource_type="work_item", updated_at=datetime.now(UTC))
    with SessionFactory() as session:
        snapshots = compute_metrics(session, workspace_id=workspace)
    work_ageing = next(s for s in snapshots if s.metric_key == "work_ageing")
    assert work_ageing.coverage_status == "complete"
    assert work_ageing.coverage_percentage == 100.0
    assert work_ageing.coverage_gap_description is None


def test_coverage_partial_when_one_of_two_accounts_stale(workspace: UUID) -> None:
    fresh_account = _insert_connector_account(workspace, provider="jira")
    stale_account = _insert_connector_account(workspace, provider="jira")
    _insert_cursor(
        workspace, fresh_account, resource_type="work_item", updated_at=datetime.now(UTC)
    )
    _insert_cursor(
        workspace,
        stale_account,
        resource_type="work_item",
        updated_at=datetime.now(UTC) - timedelta(hours=1),
    )
    with SessionFactory() as session:
        snapshots = compute_metrics(session, workspace_id=workspace)
    work_ageing = next(s for s in snapshots if s.metric_key == "work_ageing")
    assert work_ageing.coverage_status == "partial"
    assert work_ageing.coverage_percentage == 50.0
    assert work_ageing.coverage_gap_description is not None


def test_coverage_complete_at_exact_95_percent_boundary(workspace: UUID) -> None:
    """`_coverage_for` gates on `percentage >= _COMPLETE_COVERAGE_THRESHOLD`
    (95.0) -- the only prior tests exercise 100% ("clearly above") and 50%
    (the *partial* threshold), never this exact value. A regression here
    (e.g. `>` instead of `>=`, or a stray 94.0/96.0 typo) would silently
    misclassify a borderline-fresh workspace, the same metric-gating logic
    this task's own review history already found one real shipped bug in.
    """
    now = datetime.now(UTC)
    accounts = [_insert_connector_account(workspace, provider="jira") for _ in range(20)]
    for account_id in accounts[:19]:
        _insert_cursor(workspace, account_id, resource_type="work_item", updated_at=now)
    with SessionFactory() as session:
        snapshots = compute_metrics(session, workspace_id=workspace)
    work_ageing = next(s for s in snapshots if s.metric_key == "work_ageing")
    assert work_ageing.coverage_percentage == 95.0
    assert work_ageing.coverage_status == "complete"
    assert work_ageing.coverage_gap_description is None


def test_coverage_partial_just_under_95_percent_boundary(workspace: UUID) -> None:
    """The mirror of the test above: one connector short of the 95%
    threshold (18 of 19, ~94.7%) must still report `partial`, not
    `complete` -- proving the gate is genuinely `>=`, not merely correct
    at round numbers like 100%/50%.
    """
    now = datetime.now(UTC)
    accounts = [_insert_connector_account(workspace, provider="jira") for _ in range(19)]
    for account_id in accounts[:18]:
        _insert_cursor(workspace, account_id, resource_type="work_item", updated_at=now)
    with SessionFactory() as session:
        snapshots = compute_metrics(session, workspace_id=workspace)
    work_ageing = next(s for s in snapshots if s.metric_key == "work_ageing")
    assert work_ageing.coverage_percentage < 95.0
    assert work_ageing.coverage_status == "partial"
    assert work_ageing.coverage_gap_description is not None


def test_coverage_ignores_inactive_accounts(workspace: UUID) -> None:
    _insert_connector_account(workspace, provider="jira", status="disconnected")
    with SessionFactory() as session:
        snapshots = compute_metrics(session, workspace_id=workspace)
    work_ageing = next(s for s in snapshots if s.metric_key == "work_ageing")
    assert work_ageing.coverage_status == "insufficient_coverage"


def test_insufficient_coverage_nulls_the_value_even_when_previously_synced_rows_exist(
    workspace: UUID,
) -> None:
    """Final Phase 6 review finding: `_coverage_for` and the population
    queries (`_open_work_items`, the `reviews`/`changes` join) are
    computed independently -- nothing previously nulled `value`/
    `numerator`/`denominator`/`details` once coverage regressed to
    `insufficient_coverage`, even though `population` (and thus a real
    computed value) can still be nonzero from rows synced before the
    connector was disconnected or its cursor went stale. This is exactly
    the scenario a disconnected-but-previously-synced connector (or a
    merely stale sync cursor with no scheduler to refresh it) produces --
    the frontend's own `MetricCard.tsx`/`DeliveryPanel.tsx` both assert no
    number is ever shown below 50% coverage, so the backend must actually
    guarantee it, not just usually happen to.
    """
    # An active connector account with NO fresh sync cursor at all --
    # `_coverage_for` reports `insufficient_coverage`/0.0%, identical to
    # what a disconnected-but-previously-synced or merely stale-cursor
    # connector produces, while these previously-synced rows still exist.
    account_id = _insert_connector_account(workspace, provider="jira")
    now = datetime.now(UTC)
    _insert_work_item(
        workspace,
        account_id,
        external_id="open-3d",
        status="In Progress",
        provider_created_at=now - timedelta(days=3),
    )
    _insert_work_item(
        workspace,
        account_id,
        external_id="blocked-40d",
        status="Blocked",
        provider_created_at=now - timedelta(days=40),
    )

    with SessionFactory() as session:
        snapshots = compute_metrics(session, workspace_id=workspace)

    work_ageing = next(s for s in snapshots if s.metric_key == "work_ageing")
    assert work_ageing.coverage_status == "insufficient_coverage"
    assert work_ageing.population == 2  # population is still real and nonzero
    assert work_ageing.value is None
    assert work_ageing.details is None

    blocked_work = next(s for s in snapshots if s.metric_key == "blocked_work")
    assert blocked_work.coverage_status == "insufficient_coverage"
    assert blocked_work.population == 2
    assert blocked_work.value is None
    assert blocked_work.numerator is None
    assert blocked_work.denominator is None
    assert blocked_work.details is None


def test_review_latency_insufficient_coverage_nulls_the_value(workspace: UUID) -> None:
    account_id = _insert_connector_account(workspace, provider="github")
    repo_id = _insert_repo(workspace, account_id, external_id="101")
    now = datetime.now(UTC)
    change_id = _insert_change(
        workspace, account_id, repo_id, external_id="1", merged_at=now - timedelta(days=1)
    )
    _insert_review(
        workspace,
        account_id,
        change_id,
        external_id="r1",
        requested_at=now - timedelta(hours=5),
        submitted_at=now - timedelta(hours=3),
    )
    # No `change`/`review` sync cursor at all -- insufficient_coverage,
    # even though a real review-latency population exists.

    with SessionFactory() as session:
        snapshots = compute_metrics(session, workspace_id=workspace)
    review_latency = next(s for s in snapshots if s.metric_key == "review_latency")
    assert review_latency.coverage_status == "insufficient_coverage"
    assert review_latency.population == 1
    assert review_latency.value is None


# --- work_ageing / blocked_work --------------------------------------------


def test_work_ageing_and_blocked_work_computation(workspace: UUID) -> None:
    account_id = _insert_connector_account(workspace, provider="jira")
    _insert_cursor(workspace, account_id, resource_type="work_item", updated_at=datetime.now(UTC))
    now = datetime.now(UTC)
    _insert_work_item(
        workspace,
        account_id,
        external_id="open-3d",
        status="In Progress",
        provider_created_at=now - timedelta(days=3),
    )
    _insert_work_item(
        workspace,
        account_id,
        external_id="blocked-40d",
        status="Blocked",
        provider_created_at=now - timedelta(days=40),
    )
    _insert_work_item(
        workspace,
        account_id,
        external_id="done-100d",
        status="Done",
        provider_created_at=now - timedelta(days=100),
    )

    with SessionFactory() as session:
        snapshots = compute_metrics(session, workspace_id=workspace)

    work_ageing = next(s for s in snapshots if s.metric_key == "work_ageing")
    assert work_ageing.population == 2  # "done" excluded
    assert work_ageing.value is not None
    assert 21 <= work_ageing.value <= 22  # median of ~3 and ~40 days
    assert work_ageing.details is not None
    assert work_ageing.details["buckets"]["0-7d"] == 1
    assert work_ageing.details["buckets"]["31-90d"] == 1

    blocked_work = next(s for s in snapshots if s.metric_key == "blocked_work")
    assert blocked_work.population == 2
    assert blocked_work.value == 1.0
    assert blocked_work.details is not None
    assert 39 <= blocked_work.details["median_age_days"] <= 41


def test_work_ageing_excludes_items_from_a_disconnected_sibling_account(workspace: UUID) -> None:
    """Review found that with two-or-more same-provider connector
    accounts, once one goes `disconnected` (its rows' `freshness_state`
    frozen forever, since the disable cascade only fires on an explicit
    disconnect) while a sibling account stays `active` and fresh,
    `_coverage_for` correctly excludes the disconnected account from its
    own denominator -- but `_open_work_items` had no equivalent
    `connector_accounts.status` scoping at all, so the disconnected
    account's permanently-stale work items still silently contributed to
    `population`/`value` under a `complete` coverage badge. Proven here:
    a `disconnected` account's still-`permission_state='active'` open
    item must not be counted at all once its account is disconnected.
    """
    active_account_id = _insert_connector_account(workspace, provider="jira")
    disconnected_account_id = _insert_connector_account(
        workspace, provider="jira", status="disconnected"
    )
    _insert_cursor(
        workspace, active_account_id, resource_type="work_item", updated_at=datetime.now(UTC)
    )
    now = datetime.now(UTC)
    _insert_work_item(
        workspace,
        active_account_id,
        external_id="active-open",
        status="In Progress",
        provider_created_at=now - timedelta(days=3),
    )
    _insert_work_item(
        workspace,
        disconnected_account_id,
        external_id="disconnected-open",
        status="In Progress",
        provider_created_at=now - timedelta(days=200),
    )

    with SessionFactory() as session:
        snapshots = compute_metrics(session, workspace_id=workspace)

    work_ageing = next(s for s in snapshots if s.metric_key == "work_ageing")
    assert work_ageing.coverage_status == "complete"
    assert work_ageing.population == 1
    assert work_ageing.value is not None
    assert 2 <= work_ageing.value <= 4


def test_bucket_ages_days_boundaries_are_inclusive_on_the_low_side() -> None:
    """`_bucket_ages_days` uses `<=` at each boundary -- an item aged
    exactly 7/30/90 days must land in the *lower* bucket (`0-7d`/
    `8-30d`/`31-90d`), not the next one up. Tested directly against exact
    float boundaries (not through the full DB round-trip, which would be
    subject to a real timing gap between this test's own `now` and
    `compute_metrics`'s internal `datetime.now(UTC)` call skewing an
    exactly-7.0-day item to just over the boundary) -- off-by-one
    boundary bugs are common enough here to warrant a precise,
    deterministic test rather than relying on the ~3d/~40d/~100d values
    the main computation test above uses, none of which land exactly on
    a boundary.
    """
    buckets = _bucket_ages_days([7.0, 30.0, 90.0, 90.1, 6.9])
    assert buckets == {"0-7d": 2, "8-30d": 1, "31-90d": 1, "90d+": 1}


def test_work_ageing_counts_items_missing_created_at_separately(workspace: UUID) -> None:
    account_id = _insert_connector_account(workspace, provider="jira")
    _insert_cursor(workspace, account_id, resource_type="work_item", updated_at=datetime.now(UTC))
    _insert_work_item(
        workspace,
        account_id,
        external_id="no-created-at",
        status="Open",
        provider_created_at=None,
    )
    with SessionFactory() as session:
        snapshots = compute_metrics(session, workspace_id=workspace)
    work_ageing = next(s for s in snapshots if s.metric_key == "work_ageing")
    assert work_ageing.population == 1
    assert work_ageing.value is None
    assert work_ageing.details is not None
    assert work_ageing.details["items_missing_created_at"] == 1


# --- review_latency ----------------------------------------------------------


def test_review_latency_median_of_first_review_per_change(workspace: UUID) -> None:
    account_id = _insert_connector_account(workspace, provider="github")
    _insert_cursor(workspace, account_id, resource_type="change", updated_at=datetime.now(UTC))
    _insert_cursor(workspace, account_id, resource_type="review", updated_at=datetime.now(UTC))
    repo_id = _insert_repo(workspace, account_id, external_id="101")
    now = datetime.now(UTC)

    change1 = _insert_change(
        workspace, account_id, repo_id, external_id="1", merged_at=now - timedelta(days=1)
    )
    _insert_review(
        workspace,
        account_id,
        change1,
        external_id="r1",
        requested_at=now - timedelta(hours=5),
        submitted_at=now - timedelta(hours=3),
    )
    # A second, later review on the same change must not count -- only
    # the first (earliest submitted_at) is "first-review-at".
    _insert_review(
        workspace,
        account_id,
        change1,
        external_id="r2",
        requested_at=now - timedelta(hours=5),
        submitted_at=now - timedelta(hours=1),
    )

    change2 = _insert_change(
        workspace, account_id, repo_id, external_id="2", merged_at=now - timedelta(days=2)
    )
    _insert_review(
        workspace,
        account_id,
        change2,
        external_id="r3",
        requested_at=now - timedelta(hours=10),
        submitted_at=now - timedelta(hours=8),
    )

    with SessionFactory() as session:
        snapshots = compute_metrics(session, workspace_id=workspace)
    review_latency = next(s for s in snapshots if s.metric_key == "review_latency")
    assert review_latency.population == 2
    assert review_latency.value == timedelta(hours=2).total_seconds()


def test_review_latency_coverage_is_the_worse_of_change_and_review_cursors(
    workspace: UUID,
) -> None:
    """`review_latency`'s population depends on both `change` and
    `review` sync being current -- `_worse_coverage` must report the
    lower of the two, not just whichever one happens to be fresh. Proven
    in both directions: `change` cursor fresh but `review` cursor
    missing (insufficient), and the reverse.
    """
    account_id = _insert_connector_account(workspace, provider="github")
    _insert_cursor(workspace, account_id, resource_type="change", updated_at=datetime.now(UTC))
    # No `review` cursor at all -- that resource type's own coverage is
    # insufficient, and must win over `change`'s complete coverage.

    with SessionFactory() as session:
        snapshots = compute_metrics(session, workspace_id=workspace)
    review_latency = next(s for s in snapshots if s.metric_key == "review_latency")
    assert review_latency.coverage_status == "insufficient_coverage"
    assert review_latency.coverage_percentage == 0.0


def test_review_latency_coverage_is_complete_only_when_both_cursors_are_fresh(
    workspace: UUID,
) -> None:
    account_id = _insert_connector_account(workspace, provider="github")
    _insert_cursor(workspace, account_id, resource_type="change", updated_at=datetime.now(UTC))
    _insert_cursor(workspace, account_id, resource_type="review", updated_at=datetime.now(UTC))

    with SessionFactory() as session:
        snapshots = compute_metrics(session, workspace_id=workspace)
    review_latency = next(s for s in snapshots if s.metric_key == "review_latency")
    assert review_latency.coverage_status == "complete"
    assert review_latency.coverage_percentage == 100.0


def test_review_latency_excludes_changes_without_requested_at(workspace: UUID) -> None:
    account_id = _insert_connector_account(workspace, provider="github")
    repo_id = _insert_repo(workspace, account_id, external_id="101")
    now = datetime.now(UTC)
    change_id = _insert_change(
        workspace, account_id, repo_id, external_id="1", merged_at=now - timedelta(days=1)
    )
    _insert_review(
        workspace,
        account_id,
        change_id,
        external_id="r1",
        requested_at=None,
        submitted_at=now - timedelta(hours=3),
    )
    with SessionFactory() as session:
        snapshots = compute_metrics(session, workspace_id=workspace)
    review_latency = next(s for s in snapshots if s.metric_key == "review_latency")
    assert review_latency.population == 0
    assert review_latency.value is None


def test_review_latency_excludes_changes_outside_window(workspace: UUID) -> None:
    account_id = _insert_connector_account(workspace, provider="github")
    repo_id = _insert_repo(workspace, account_id, external_id="101")
    now = datetime.now(UTC)
    change_id = _insert_change(
        workspace, account_id, repo_id, external_id="1", merged_at=now - timedelta(days=45)
    )
    _insert_review(
        workspace,
        account_id,
        change_id,
        external_id="r1",
        requested_at=now - timedelta(days=45, hours=5),
        submitted_at=now - timedelta(days=45, hours=3),
    )
    with SessionFactory() as session:
        snapshots = compute_metrics(session, workspace_id=workspace)
    review_latency = next(s for s in snapshots if s.metric_key == "review_latency")
    assert review_latency.population == 0


def test_review_latency_excludes_negative_latency_rows(workspace: UUID) -> None:
    """A row where `submitted_at` precedes `requested_at` (a data-integrity
    edge case, not expected in practice) must be dropped from both
    `population` and the median, not merely left with a negative
    contribution -- review found this filter had zero test coverage.
    """
    account_id = _insert_connector_account(workspace, provider="github")
    repo_id = _insert_repo(workspace, account_id, external_id="101")
    now = datetime.now(UTC)
    change_id = _insert_change(
        workspace, account_id, repo_id, external_id="1", merged_at=now - timedelta(days=1)
    )
    _insert_review(
        workspace,
        account_id,
        change_id,
        external_id="r1",
        requested_at=now - timedelta(hours=1),
        submitted_at=now - timedelta(hours=3),
    )
    with SessionFactory() as session:
        snapshots = compute_metrics(session, workspace_id=workspace)
    review_latency = next(s for s in snapshots if s.metric_key == "review_latency")
    assert review_latency.population == 0
    assert review_latency.value is None


def test_review_latency_window_boundary_is_inclusive(workspace: UUID) -> None:
    """`_compute_review_latency`'s query filters `c.merged_at &gt;=
    window_start`, so a change merged exactly 30 days ago must be
    *included*, not excluded by an off-by-one `&gt;` instead of `&gt;=`
    -- the same boundary-coverage gap already closed once for
    `_compute_time_to_restore`'s identical 30-day window (see
    `test_time_to_restore_window_boundary_is_inclusive`), never carried
    over to this metric's own independent window. Called directly with
    an explicit `now` rather than through `compute_metrics`'s own
    internal `datetime.now(UTC)` call, avoiding a timing-skew false
    boundary.
    """
    account_id = _insert_connector_account(workspace, provider="github")
    _insert_cursor(workspace, account_id, resource_type="change", updated_at=datetime.now(UTC))
    _insert_cursor(workspace, account_id, resource_type="review", updated_at=datetime.now(UTC))
    repo_id = _insert_repo(workspace, account_id, external_id="101")
    now = datetime.now(UTC)
    window_start = now - timedelta(days=30)
    change_id = _insert_change(
        workspace, account_id, repo_id, external_id="1", merged_at=window_start
    )
    _insert_review(
        workspace,
        account_id,
        change_id,
        external_id="r1",
        requested_at=window_start - timedelta(hours=5),
        submitted_at=window_start - timedelta(hours=3),
    )
    with SessionFactory() as session:
        snapshot = _compute_review_latency(session, workspace_id=workspace, now=now)
    assert snapshot.population == 1
    assert snapshot.value == timedelta(hours=2).total_seconds()


def _insert_incident(
    workspace_id: UUID,
    *,
    status: str,
    detected_at: datetime,
    resolved_at: datetime | None,
) -> UUID:
    incident_id = uuid4()
    now = datetime.now(UTC)
    with engine.begin() as connection:
        user_id = connection.execute(
            text("SELECT id FROM users WHERE workspace_id = :ws"), {"ws": workspace_id}
        ).scalar_one()
        connection.execute(
            text(
                """
                INSERT INTO incidents (
                    id, workspace_id, title, severity, status, detected_at,
                    resolved_at, version, created_by, updated_by, created_at, updated_at
                ) VALUES (
                    :id, :ws, 'An incident', 'high', :status, :detected_at,
                    :resolved_at, 1, :actor, :actor, :now, :now
                )
                """
            ),
            {
                "id": incident_id,
                "ws": workspace_id,
                "status": status,
                "detected_at": detected_at,
                "resolved_at": resolved_at,
                "actor": user_id,
                "now": now,
            },
        )
    return incident_id


# --- time_to_restore ---------------------------------------------------------


def test_time_to_restore_median_of_resolved_incidents_in_window(workspace: UUID) -> None:
    now = datetime.now(UTC)
    _insert_incident(
        workspace,
        status="resolved",
        detected_at=now - timedelta(days=1, hours=2),
        resolved_at=now - timedelta(days=1),
    )
    _insert_incident(
        workspace,
        status="resolved",
        detected_at=now - timedelta(days=2, hours=6),
        resolved_at=now - timedelta(days=2),
    )
    # Still open -- must not contribute to the population or the median.
    _insert_incident(
        workspace, status="open", detected_at=now - timedelta(hours=1), resolved_at=None
    )
    # Resolved, but outside the 30-day window -- must be excluded too.
    _insert_incident(
        workspace,
        status="resolved",
        detected_at=now - timedelta(days=45, hours=3),
        resolved_at=now - timedelta(days=45),
    )

    with SessionFactory() as session:
        snapshots = compute_metrics(session, workspace_id=workspace)
    time_to_restore = next(s for s in snapshots if s.metric_key == "time_to_restore")
    assert time_to_restore.population == 2
    assert time_to_restore.value == timedelta(hours=4).total_seconds()
    assert time_to_restore.coverage_status == "complete"
    assert time_to_restore.coverage_percentage == 100.0


def test_time_to_restore_window_boundary_is_inclusive(workspace: UUID) -> None:
    """`_compute_time_to_restore`'s query filters `resolved_at &gt;=
    window_start`, so an incident resolved exactly 30 days ago must be
    *included*, not excluded by an off-by-one `&gt;` instead of `&gt;=`.
    Called directly with an explicit `now` (the function accepts one)
    rather than through `compute_metrics`'s own internal `datetime.now(UTC)`
    call, avoiding the exact kind of timing-skew false boundary this
    codebase's own `_bucket_ages_days` test already had to work around.
    """
    now = datetime.now(UTC)
    window_start = now - timedelta(days=30)
    _insert_incident(
        workspace,
        status="resolved",
        detected_at=window_start - timedelta(hours=3),
        resolved_at=window_start,
    )
    with SessionFactory() as session:
        snapshot = _compute_time_to_restore(session, workspace_id=workspace, now=now)
    assert snapshot.population == 1
    assert snapshot.value == timedelta(hours=3).total_seconds()


def test_time_to_restore_is_workspace_isolated(workspace: UUID) -> None:
    other_workspace_id = uuid4()
    other_user_id = uuid4()
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, 'Other Workspace', 'UTC', :now)"
            ),
            {"id": other_workspace_id, "now": now},
        )
        connection.execute(
            text(
                "INSERT INTO users (id, workspace_id, email, password_hash, created_at) "
                "VALUES (:id, :workspace_id, :email, 'x', :now)"
            ),
            {
                "id": other_user_id,
                "workspace_id": other_workspace_id,
                "email": f"{other_user_id}@example.test",
                "now": now,
            },
        )
    try:
        _insert_incident(
            other_workspace_id,
            status="resolved",
            detected_at=now - timedelta(hours=2),
            resolved_at=now,
        )
        with SessionFactory() as session:
            snapshots = compute_metrics(session, workspace_id=workspace)
        time_to_restore = next(s for s in snapshots if s.metric_key == "time_to_restore")
        assert time_to_restore.population == 0
        assert time_to_restore.value is None
    finally:
        with engine.begin() as connection:
            for table in ("incidents", "users"):
                connection.execute(
                    text(f"DELETE FROM {table} WHERE workspace_id = :ws"),  # noqa: S608
                    {"ws": other_workspace_id},
                )
            connection.execute(
                text("DELETE FROM workspaces WHERE id = :ws"), {"ws": other_workspace_id}
            )


# --- always-insufficient metrics --------------------------------------------


def test_deployment_dependent_metrics_always_insufficient(workspace: UUID) -> None:
    with SessionFactory() as session:
        snapshots = compute_metrics(session, workspace_id=workspace)
    for metric_key in (
        "delivery_frequency",
        "lead_time_for_changes",
        "change_failure_rate",
    ):
        snapshot = next(s for s in snapshots if s.metric_key == metric_key)
        assert snapshot.coverage_status == "insufficient_coverage"
        assert snapshot.coverage_percentage == 0.0
        assert snapshot.value is None
        assert snapshot.population == 0


def test_time_to_restore_is_complete_coverage_with_no_incidents(workspace: UUID) -> None:
    """`time_to_restore` (Task 6) reads directly from the workspace-
    authored `incidents` table, not a connector sync cursor -- its
    coverage is unconditionally `'complete'` even with zero incidents
    recorded (a legitimate "nothing to restore from" state), unlike the
    three deployment-dependent metrics above which report
    `insufficient_coverage` because no source exists at all. See
    `metrics.py`'s own module docstring for the full reasoning.
    """
    with SessionFactory() as session:
        snapshots = compute_metrics(session, workspace_id=workspace)
    snapshot = next(s for s in snapshots if s.metric_key == "time_to_restore")
    assert snapshot.coverage_status == "complete"
    assert snapshot.coverage_percentage == 100.0
    assert snapshot.value is None
    assert snapshot.population == 0


# --- compute_and_store_metrics: insert-only ---------------------------------


def test_compute_and_store_metrics_is_insert_only(workspace: UUID) -> None:
    with SessionFactory() as session:
        first = compute_and_store_metrics(session, workspace_id=workspace)
        session.commit()
    with SessionFactory() as session:
        second = compute_and_store_metrics(session, workspace_id=workspace)
        session.commit()

    assert len(first) == 7
    assert len(second) == 7
    assert {row["id"] for row in first}.isdisjoint({row["id"] for row in second})

    with engine.begin() as connection:
        count = connection.execute(
            text("SELECT count(*) FROM delivery_metric_snapshots WHERE workspace_id = :ws"),
            {"ws": workspace},
        ).scalar_one()
    assert count == 14


def test_details_json_round_trips_through_the_database(workspace: UUID) -> None:
    """Every `details` assertion elsewhere in this file checks the
    in-memory `MetricSnapshot` returned by `compute_metrics`, never a row
    actually read back after `compute_and_store_metrics`'s own
    `dumps(snapshot.details)` write -- proves the JSON text column
    genuinely round-trips (write, then a fresh read-and-parse), not just
    that the in-memory dict was correct before serialization.
    """
    from json import loads

    account_id = _insert_connector_account(workspace, provider="jira")
    _insert_cursor(workspace, account_id, resource_type="work_item", updated_at=datetime.now(UTC))
    now = datetime.now(UTC)
    _insert_work_item(
        workspace,
        account_id,
        external_id="open-3d",
        status="Open",
        provider_created_at=now - timedelta(days=3),
    )

    with SessionFactory() as session:
        compute_and_store_metrics(session, workspace_id=workspace)
        session.commit()

    with engine.begin() as connection:
        details_json = connection.execute(
            text(
                "SELECT details_json FROM delivery_metric_snapshots "
                "WHERE workspace_id = :ws AND metric_key = 'work_ageing'"
            ),
            {"ws": workspace},
        ).scalar_one()
    assert details_json is not None
    details = loads(details_json)
    assert details["buckets"]["0-7d"] == 1
    assert details["items_missing_created_at"] == 0


# --- end-to-end: real GET /engineering/metrics endpoint ---------------------


def _headers_for_session(token: str) -> dict[str, str]:
    # GET /engineering/metrics requires CsrfDep too, despite being a GET --
    # it writes new rows on every call. See get_metrics_endpoint's own
    # docstring for why a state-changing GET needs the same CSRF check
    # every other write endpoint in this domain already requires.
    csrf = hmac_new(settings.session_secret.encode(), token.encode(), "sha256").hexdigest()
    return {"X-CSRF-Token": csrf, "X-Correlation-ID": str(uuid4())}


@pytest.fixture
def client_context(workspace: UUID) -> Iterator[tuple[TestClient, str]]:
    token = f"session-{uuid4()}"
    now = datetime.now(UTC)
    with engine.begin() as connection:
        user_id = connection.execute(
            text("SELECT id FROM users WHERE workspace_id = :ws"), {"ws": workspace}
        ).scalar_one()
        connection.execute(
            text(
                "INSERT INTO sessions (id, workspace_id, user_id, token_hash, "
                "expires_at, last_seen_at) "
                "VALUES (:id, :workspace_id, :user_id, :token_hash, :expires_at, :now)"
            ),
            {
                "id": uuid4(),
                "workspace_id": workspace,
                "user_id": user_id,
                "token_hash": sha256(token.encode()).hexdigest(),
                "expires_at": now + timedelta(hours=1),
                "now": now,
            },
        )
    client = TestClient(app)
    client.cookies.set("ecc_session", token)
    try:
        yield client, token
    finally:
        client.close()
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM sessions WHERE workspace_id = :ws"), {"ws": workspace}
            )


def test_get_metrics_endpoint_returns_all_seven_metrics(
    workspace: UUID, client_context: tuple[TestClient, str]
) -> None:
    client, token = client_context
    response = client.get("/api/v1/engineering/metrics", headers=_headers_for_session(token))
    assert response.status_code == 200, response.text
    body = response.json()
    metric_keys = {row["metric_key"] for row in body["metrics"]}
    assert metric_keys == {
        "delivery_frequency",
        "lead_time_for_changes",
        "change_failure_rate",
        "time_to_restore",
        "work_ageing",
        "blocked_work",
        "review_latency",
    }
    for row in body["metrics"]:
        if row["metric_key"] == "time_to_restore":
            # Workspace-authored, not sync-dependent -- always "complete"
            # coverage even with zero incidents. See metrics.py's docstring.
            assert row["coverage_status"] == "complete"
        else:
            assert row["coverage_status"] == "insufficient_coverage"  # nothing connected yet


def test_get_metrics_endpoint_rejects_missing_csrf_token(
    workspace: UUID, client_context: tuple[TestClient, str]
) -> None:
    """A state-changing GET (writes seven rows every call) authenticated
    only by a session cookie is reachable by a plain cross-site top-level
    navigation, which still attaches the cookie -- CsrfDep closes that,
    the same mechanism every mutation endpoint in this domain already
    requires. A missing X-CSRF-Token header must be rejected.
    """
    client, _token = client_context
    response = client.get("/api/v1/engineering/metrics", headers={"X-Correlation-ID": str(uuid4())})
    assert response.status_code == 403
    with engine.begin() as connection:
        count = connection.execute(
            text("SELECT count(*) FROM delivery_metric_snapshots WHERE workspace_id = :ws"),
            {"ws": workspace},
        ).scalar_one()
    assert count == 0


def test_get_metrics_endpoint_is_workspace_isolated(
    workspace: UUID, client_context: tuple[TestClient, str]
) -> None:
    client, token = client_context
    other_workspace_id = uuid4()
    other_account_id = uuid4()
    now = datetime.now(UTC)
    other_user_id = uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, 'Other Workspace', 'UTC', :now)"
            ),
            {"id": other_workspace_id, "now": now},
        )
        connection.execute(
            text(
                "INSERT INTO users (id, workspace_id, email, password_hash, created_at) "
                "VALUES (:id, :workspace_id, :email, 'x', :now)"
            ),
            {
                "id": other_user_id,
                "workspace_id": other_workspace_id,
                "email": f"{other_user_id}@example.test",
                "now": now,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO connector_accounts (
                    id, workspace_id, provider, external_account_id, display_name,
                    granted_scopes, encrypted_credentials, status, version,
                    created_by, updated_by, created_at, updated_at
                ) VALUES (
                    :id, :workspace_id, 'jira', 'other-acct', 'Other',
                    ARRAY[]::text[], :cred, 'active', 1, :actor, :actor, :now, :now
                )
                """
            ),
            {
                "id": other_account_id,
                "workspace_id": other_workspace_id,
                "cred": b"fake",
                "actor": other_user_id,
                "now": now,
            },
        )
        connection.execute(
            text(
                "INSERT INTO sync_cursors (id, workspace_id, connector_account_id, "
                "resource_type, cursor_value, updated_at) "
                "VALUES (:id, :ws, :acct, 'work_item', 'x', :now)"
            ),
            {"id": uuid4(), "ws": other_workspace_id, "acct": other_account_id, "now": now},
        )

    try:
        response = client.get("/api/v1/engineering/metrics", headers=_headers_for_session(token))
        assert response.status_code == 200, response.text
        work_ageing = next(
            row for row in response.json()["metrics"] if row["metric_key"] == "work_ageing"
        )
        # The caller's own workspace has no connected account -- the
        # *other* workspace's freshly-synced account must never leak in.
        assert work_ageing["coverage_status"] == "insufficient_coverage"
    finally:
        with engine.begin() as connection:
            for table in ("sync_cursors", "connector_accounts", "users"):
                connection.execute(
                    text(f"DELETE FROM {table} WHERE workspace_id = :ws"),  # noqa: S608
                    {"ws": other_workspace_id},
                )
            connection.execute(
                text("DELETE FROM workspaces WHERE id = :ws"), {"ws": other_workspace_id}
            )
