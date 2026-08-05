"""Phase 10 Gmail Connector Task 2: widen `sync_cursors.resource_type` to
accept `'message'`.

`ecc.domains.engineering.connector_accounts.ResourceType` (the request-body
Literal `POST .../sync` validates against) was widened to add `"message"`
in this same task, mirroring `ck_connector_accounts_provider`'s `'gmail'`
addition and `ck_personal_domains_domain_key`'s `'email'` addition, both
already done by migration `0069` (Task 1). `sync_cursors.resource_type`'s
own `ck_sync_cursors_resource_type` CHECK constraint -- the third closed
set `ResourceType` must stay a subset of, per that Literal's own comment
("Matches migration `0044_phase6_connector_platform.py`'s ... CHECK
constraint exactly") -- was missed by migration `0069`, which widened the
other two but never touched this one.

**Found by review, not part of the original Task 1/2 implementation.**
`sync_connector_endpoint` (`connector_accounts.py`) INSERTs into `sync_
cursors` with `resource_type = payload.resource_type` whenever a sync's
`SyncOutcome.next_cursor` is not `None` -- true for essentially every
successful `gmail`/`"message"` sync once at least one message (or, for an
empty window, `users.getProfile`) has yielded a `historyId`. Without this
migration, that INSERT violates `ck_sync_cursors_resource_type` (`'message'`
is not in the allowed set), raising an uncaught `IntegrityError` inside the
same transaction as `sync_runs`' own success-status `UPDATE` -- rolling
that back too, leaving the `sync_runs` row stuck `'running'` until the
stale-run reaper eventually reaps it, and returning an unhandled 500
instead of the app's structured error envelope. In short: every real
`POST /connectors/{id}/sync` call for a `gmail` account's `"message"`
resource type was broken by this gap, for any call that actually
progressed (the happy path, not merely the zero-item/rate-limited/
consent-revoked edge cases this task's own test file happens to cover
without ever driving a `next_cursor` value through this specific endpoint
-- `tests/test_gmail_connector_sync_postgres.py` exercises `GmailAdapter`
directly, never through `sync_connector_endpoint`, which is why this
survived to review undetected by the existing test suite).
"""

from alembic import op

revision = "0070_gmail_sync_cursor_type"
down_revision = "0069_phase10_gmail_connector"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_sync_cursors_resource_type", "sync_cursors", type_="check")
    op.create_check_constraint(
        "ck_sync_cursors_resource_type",
        "sync_cursors",
        "resource_type IN ('repository', 'work_item', 'change', 'review', "
        "'deployment', 'incident', 'monitor', 'service_definition', 'dashboard', 'message')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_sync_cursors_resource_type", "sync_cursors", type_="check")
    op.create_check_constraint(
        "ck_sync_cursors_resource_type",
        "sync_cursors",
        "resource_type IN ('repository', 'work_item', 'change', 'review', "
        "'deployment', 'incident', 'monitor', 'service_definition', 'dashboard')",
    )
