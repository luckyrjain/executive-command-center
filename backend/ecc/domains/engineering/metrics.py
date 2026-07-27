"""Delivery and reliability metric computation (Phase 6 Task 5,
`docs/phases/phase-006/DELIVERY-INTELLIGENCE-CONTRACT.md`'s seven
approved metric definitions made concrete against real data).

**Only three of the seven metrics are genuinely computable in this
activation: `work_ageing`, `blocked_work`, `review_latency`.** The other
four -- `delivery_frequency`, `lead_time_for_changes`, `change_failure_
rate`, `time_to_restore` -- all require `deployments` and/or `incidents`
data (`lead_time_for_changes`'s own contract population is "changes
merged in window *with a linked deployment*," not merely merged changes
-- `github_adapter.py`'s new `_sync_changes`/`_sync_reviews` alone does
not satisfy that), and neither table exists yet (`DATA-MODEL.md`'s own
"Tasks 5-6" note: `incidents` is explicit Task 6 scope; `deployments` has
no task assigned to it at all yet, disclosed in
`github_adapter.py`/`gitlab_adapter.py`'s own module docstrings as
deferred). Rather than compute a number under a materially different
definition than the contract states (a real risk this codebase's own
review history has caught before -- see `jira_adapter.py`'s DST-cursor
fix and its "never fabricate a number that looks plausible but isn't
backed by real data" precedent), these four always report
`coverage_status = 'insufficient_coverage'`, `value = None`, exactly
matching `DELIVERY-INTELLIGENCE-CONTRACT.md`'s own "no metric is
computed or displayed below 50% coverage at all" rule applied to its
most extreme case: 0% coverage, because no source exists at all yet, not
merely an under-synced one.

**Coverage is inferred from `sync_cursors.updated_at` recency, not a
separate historical completeness ledger.** `sync_cursors` is upserted on
every sync call that returns a `next_cursor` (`connector_accounts.py`'s
phase 3), regardless of that call's own `succeeded`/`partial` status --
a `partial` result is normal, expected behavior for a bounded, resumable
sync (the same reasoning `_MAX_PAGES_PER_CALL` bounds throughout Tasks
2-5), not a failure state a coverage calculation should penalize
further. `_coverage_for` treats a resource type as "fresh" for a given
connector account when its cursor was updated within the phase's own
five-minute incremental-lag NFR (`RFC-005`), and averages that across
every active connector account of the metric's relevant provider(s) in
the workspace. **Accepted limitation, disclosed rather than silently
assumed away:** this does not distinguish "backfill fully complete" from
"a bounded-page sync recently made some progress" -- a connector that
just connected a repository with thousands of PRs could show `complete`
coverage after one recent (but far from exhaustive) sync call. A
dedicated "backfill completion" ledger is a real gap this task does not
close; see `DELIVERY-INTELLIGENCE-CONTRACT.md`'s own "Task 5 status"
section.

**No periodic sync/computation scheduler exists yet.** Every metric
computation in this module is triggered by calling `GET /engineering/
metrics` itself -- there is no background job recomputing snapshots on a
schedule, mirroring `POST /connectors/{id}/sync`'s own identical
"manual trigger only, no scheduler" reality since Task 1. A `GET`
endpoint with this side effect (writing a new, immutable snapshot row
every time it is called) is a deliberate, disclosed departure from pure
REST semantics, not an oversight -- see `connector_accounts.py`'s
`get_metrics_endpoint` docstring for the full reasoning.

**Heuristic "open"/"blocked" classification, disclosed.** Jira's
workflow statuses are fully customizable per project; this module has no
per-workspace status-category configuration to consult. `_CLOSED_
STATUSES` treats any work item whose `status` case-insensitively matches
`done`/`closed`/`resolved` as closed (everything else counts as "open"
for `work_ageing`'s population); `blocked_work` treats any open item
whose `status` case-insensitively contains the substring `blocked` as
blocked. Both are reasonable defaults, not a configurable mapping --
a workspace using different status vocabulary (e.g. a custom "On Hold"
column with no literal "blocked" in its name) will undercount, which is
a real, disclosed limitation rather than a silently wrong number: no
metric here is asserted more precise than this heuristic actually is.

**`review_latency`'s population is narrower than "merged changes with at
least one review," disclosed.** `DELIVERY-INTELLIGENCE-CONTRACT.md`'s own
population line names any merged change with at least one review; this
implementation further requires a captured `requested_at`, since the
metric's own definition is a *duration from request to first review*,
which is meaningless without a request timestamp. `requested_at` is
`None` for two real, distinct reasons (`github_adapter.py`'s own
docstring): no `review_requested` timeline event exists at all (an
ad-hoc review with no formal request), or the timeline fetch -- a single
unpaginated call -- doesn't reach far enough back to find a real one
that happened. Either way, that change is silently excluded from
`population`, not merely left with a null contribution -- a real,
disclosed narrowing of the contract's stated population, not a
materially different metric definition (the duration itself, once
computed, still means exactly what the contract says). Coverage for
this metric is the worse of the `change`/`review` sync cursors'
individual coverage (`_worse_coverage`), since the population genuinely
depends on both being current.

**`aggregation_scope` is always `'system'`.** No `Team`/`Workstream`
entity exists anywhere in this codebase yet -- see migration
`0048_phase6_delivery_metrics.py`'s own docstring.

**Snapshots are insert-only.** `compute_and_store_metrics` never updates
or deletes an existing `delivery_metric_snapshots` row -- each call
writes seven new rows (one per metric), preserving every prior
computation as history, per `DELIVERY-INTELLIGENCE-CONTRACT.md`'s "a
later redefinition... never rewrites a historical snapshot" rule.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from json import dumps
from statistics import median
from typing import Any, Literal
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.orm import Session

MetricKey = Literal[
    "delivery_frequency",
    "lead_time_for_changes",
    "change_failure_rate",
    "time_to_restore",
    "work_ageing",
    "blocked_work",
    "review_latency",
]
CoverageStatus = Literal["complete", "partial", "insufficient_coverage"]

_COMPLETE_COVERAGE_THRESHOLD = 95.0
_PARTIAL_COVERAGE_THRESHOLD = 50.0
# RFC-005's incremental-sync-lag NFR, reused here as the coverage
# freshness window -- see this module's own docstring.
_FRESHNESS_WINDOW = timedelta(minutes=5)
_REVIEW_LATENCY_WINDOW = timedelta(days=30)
_CLOSED_STATUSES = frozenset({"done", "closed", "resolved"})
_BLOCKED_STATUS_MARKER = "blocked"


@dataclass(frozen=True, slots=True)
class MetricSnapshot:
    metric_key: MetricKey
    window_label: str
    population: int
    numerator: float | None
    denominator: float | None
    value: float | None
    details: dict[str, Any] | None
    coverage_status: CoverageStatus
    coverage_percentage: float
    coverage_gap_description: str | None


def _coverage_for(
    session: Session, *, workspace_id: UUID, resource_type: str, providers: tuple[str, ...]
) -> tuple[CoverageStatus, float, str | None]:
    if not providers:
        return (
            "insufficient_coverage",
            0.0,
            f"No connector adapter populates '{resource_type}' data yet",
        )
    account_ids = (
        session.execute(
            text(
                "SELECT id FROM connector_accounts WHERE workspace_id = :workspace_id "
                "AND status = 'active' AND provider = ANY(:providers)"
            ),
            {"workspace_id": workspace_id, "providers": list(providers)},
        )
        .scalars()
        .all()
    )
    total = len(account_ids)
    provider_label = "/".join(providers)
    if total == 0:
        return (
            "insufficient_coverage",
            0.0,
            f"No connected, active {provider_label} account for this metric's population",
        )
    now = datetime.now(UTC)
    fresh = 0
    for account_id in account_ids:
        updated_at = session.execute(
            text(
                "SELECT updated_at FROM sync_cursors WHERE workspace_id = :workspace_id "
                "AND connector_account_id = :account_id AND resource_type = :resource_type"
            ),
            {
                "workspace_id": workspace_id,
                "account_id": account_id,
                "resource_type": resource_type,
            },
        ).scalar_one_or_none()
        if updated_at is not None and updated_at >= now - _FRESHNESS_WINDOW:
            fresh += 1
    percentage = (fresh / total) * 100
    if percentage >= _COMPLETE_COVERAGE_THRESHOLD:
        return "complete", percentage, None
    freshness_minutes = int(_FRESHNESS_WINDOW.total_seconds() // 60)
    gap = (
        f"{fresh} of {total} connected {provider_label} account(s) synced "
        f"'{resource_type}' within the last {freshness_minutes} minutes"
    )
    if percentage >= _PARTIAL_COVERAGE_THRESHOLD:
        return "partial", percentage, gap
    return "insufficient_coverage", percentage, gap


def _bucket_ages_days(ages_days: list[float]) -> dict[str, int]:
    buckets = {"0-7d": 0, "8-30d": 0, "31-90d": 0, "90d+": 0}
    for age in ages_days:
        if age <= 7:
            buckets["0-7d"] += 1
        elif age <= 30:
            buckets["8-30d"] += 1
        elif age <= 90:
            buckets["31-90d"] += 1
        else:
            buckets["90d+"] += 1
    return buckets


def _open_work_items(session: Session, *, workspace_id: UUID) -> list[dict[str, Any]]:
    rows = session.execute(
        text(
            "SELECT status, provider_created_at FROM engineering_work_items "
            "WHERE workspace_id = :workspace_id AND permission_state = 'active'"
        ),
        {"workspace_id": workspace_id},
    ).mappings()
    return [
        dict(row) for row in rows if (row["status"] or "").strip().lower() not in _CLOSED_STATUSES
    ]


def _compute_work_ageing(session: Session, *, workspace_id: UUID, now: datetime) -> MetricSnapshot:
    coverage_status, coverage_pct, gap = _coverage_for(
        session, workspace_id=workspace_id, resource_type="work_item", providers=("jira",)
    )
    open_items = _open_work_items(session, workspace_id=workspace_id)
    population = len(open_items)
    ages_days = [
        (now - item["provider_created_at"]).total_seconds() / 86400
        for item in open_items
        if item["provider_created_at"] is not None
    ]
    value = median(ages_days) if ages_days else None
    details = {
        "buckets": _bucket_ages_days(ages_days),
        "items_missing_created_at": population - len(ages_days),
    }
    return MetricSnapshot(
        metric_key="work_ageing",
        window_label="point_in_time",
        population=population,
        numerator=None,
        denominator=None,
        value=value,
        details=details,
        coverage_status=coverage_status,
        coverage_percentage=coverage_pct,
        coverage_gap_description=gap,
    )


def _compute_blocked_work(session: Session, *, workspace_id: UUID, now: datetime) -> MetricSnapshot:
    coverage_status, coverage_pct, gap = _coverage_for(
        session, workspace_id=workspace_id, resource_type="work_item", providers=("jira",)
    )
    open_items = _open_work_items(session, workspace_id=workspace_id)
    population = len(open_items)
    blocked_items = [
        item for item in open_items if _BLOCKED_STATUS_MARKER in (item["status"] or "").lower()
    ]
    blocked_count = len(blocked_items)
    blocked_ages_days = [
        (now - item["provider_created_at"]).total_seconds() / 86400
        for item in blocked_items
        if item["provider_created_at"] is not None
    ]
    details = {"median_age_days": median(blocked_ages_days)} if blocked_ages_days else None
    return MetricSnapshot(
        metric_key="blocked_work",
        window_label="point_in_time",
        population=population,
        numerator=float(blocked_count),
        denominator=float(population) if population else None,
        value=float(blocked_count),
        details=details,
        coverage_status=coverage_status,
        coverage_percentage=coverage_pct,
        coverage_gap_description=gap,
    )


def _worse_coverage(
    a: tuple[CoverageStatus, float, str | None], b: tuple[CoverageStatus, float, str | None]
) -> tuple[CoverageStatus, float, str | None]:
    """`review_latency`'s population depends on both `change` and
    `review` sync being current -- a workspace whose `change` cursor is
    stale but whose `review` cursor happens to be fresh would otherwise
    report `complete` while materially undercounting the true
    population (review found this real gap). Reports whichever of the
    two resource types' coverage is lower, the more honest number.
    """
    return a if a[1] <= b[1] else b


def _compute_review_latency(
    session: Session, *, workspace_id: UUID, now: datetime
) -> MetricSnapshot:
    change_coverage = _coverage_for(
        session, workspace_id=workspace_id, resource_type="change", providers=("github",)
    )
    review_coverage = _coverage_for(
        session, workspace_id=workspace_id, resource_type="review", providers=("github",)
    )
    coverage_status, coverage_pct, gap = _worse_coverage(change_coverage, review_coverage)
    window_start = now - _REVIEW_LATENCY_WINDOW
    rows = (
        session.execute(
            text(
                """
            SELECT DISTINCT ON (r.change_id) r.requested_at, r.submitted_at
            FROM reviews r
            JOIN changes c ON c.id = r.change_id AND c.workspace_id = r.workspace_id
            WHERE r.workspace_id = :workspace_id
              AND c.merged_at >= :window_start
              AND r.requested_at IS NOT NULL
              AND r.submitted_at IS NOT NULL
            ORDER BY r.change_id, r.submitted_at ASC
            """
            ),
            {"workspace_id": workspace_id, "window_start": window_start},
        )
        .mappings()
        .all()
    )
    # `population` is the count of changes actually contributing to
    # `value` below (a negative "latency" -- submitted before requested,
    # a data-integrity edge case, not expected in practice -- is dropped
    # from both, keeping the two internally consistent) rather than the
    # raw row count before that filter, which review found could
    # otherwise silently diverge from what the median was computed over.
    latencies_seconds = [
        (row["submitted_at"] - row["requested_at"]).total_seconds()
        for row in rows
        if row["submitted_at"] >= row["requested_at"]
    ]
    population = len(latencies_seconds)
    value = median(latencies_seconds) if latencies_seconds else None
    return MetricSnapshot(
        metric_key="review_latency",
        window_label="30d",
        population=population,
        numerator=None,
        denominator=None,
        value=value,
        details=None,
        coverage_status=coverage_status,
        coverage_percentage=coverage_pct,
        coverage_gap_description=gap,
    )


def _insufficient_coverage_snapshot(
    metric_key: MetricKey, *, window_label: str, gap: str
) -> MetricSnapshot:
    return MetricSnapshot(
        metric_key=metric_key,
        window_label=window_label,
        population=0,
        numerator=None,
        denominator=None,
        value=None,
        details=None,
        coverage_status="insufficient_coverage",
        coverage_percentage=0.0,
        coverage_gap_description=gap,
    )


def compute_metrics(session: Session, *, workspace_id: UUID) -> list[MetricSnapshot]:
    """Computes all seven metric snapshots -- three real, four
    `insufficient_coverage` -- without writing them. Split from
    `compute_and_store_metrics` so a caller (or a test) can inspect
    computed values without also creating a new immutable row each call.
    """
    now = datetime.now(UTC)
    return [
        _insufficient_coverage_snapshot(
            "delivery_frequency",
            window_label="30d",
            gap="No deployment sync exists yet (no task populates 'deployments')",
        ),
        _insufficient_coverage_snapshot(
            "lead_time_for_changes",
            window_label="30d",
            gap=(
                "This metric's own population is changes merged with a linked "
                "deployment; no deployment sync exists yet"
            ),
        ),
        _insufficient_coverage_snapshot(
            "change_failure_rate",
            window_label="30d",
            gap="No deployment or incident sync exists yet",
        ),
        _insufficient_coverage_snapshot(
            "time_to_restore",
            window_label="30d",
            gap="No incident sync exists yet (Task 6 scope)",
        ),
        _compute_work_ageing(session, workspace_id=workspace_id, now=now),
        _compute_blocked_work(session, workspace_id=workspace_id, now=now),
        _compute_review_latency(session, workspace_id=workspace_id, now=now),
    ]


def compute_and_store_metrics(session: Session, *, workspace_id: UUID) -> list[dict[str, Any]]:
    """Computes all seven snapshots and inserts each as a new, immutable
    `delivery_metric_snapshots` row -- never updates or deletes an
    existing one. Does not commit; the caller (`get_metrics_endpoint`)
    owns the transaction, matching every other write in this domain.
    """
    now = datetime.now(UTC)
    snapshots = compute_metrics(session, workspace_id=workspace_id)
    stored: list[dict[str, Any]] = []
    for snapshot in snapshots:
        row_id = uuid4()
        session.execute(
            text(
                """
                INSERT INTO delivery_metric_snapshots (
                    id, workspace_id, metric_key, definition_version, aggregation_scope,
                    window_label, window_start, window_end, population, numerator,
                    denominator, value, details_json, coverage_status, coverage_percentage,
                    coverage_gap_description, computed_at, created_at
                ) VALUES (
                    :id, :workspace_id, :metric_key, 1, 'system',
                    :window_label, NULL, NULL, :population, :numerator,
                    :denominator, :value, :details_json, :coverage_status,
                    :coverage_percentage, :coverage_gap_description, :now, :now
                )
                """
            ),
            {
                "id": row_id,
                "workspace_id": workspace_id,
                "metric_key": snapshot.metric_key,
                "window_label": snapshot.window_label,
                "population": snapshot.population,
                "numerator": snapshot.numerator,
                "denominator": snapshot.denominator,
                "value": snapshot.value,
                "details_json": dumps(snapshot.details) if snapshot.details is not None else None,
                "coverage_status": snapshot.coverage_status,
                "coverage_percentage": snapshot.coverage_percentage,
                "coverage_gap_description": snapshot.coverage_gap_description,
                "now": now,
            },
        )
        stored.append(
            {
                "id": row_id,
                "metric_key": snapshot.metric_key,
                "window_label": snapshot.window_label,
                "population": snapshot.population,
                "numerator": snapshot.numerator,
                "denominator": snapshot.denominator,
                "value": snapshot.value,
                "details": snapshot.details,
                "coverage_status": snapshot.coverage_status,
                "coverage_percentage": snapshot.coverage_percentage,
                "coverage_gap_description": snapshot.coverage_gap_description,
                "computed_at": now,
            }
        )
    return stored
