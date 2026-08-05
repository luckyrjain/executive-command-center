"""Phase 10 Gmail Connector Task 1: widen `connector_accounts.provider`
and `personal_domains.domain_key` to accept `gmail`/`email`, and create
`email_threads`/`email_messages` (design doc Decision 2: dedicated tables,
not the generic `domain_records` shape, since a thread/message's own
structure -- sender, recipients, direction, snippet, body -- outgrows a
generic JSON payload the same way `incidents`/`engineering_decisions`
outgrew it in Phase 6).

`docs/superpowers/specs/2026-08-04-phase-10-gmail-connector-design.md`
Decisions 1-2; `docs/superpowers/plans/2026-08-04-phase-10-gmail-connector.md`
Task 1.

**Two structural fields plaintext, two encrypted, per Decision 2's
"encrypt the narrative, not the whole row" convention this migration
reuses rather than reinvents** (`ecc.domains.personal.domains.py`'s own
`_ENCRYPTED_FIELD_NAMES_BY_RECORD_TYPE` docstring states the identical
split for `domain_records.payload`). `email_threads.subject` and
`email_messages.sender`/`recipients`/`sent_at`/`direction` are plaintext
-- needed unencrypted for Task 2's sender/recipient `pkos_nodes`
resolution and Task 3's "awaiting reply" heuristic, both server-side
queries a fully-encrypted row would block. `email_messages.snippet`/`body`
are the actual narrative content and are individually Fernet-encrypted
(`ecc.domains.personal.crypto.encrypt_field`/`decrypt_field`, the
existing `ECC_PERSONAL_DATA_ENCRYPTION_KEY` -- no new key, per Decision 2)
before write, stored as `text` (Fernet's own ciphertext token is already
URL-safe-base64 ASCII) rather than `bytea`, matching `domain_records`'
JSONB-field convention rather than `connector_accounts.encrypted_
credentials`'s `bytea` column, since neither of these two columns is a
whole-row credential blob.

**`body` is nullable and starts `NULL`.** Task 1/2 sync `gmail.metadata`
only; a message's `body` is populated only once `gmail.readonly` is
called for it (Task 5's proactive action-detection fetch, or Task 6's
on-demand thread-open fetch) -- `NULL` means "not yet fetched," not "empty
email."

**No `owner_id`/`visibility` authz columns on these two tables.** Unlike
Phase 6/8's workspace-visible resource tables, `email_threads`/`email_
messages` are governed by Phase 7's per-owner personal-domain model, not
`ecc.platform.authz`'s role/grant model -- every query is scoped to
`(workspace_id, owner_id)` directly, the same shape `domain_records`
itself uses, and neither table is a member of `authz.py`'s
`_RESOURCE_TABLES` closed set.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0069_phase10_gmail_connector"
down_revision = "0068_phase8_membership_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)

    op.drop_constraint("ck_connector_accounts_provider", "connector_accounts", type_="check")
    op.create_check_constraint(
        "ck_connector_accounts_provider",
        "connector_accounts",
        "provider IN ('github', 'gitlab', 'jira', 'sandbox', 'datadog', 'gmail')",
    )

    op.drop_constraint("ck_personal_domains_domain_key", "personal_domains", type_="check")
    op.create_check_constraint(
        "ck_personal_domains_domain_key",
        "personal_domains",
        "domain_key IN ('habits', 'learning', 'travel', 'relationships', "
        "'health', 'finance', 'email')",
    )

    # --- email_threads ------------------------------------------------------

    op.create_table(
        "email_threads",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("owner_id", uuid, nullable=False),
        sa.Column("domain_key", sa.String(20), nullable=False, server_default="email"),
        sa.Column("connector_account_id", uuid, nullable=False),
        sa.Column("external_thread_id", sa.String(255), nullable=False),
        sa.Column("subject", sa.Text(), nullable=True),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("domain_key = 'email'", name="ck_email_threads_domain_key"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "owner_id", "domain_key"],
            [
                "personal_domains.workspace_id",
                "personal_domains.owner_id",
                "personal_domains.domain_key",
            ],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "connector_account_id"],
            ["connector_accounts.workspace_id", "connector_accounts.id"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "connector_account_id",
            "external_thread_id",
            name="uq_email_threads_account_external_id",
        ),
        # Required for `email_messages`' own composite FK to
        # `(email_threads.workspace_id, email_threads.id)` below -- `id`
        # alone is already the primary key, but Postgres requires a unique
        # constraint/index covering exactly the referenced column pair for
        # a composite FK, matching `domain_sources.uq_domain_sources_
        # workspace_id`'s identical precedent (migration `0054`).
        sa.UniqueConstraint("workspace_id", "id", name="uq_email_threads_workspace_id"),
    )
    op.create_index(
        "ix_email_threads_owner_last_message",
        "email_threads",
        ["workspace_id", "owner_id", sa.text("last_message_at DESC")],
    )

    # --- email_messages -------------------------------------------------------

    op.create_table(
        "email_messages",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("owner_id", uuid, nullable=False),
        sa.Column("thread_id", uuid, nullable=False),
        sa.Column("external_message_id", sa.String(255), nullable=False),
        sa.Column("sender", sa.String(320), nullable=False),
        sa.Column("recipients", postgresql.ARRAY(sa.String(320)), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("direction", sa.String(10), nullable=False),
        # Encrypted, per module docstring -- `text`, not `bytea` (Fernet's
        # own ciphertext token is already URL-safe-base64 ASCII).
        sa.Column("snippet", sa.Text(), nullable=True),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("body_fetched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "direction IN ('inbound', 'outbound')", name="ck_email_messages_direction"
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        # Direct `(workspace_id, owner_id) -> users` FK -- strictly redundant
        # with the transitive integrity `email_threads`' own FK to
        # `personal_domains(workspace_id, owner_id, domain_key)` already
        # provides (the same shape `domain_records`, this migration's own
        # docstring's cited precedent, relies on alone with no direct FK to
        # `users`). Kept as an extra, harmless belt-and-suspenders
        # constraint specifically on `email_messages` -- disclosed here
        # rather than left as an unexplained asymmetry between these two
        # sibling tables' own constraint sets.
        sa.ForeignKeyConstraint(
            ["workspace_id", "owner_id"], ["users.workspace_id", "users.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "thread_id"],
            ["email_threads.workspace_id", "email_threads.id"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "thread_id",
            "external_message_id",
            name="uq_email_messages_thread_external_id",
        ),
    )
    op.create_index(
        "ix_email_messages_thread_sent",
        "email_messages",
        ["workspace_id", "thread_id", sa.text("sent_at DESC")],
    )


def downgrade() -> None:
    op.drop_index("ix_email_messages_thread_sent", table_name="email_messages")
    op.drop_table("email_messages")
    op.drop_index("ix_email_threads_owner_last_message", table_name="email_threads")
    op.drop_table("email_threads")

    op.drop_constraint("ck_personal_domains_domain_key", "personal_domains", type_="check")
    op.create_check_constraint(
        "ck_personal_domains_domain_key",
        "personal_domains",
        "domain_key IN ('habits', 'learning', 'travel', 'relationships', 'health', 'finance')",
    )

    op.drop_constraint("ck_connector_accounts_provider", "connector_accounts", type_="check")
    op.create_check_constraint(
        "ck_connector_accounts_provider",
        "connector_accounts",
        "provider IN ('github', 'gitlab', 'jira', 'sandbox', 'datadog')",
    )
