"""Drop pkos_nodes.entity_id, closing a gap an audit of the shipped Phase 2
code found: migration 0010 added this column (nullable, no FK, no unique
constraint) to hold "a pointer back to an owning domain aggregate when the
entity mirrors one" per docs/superpowers/specs/2026-07-21-phase-2-
knowledge-platform-design.md's Open decision 1 and PKOS-SCHEMA.md's mapping
table -- but no code path in the shipped implementation ever writes a
non-NULL value to it (entities.py's create/patch, entity_operations.py's
merge/reverse/split all list their column sets explicitly and never
include it), and PKOS-SCHEMA.md's own accompanying
unique(workspace_id, entity_type, entity_id) constraint was never created
either. Every row's entity_id has always been and will always be NULL; the
"mirror a domain aggregate" feature it was meant to support was never
built.

entity_id was nonetheless being read and re-exposed as a permanently-null
public API field (EntityResponse.entity_id) purely because it was part of
the SELECT-all field list, not because anything reads its value -- see the
paired change removing it from _ENTITY_FIELDS/EntityResponse/_project in
entities.py and entities_mutations.py, and from the frontend's
KnowledgeEntity type.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0020_phase2_drop_dead_entity_id"
down_revision = "0019_phase2_mutable_versioning"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("pkos_nodes", "entity_id")


def downgrade() -> None:
    op.add_column(
        "pkos_nodes", sa.Column("entity_id", postgresql.UUID(as_uuid=True), nullable=True)
    )
