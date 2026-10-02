"""Authorizing a write against an existing `workflow_id` family.

A family has no owner or visibility of its own that callers are checked
against: `workflow_definitions` is only the identity row. What a member may
see and change is decided per `workflow_versions` row. Two writes act on a
whole family, though: appending a draft (`POST /automations/workflows`) and
creating a policy for it (`POST /automations/policies`). Both go through
`lock_and_authorize_family`, so they answer the same refusals.

Its own module because both `workflows.py` and `policy.py` need it, and
`workflows.py` already imports `policy.py`.
"""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from ecc.auth import AuthContext
from ecc.platform import authz


def lock_and_authorize_family(session: Session, auth: AuthContext, workflow_id: str) -> bool:
    """Gate a write against an existing `workflow_id` family: the caller
    must be able to read (else `404 WORKFLOW_NOT_FOUND`) and write (else
    `403 INSUFFICIENT_ROLE`) both the family's latest version and its
    active version. The latest is the one a new draft supersedes (and whose
    number the draft response's `version` discloses); the active one is
    what publishing a new draft would retire, and what a policy would let
    run. Checking only the latest would let a visible draft stacked on
    someone else's private active version open the family.

    Returns whether the family exists. A missing family is not refused
    here: appending a draft creates it, so the caller decides what a
    missing family means.

    All read checks run before any write check, so a family with any
    version the caller cannot see answers the same 404, never a 403 that
    shows part of it is visible. That 404 is the one an unknown
    `version_id` or `workflow_id` answers, and carries no version number.

    Must run inside the write transaction, after the membership and
    idempotency locks and before `load_cached` (ADR-0014). The family row
    and both version rows are locked first, so an ownership or visibility
    change committed while this waits is what the checks see.
    `create_workflow_draft` re-locks the same rows (a no-op).
    """
    # NO KEY UPDATE: still serializes concurrent appends to this family,
    # without also waiting on every transaction that holds a foreign-key
    # share lock on it (any write to one of its version rows).
    family = session.execute(
        text(
            "SELECT id FROM workflow_definitions "
            "WHERE workspace_id = :workspace_id AND workflow_id = :workflow_id "
            "FOR NO KEY UPDATE"
        ),
        {"workspace_id": auth.workspace_id, "workflow_id": workflow_id},
    ).one_or_none()
    if family is None:
        return False
    params = {"workspace_id": auth.workspace_id, "workflow_id": workflow_id}
    # Latest first, then active: the same order `activate_workflow_version`
    # takes (its target, then the active row), so the two cannot deadlock.
    latest_id = session.execute(
        text(
            "SELECT id FROM workflow_versions "
            "WHERE workspace_id = :workspace_id AND workflow_id = :workflow_id "
            "ORDER BY version DESC LIMIT 1 FOR UPDATE"
        ),
        params,
    ).scalar_one_or_none()
    active_id = session.execute(
        text(
            "SELECT id FROM workflow_versions "
            "WHERE workspace_id = :workspace_id AND workflow_id = :workflow_id "
            "AND status = 'active' FOR UPDATE"
        ),
        params,
    ).scalar_one_or_none()
    resources = [
        ("workflow_versions", v) for v in dict.fromkeys((latest_id, active_id)) if v is not None
    ]
    # A family row with no versions (not produced by any code path today:
    # `create_workflow_draft` inserts both together) is checked against its
    # own `owner_id`/`visibility` instead, so it fails closed rather than
    # skipping authorization.
    if not resources:
        resources = [("workflow_definitions", family.id)]
    for resource_type, resource_id in resources:
        if not authz.authorize(
            session, auth, resource_type=resource_type, resource_id=resource_id, action="read"
        ):
            raise HTTPException(status_code=404, detail="WORKFLOW_NOT_FOUND")
    for resource_type, resource_id in resources:
        if not authz.authorize(
            session, auth, resource_type=resource_type, resource_id=resource_id, action="write"
        ):
            raise HTTPException(status_code=403, detail="INSUFFICIENT_ROLE")
    return True
