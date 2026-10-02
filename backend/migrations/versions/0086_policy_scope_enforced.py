"""Policy scope enforcement (`docs/superpowers/specs/2026-10-01-automation-
policy-scope-enforcement-design.md`, Decisions 3 and 4).

- `automation_policies.scope_enforced boolean NOT NULL DEFAULT false`. Every
  existing row becomes a *legacy* row (no scope check, exactly today's
  behaviour) and ages out within 90 days, since policies are immutable and
  `expires_at` is fixed at creation. The default is deliberately **not**
  flipped to true: `policy.create_policy` sets `scope_enforced = true`
  explicitly, so an old app instance during a rolling deploy keeps writing
  legacy rows instead of failing the CHECK below.
- `ck_automation_policies_enforced_scope`: an enforced row must name at
  least one action type and at least one data class, every data class one
  of Phase 4's four. Action-type vocabulary is application-only (it grows
  with adapters). A parity test pins this literal to
  `adapter_contract.DATA_CLASSES`.
- `workflow_run_steps.dispatch_value numeric(14,2) NULL`: the monetary value
  a first dispatch moved, summed per run against `value_limit`. NULL on
  every other row (compensation, rejected approval, failed validation,
  pre-migration), which counts as 0.

Operational note: both `ADD COLUMN`s are metadata-only on PostgreSQL 11+
(constant default / no default). The CHECK is validated against existing
rows, all of which are `scope_enforced = false` and so pass trivially.
"""

import sqlalchemy as sa
from alembic import op

revision = "0086_policy_scope_enforced"
down_revision = "0085_distinct_approver"
branch_labels = None
depends_on = None

_CHECK = "ck_automation_policies_enforced_scope"


def upgrade() -> None:
    op.add_column(
        "automation_policies",
        sa.Column("scope_enforced", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_check_constraint(
        _CHECK,
        "automation_policies",
        "NOT scope_enforced OR ("
        "cardinality(action_types) >= 1 "
        "AND cardinality(data_classes) >= 1 "
        "AND data_classes <@ ARRAY['public','internal','sensitive','restricted']::text[]"
        ")",
    )
    op.add_column(
        "workflow_run_steps",
        sa.Column("dispatch_value", sa.Numeric(14, 2), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("workflow_run_steps", "dispatch_value")
    op.drop_constraint(_CHECK, "automation_policies", type_="check")
    op.drop_column("automation_policies", "scope_enforced")
