"""Who may see a workflow run: the run row and the version it is pinned to.

A run's detail (workflow_id, step results) is its workflow's, so a run is
visible only while the `workflow_versions` row it is pinned to is readable
too. Checked live rather than copied onto the run at enqueue: a later
visibility change, ownership transfer or revoked grant on the version then
applies to its existing runs, scheduled ones included, with no backfill.

An approval request is about one run's step (its digest and categories
describe the workflow's action), so it is visible only while its run is.

Its own module, rather than in `worker.py`, because `approvals.py` needs it
and `worker.py` already imports `approvals.py`.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from ecc.auth import AuthContext
from ecc.platform import authz

PINNED_VERSION_JOIN = (
    "workflow_versions.workspace_id = workflow_runs.workspace_id "
    "AND workflow_versions.workflow_id = workflow_runs.workflow_id "
    "AND workflow_versions.version = workflow_runs.workflow_version"
)


def visible_runs_filter_sql(session: Session, auth: AuthContext) -> tuple[str, dict[str, Any]]:
    """A `WHERE` fragment over an unaliased `workflow_runs`: the run is
    readable and so is its pinned version. Its bind params use the `run_`
    and `version_` authz prefixes."""
    run_sql, run_params = authz.visible_resource_filter_sql(
        session,
        auth,
        resource_type="workflow_runs",
        action="read",
        table_alias="workflow_runs",
        param_prefix="run_",
    )
    version_sql, version_params = authz.visible_resource_filter_sql(
        session,
        auth,
        resource_type="workflow_versions",
        action="read",
        table_alias="workflow_versions",
        param_prefix="version_",
    )
    return (
        f"({run_sql} AND EXISTS (SELECT 1 FROM workflow_versions "  # noqa: S608 -- constants and authz fragments only
        f"WHERE {PINNED_VERSION_JOIN} AND {version_sql}))",
        {**run_params, **version_params},
    )


def run_visible(
    session: Session, auth: AuthContext, run_id: UUID, *, lock_version: bool = False
) -> bool:
    """`authorize(read)` on the run and on its pinned version. With
    `lock_version`, the version row is locked `FOR SHARE` before either
    check, so a concurrent ownership transfer or visibility change is seen.
    The caller must already hold the run row's lock: the order is run, then
    version."""
    version_id = session.execute(
        text(
            "SELECT workflow_versions.id FROM workflow_runs "
            f"JOIN workflow_versions ON {PINNED_VERSION_JOIN} "
            "WHERE workflow_runs.workspace_id = :workspace_id AND workflow_runs.id = :id"
            + (" FOR SHARE OF workflow_versions" if lock_version else "")
        ),
        {"workspace_id": auth.workspace_id, "id": run_id},
    ).scalar_one_or_none()
    return (
        version_id is not None
        and authz.authorize(
            session, auth, resource_type="workflow_runs", resource_id=run_id, action="read"
        )
        and authz.authorize(
            session, auth, resource_type="workflow_versions", resource_id=version_id, action="read"
        )
    )
