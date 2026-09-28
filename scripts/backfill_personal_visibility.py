"""Personal-data visibility/owner backfill (Spec A S1.8(b)) -- an ops command,
not a migration.

While `ECC_PERSONAL_DATA_ISOLATION` is off the application writes Gmail-derived
rows `visibility='workspace'`, and several of them owned by someone other than
the mailbox owner (the default-owner trigger's "workspace's earliest user",
or the member who ran a sync). This command makes every row of Spec A's
personal data set (`ecc.platform.connector_security.PERSONAL_ROW_PREDICATES`,
the single source of truth) private to its mailbox owner, revokes the active
`resource_grants` on those rows, and fixes the owner of Gmail-derived
`entity_aliases` (which stay workspace knowledge, DS3 (a')).

When to run: rollout step R5 -- after `ECC_PERSONAL_DATA_ISOLATION` has been
enabled on the application (and restarted) -- and again after any re-enable
of the flag, because rows written while it was off stay `workspace`. Run
`--dry-run` first and review its report.

Per-table rules (a row is changed only when it is not already
`(target owner, target visibility)` or still has an active grant):

    connector_accounts   owner unchanged, `private`.
    sync_runs,           owner = the personal connector's owner (flag-off
    sync_cursors         rows may be owned by the member who ran the sync),
                         `private`. (If the connector owner could not be
                         read: `private`, current owner, reported
                         `connector_owner_unknown` -- unreachable today,
                         `connector_accounts.owner_id` is NOT NULL.)
    attention_items      (`entity_type='email_thread'`) owner = the thread's
                         `email_threads.owner_id` (the value the attention
                         writer copies; kept as-is if the thread is gone),
                         `private`.
    recommendations      (`email_action_detected`) owner unchanged, `private`
                         -- only if the owner is the mailbox owner:
                         `created_by` (the detect-action hook creates as the
                         mailbox owner), and no cited `gmail_sync` evidence
                         resolves (rule below) to a different owner.
                         Otherwise made `private` with its CURRENT owner and
                         reported (`owner_not_mailbox_owner`).
    ai_runs              (`email.*` task types) owner unchanged, `private` --
                         only if the owner is the mailbox owner: `actor_id`
                         (email tasks read only the actor's own mailbox), and
                         the `input_ref` thread, when it still exists, is
                         owned by that actor. Otherwise made `private` with
                         its CURRENT owner and reported
                         (`owner_not_mailbox_owner`).
    ai_run_steps         (of an email run) follow the parent run's decision:
                         the parent's target owner, `private`; a step of a
                         reported parent is reported with the parent's
                         reason too.
    pkos_evidence        (`gmail_sync`) Evidence owner rule (F1): for
                         `source_ref` `gmail:<id>` / `gmail:detect_action:
                         <id>`, the distinct owners of `<id>` across
                         `email_messages.external_message_id` and
                         `email_message_id_purge_log` (same workspace).
                         Exactly one -> that owner, `private`. Zero or
                         several (or an unrecognised `source_ref`) -> the
                         row is left UNCHANGED and reported. Never assigned
                         to the fallback earliest user.
    entity_aliases       whose `source_id` is `gmail_sync` evidence: owner =
                         that evidence's owner by the same rule; visibility
                         unchanged (workspace knowledge). Unresolved ->
                         unchanged + reported. Grants are not revoked (not
                         personal data).

Alias active-owner rule (`entity_aliases` only): a decision that would CHANGE
an alias's owner is applied only when the new owner is an `active` member of
the workspace (`users.account_id` -> `workspace_memberships.status`; "not
active" includes suspended and removed members); otherwise the alias is left
unchanged and reported `alias_owner_inactive`. Aliases are workspace knowledge
that member removal re-owns (to the node's owner) while it retains the removed
member's messages (DS2), so without this rule a re-run would hand them back to
the removed member. Every other table is assigned its mailbox / connector /
F1 / parent owner + `private` even when that member is no longer active --
the flag-on steady state: a removed member's personal rows stay theirs,
private (DS2).

Unresolved reasons (CSV `reason`) -- meaning -> operator action:

    evidence_owner_ambiguous   the message id has several owners (per-thread
    evidence_owner_unknown     ids collide) / none (message and purge-log
    evidence_source_ref_       entry gone) / the `source_ref` is not a Gmail
      unrecognised             message ref. Row stays as written (usually
                               workspace). -> Manual review: re-own and make
                               private via SQL/admin, or accept and document.
                               Reported on every run until handled.
    alias_owner_inactive       an alias (workspace knowledge) resolves to a
                               member who is not active (removed or
                               suspended). Expected after a removal (removal
                               re-owned it) -> no action; for a suspended
                               member, re-run after reactivation, or accept.
                               (Also applies to the F1 ambiguity reasons
                               above for aliases: aliases stay workspace-
                               visible, so review is optional.)
    owner_not_mailbox_owner    an email recommendation / ai run (and the
                               run's steps) is owned by someone other than
                               its mailbox owner (e.g. an ownership transfer,
                               or cited evidence / thread of another owner).
                               Made private with its current owner -> fix
                               the owner manually (or accept).
    connector_owner_unknown    a run/cursor whose connector owner could not
                               be read (unreachable today). Made private
                               with its current owner -> fix the owner
                               manually.
    changed_since_backfill     (restore) the row no longer holds what the
                               backfill set -> expected; no action, or
                               restore manually.
    previous_owner_inactive    (restore) the logged previous owner is no
                               longer an active member -> expected; no
                               action, or pick an owner manually.
    no_longer_personal         (restore) the row exists but is no longer in
                               the personal data set -> expected; no action.
    deleted                    (restore) the row no longer exists (workspace
                               unknown, so also listed under --workspace-id)
                               -> expected; no action.

Every change is logged to `personal_visibility_backfill_log` (previous
visibility and owner, grants revoked, `run_id`) in the same transaction as the
change itself; rows with `version` get it bumped. Grants revoked are the
active (`revoked_at IS NULL`) `resource_grants` on EVERY row of the personal
data set -- including rows already private and rows left unresolved (for
those only the grants go: they are logged with previous = current values and
their `unresolved` CSV record carries `grants_revoked`). Never on
`entity_aliases` (workspace knowledge, DS3). `rows_changed` counts logged
rows, i.e. includes grant-only changes.

Transactions: one short transaction per batch (at most `--batch-size` rows of
one table in one workspace, keyset-paginated by id), holding the shared side
of the workspace's membership-mutation advisory lock (the key member removal
takes exclusively) and `FOR UPDATE` on the batch's rows. Safe to interrupt
and re-run: every batch commits on its own, and a re-run skips rows that are
already right, so a second run changes nothing (exit 0 unless unresolved
rows remain). Rows that remain unresolved are reported on every run.
Batches walk each table's primary key filtered by `workspace_id`;
`entity_aliases` has no `(workspace_id, id)` index, so its batches walk
`entity_aliases_pkey` -- fine at the measured scale (plan note N21: 100k
aliases); add that index if alias volume grows.

`--dry-run`: one `REPEATABLE READ, READ ONLY` transaction, always rolled
back; reports the same counts and unresolved ids, writes nothing.

`--restore <run_id>`: puts back the previous visibility AND owner of every
row that run logged. **Revoked grants are NOT restored** (re-grant manually if
needed). It is a compare-and-set: the log stores only the previous values, so
the value the run set is reconstructed by re-applying this command's own rules
to the row as it is now; a row is restored only while its current `(owner_id,
visibility)` still equals that target (and, for aliases, its visibility still
equals the logged one -- the run never changed it). A row changed since the
backfill (re-owned, re-shared, reclassified, or no longer resolvable) is left
alone and reported `changed_since_backfill`. The previous owner must still be
an `active` member of the row's workspace, else `previous_owner_inactive`.
Rows that no longer match the personal predicate (`no_longer_personal`) or no
longer exist (`deleted`) are itemized too, also under `--workspace-id`.
Rows already holding their previous values are skipped, so a second restore
of the same run changes nothing. Restore exit code: 0 only if every logged
row within scope was restored (or already was; `deleted` rows are always
listed, even under `--workspace-id`), 1 if any row was reported, 2 on error.

Rollback runbook: every invocation gets its own run id (printed first on
stderr), and a re-run after a flag re-enable logs its changes under a new one.
To roll back, restore EVERY run id, newest first:

    SELECT run_id, min(at) FROM personal_visibility_backfill_log
    GROUP BY 1 ORDER BY 2 DESC;

Flag guard: a real run refuses (exit 2) unless `ECC_PERSONAL_DATA_ISOLATION`
is enabled in this process's environment -- the operator's explicit
confirmation that the application now writes these rows private. Backfilling
while the application still writes them `workspace` would leave a
half-private data set and a log whose restore point keeps moving.
`--dry-run` (read-only; R5 asks for it before the real run) and `--restore`
(the rollback path, run after turning the flag back off) are allowed without
it.

After a real run (or a restore) the knowledge projections must be rebuilt:
`retrieval_documents` / `embedding_projections` built before the backfill may
still carry content backed by now-private evidence (plan note N29(3)). This
command does not do it itself (a separate, heavy, non-privacy-critical
rebuild over every node that may load the embedding model and uses the app's
own session settings); it prints the command to run:

    PYTHONPATH=backend ECC_DATABASE_URL=<url> \
        uv run python scripts/rebuild_knowledge_projections.py [--workspace-id <UUID>]

Retention of `personal_visibility_backfill_log` (plan notes N8/N9): it holds
ids only (table name, row id, previous owner user id, visibility, grant
count, run id, time) -- no content. Keep every run's rows until rollout step
R7 (flags removed after two clean weeks), since they are the only input for
`--restore`. Then purge them explicitly, e.g.

    DELETE FROM personal_visibility_backfill_log WHERE run_id = '<run_id>';
    -- or, after R7: DELETE FROM personal_visibility_backfill_log WHERE at < '<R7 date>';

The table has no `workspace_id` and no foreign keys: deleting a workspace or
user leaves its log rows behind as dangling ids (restore then reports them as
`previous_owner_inactive` / `deleted`); purge them with the rest.
Back the table up before downgrading past migration 0082.

CLI usage -- from a repository checkout, pointing at the target database:

    PYTHONPATH=backend ECC_DATABASE_URL=<url> \
        uv run python scripts/backfill_personal_visibility.py --dry-run [--workspace-id <UUID>]

    PYTHONPATH=backend ECC_DATABASE_URL=<url> ECC_PERSONAL_DATA_ISOLATION=true \
        uv run python scripts/backfill_personal_visibility.py [--workspace-id <UUID>]

    PYTHONPATH=backend ECC_DATABASE_URL=<url> \
        uv run python scripts/backfill_personal_visibility.py --restore <run_id>

`ECC_DATABASE_URL` must be set explicitly (no `.env`/default fallback). Output:
CSV on stdout -- one `count` record per table and one `unresolved` record per
reported row -- left unchanged, or made private with an unverified owner (ids
and code-defined reasons only, never content); the
run id, target host/port/database name (never credentials) and a summary on
stderr. Exit codes: 0 done, nothing unresolved; 1 done, unresolved rows
reported; 2 error (flag/env/argument/workspace errors, or a database error --
batches committed before the error stay committed and are logged under the
printed run id). Errors print only the exception class and SQLSTATE.
"""

from __future__ import annotations

import argparse
import csv
import io
import os
import sys
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Final, Literal
from uuid import UUID, uuid4

from sqlalchemy import Connection, Engine, create_engine, make_url, text
from sqlalchemy.pool import NullPool

EXIT_CLEAN: Final = 0
EXIT_UNRESOLVED: Final = 1
EXIT_ERROR: Final = 2

DATABASE_URL_ENV: Final = "ECC_DATABASE_URL"
ISOLATION_FLAG_ENV: Final = "ECC_PERSONAL_DATA_ISOLATION"

_STATEMENT_TIMEOUT: Final = "120s"
_LOCK_TIMEOUT: Final = "5s"
DEFAULT_BATCH_SIZE: Final = 500
_MAX_BATCH_SIZE: Final = 10_000

REBUILD_COMMAND: Final = (
    "PYTHONPATH=backend ECC_DATABASE_URL=<url> "
    "uv run python scripts/rebuild_knowledge_projections.py [--workspace-id <UUID>]"
)

# Processing order matters: `ai_run_steps` follow their parent `ai_runs`.
# Also the allowlist of `table_name` values ever interpolated into SQL (the
# log's `table_name` is checked against it before `--restore` uses it).
TABLES: Final[tuple[str, ...]] = (
    "connector_accounts",
    "sync_runs",
    "sync_cursors",
    "attention_items",
    "recommendations",
    "ai_runs",
    "ai_run_steps",
    "pkos_evidence",
    "entity_aliases",
)
_VERSIONED: Final[frozenset[str]] = frozenset(
    {"connector_accounts", "recommendations", "entity_aliases"}
)

_DETECT_ACTION_PREFIX: Final = "gmail:detect_action:"
_SYNC_PREFIX: Final = "gmail:"

Reason = Literal[
    "evidence_owner_unknown",
    "evidence_owner_ambiguous",
    "evidence_source_ref_unrecognised",
    "owner_not_mailbox_owner",
    "connector_owner_unknown",
    "alias_owner_inactive",
    "changed_since_backfill",
    "previous_owner_inactive",
    "no_longer_personal",
    "deleted",
]


# ---------------------------------------------------------------------------
# Personal data set (single source of truth: ecc.platform.connector_security)
# ---------------------------------------------------------------------------


def _personal_data_set() -> tuple[Mapping[str, str], dict[str, object]]:
    # Imported lazily: importing `ecc` reads settings; any failure must
    # surface through `main()`'s guarded region (exit 2, class only).
    from ecc.platform.connector_security import (  # noqa: PLC0415
        PERSONAL_ROW_PREDICATES,
        personal_sql_params,
    )

    return PERSONAL_ROW_PREDICATES, personal_sql_params()


def _membership_lock_key(workspace_id: UUID) -> str:
    from ecc.platform.connector_security import membership_mutation_lock_key  # noqa: PLC0415

    return membership_mutation_lock_key(workspace_id)


# Gmail-derived alias: its `source_id` is a `gmail_sync` evidence row -- the
# same test `membership_removal._reassign_gmail_aliases` applies.
_GMAIL_ALIAS_PREDICATE: Final = (
    "EXISTS (SELECT 1 FROM pkos_evidence ev WHERE ev.workspace_id = entity_aliases.workspace_id "
    "AND ev.id = entity_aliases.source_id AND ev.source_type = :evidence_source_type)"
)

_EXTRA_COLUMNS: Final[Mapping[str, str]] = {
    "connector_accounts": "",
    "sync_runs": (
        ", (SELECT ca.owner_id FROM connector_accounts ca WHERE ca.id = "
        "sync_runs.connector_account_id AND ca.workspace_id = sync_runs.workspace_id) "
        "AS mailbox_owner"
    ),
    "sync_cursors": (
        ", (SELECT ca.owner_id FROM connector_accounts ca WHERE ca.id = "
        "sync_cursors.connector_account_id AND ca.workspace_id = sync_cursors.workspace_id) "
        "AS mailbox_owner"
    ),
    "attention_items": (
        ", (SELECT et.owner_id FROM email_threads et WHERE et.id = attention_items.entity_id "
        "AND et.workspace_id = attention_items.workspace_id) AS thread_owner"
    ),
    "recommendations": ", recommendations.created_by, recommendations.evidence_ids",
    "ai_runs": ", ai_runs.actor_id, ai_runs.input_ref->>'thread_id' AS thread_ref",
    "ai_run_steps": ", ai_run_steps.run_id",
    "pkos_evidence": ", pkos_evidence.source_ref",
    "entity_aliases": (
        ", (SELECT ev.source_ref FROM pkos_evidence ev WHERE ev.id = entity_aliases.source_id "
        "AND ev.workspace_id = entity_aliases.workspace_id) AS source_ref"
    ),
}


def _row_predicate(table: str) -> str:
    if table == "entity_aliases":
        return _GMAIL_ALIAS_PREDICATE
    return _personal_data_set()[0][table]


def _batch_sql(table: str, *, lock: bool) -> str:
    if table not in TABLES:  # allowlist before any interpolation
        raise ValueError("unknown table")
    lock_clause = f" FOR UPDATE OF {table}" if lock else ""
    return (
        f"SELECT {table}.id, {table}.owner_id, {table}.visibility{_EXTRA_COLUMNS[table]} "  # noqa: S608
        f"FROM {table} WHERE {table}.workspace_id = :workspace_id "
        f"AND ({_row_predicate(table)}) AND {table}.id > :after "
        f"ORDER BY {table}.id LIMIT :limit{lock_clause}"
    )


# ---------------------------------------------------------------------------
# Owner resolution
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Target:
    owner_id: UUID
    visibility: str
    # Set when the row is still changed (made private, owner kept) but must
    # also be reported: its owner could not be verified as the mailbox owner.
    reason: Reason | None = None


@dataclass(frozen=True)
class Unresolved:
    reason: Reason


Decision = Target | Unresolved


def message_id_from_source_ref(source_ref: str | None) -> str | None:
    """`<id>` of `gmail:detect_action:<id>` / `gmail:<id>`, else None."""
    if source_ref is None:
        return None
    for prefix in (_DETECT_ACTION_PREFIX, _SYNC_PREFIX):
        if source_ref.startswith(prefix):
            message_id = source_ref[len(prefix) :]
            return message_id or None
    return None


def _message_owners(
    conn: Connection, workspace_id: UUID, message_ids: set[str]
) -> dict[str, set[UUID]]:
    """Distinct owners of each external message id across `email_messages`
    and `email_message_id_purge_log` in `workspace_id` (Evidence owner rule
    F1; the two sources `gmail_revocation`'s ambiguity check uses)."""
    owners: dict[str, set[UUID]] = {mid: set() for mid in message_ids}
    if not message_ids:
        return owners
    rows = conn.execute(
        text(
            "SELECT external_message_id, owner_id FROM email_messages "
            "WHERE workspace_id = :ws AND external_message_id = ANY(:ids) "
            "UNION "
            "SELECT external_message_id, owner_id FROM email_message_id_purge_log "
            "WHERE workspace_id = :ws AND external_message_id = ANY(:ids)"
        ),
        {"ws": workspace_id, "ids": sorted(message_ids)},
    ).all()
    for message_id, owner_id in rows:
        owners[message_id].add(owner_id)
    return owners


def _evidence_owner(source_ref: str | None, owners: Mapping[str, set[UUID]]) -> UUID | Unresolved:
    message_id = message_id_from_source_ref(source_ref)
    if message_id is None:
        return Unresolved("evidence_source_ref_unrecognised")
    found = owners.get(message_id, set())
    if len(found) == 1:
        return next(iter(found))
    return Unresolved("evidence_owner_unknown" if not found else "evidence_owner_ambiguous")


def _refs_owners(
    conn: Connection, workspace_id: UUID, refs: Sequence[str | None]
) -> dict[str, set[UUID]]:
    ids = {mid for mid in (message_id_from_source_ref(r) for r in refs) if mid is not None}
    return _message_owners(conn, workspace_id, ids)


def _as_uuid(value: object) -> UUID | None:
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        try:
            return UUID(value)
        except ValueError:
            return None
    return None


def _ai_run_decisions(
    conn: Connection, workspace_id: UUID, rows: Sequence[Mapping[str, Any]]
) -> dict[UUID, Decision]:
    thread_ids = {t for t in (_as_uuid(r["thread_ref"]) for r in rows) if t is not None}
    thread_owner: dict[UUID, UUID] = {}
    if thread_ids:
        thread_owner = {
            tid: owner
            for tid, owner in conn.execute(
                text(
                    "SELECT id, owner_id FROM email_threads "
                    "WHERE workspace_id = :ws AND id = ANY(:ids)"
                ),
                {"ws": workspace_id, "ids": sorted(thread_ids)},
            ).all()
        }
    decisions: dict[UUID, Decision] = {}
    for row in rows:
        mailbox_owner = row["actor_id"]
        thread = _as_uuid(row["thread_ref"])
        contradicted = thread is not None and thread_owner.get(thread, mailbox_owner) != (
            mailbox_owner
        )
        if row["owner_id"] != mailbox_owner or contradicted:
            decisions[row["id"]] = Target(row["owner_id"], "private", "owner_not_mailbox_owner")
        else:
            decisions[row["id"]] = Target(mailbox_owner, "private")
    return decisions


def _decide(
    conn: Connection, table: str, workspace_id: UUID, rows: Sequence[Mapping[str, Any]]
) -> dict[UUID, Decision]:
    if table == "connector_accounts":
        return {r["id"]: Target(r["owner_id"], "private") for r in rows}
    if table in ("sync_runs", "sync_cursors"):
        return {
            r["id"]: (
                Target(r["mailbox_owner"], "private")
                if r["mailbox_owner"] is not None
                # Defensive; unreachable today: the personal predicate needs
                # the connector row, whose `owner_id` is NOT NULL.
                else Target(r["owner_id"], "private", "connector_owner_unknown")
            )
            for r in rows
        }
    if table == "attention_items":
        return {r["id"]: Target(r["thread_owner"] or r["owner_id"], "private") for r in rows}
    if table == "recommendations":
        return _recommendation_decisions(conn, workspace_id, rows)
    if table == "ai_runs":
        return _ai_run_decisions(conn, workspace_id, rows)
    if table == "ai_run_steps":
        return _step_decisions(conn, workspace_id, rows)
    if table in ("pkos_evidence", "entity_aliases"):
        owners = _refs_owners(conn, workspace_id, [r["source_ref"] for r in rows])
        decisions: dict[UUID, Decision] = {}
        for r in rows:
            owner = _evidence_owner(r["source_ref"], owners)
            visibility = "private" if table == "pkos_evidence" else r["visibility"]
            decisions[r["id"]] = (
                owner if isinstance(owner, Unresolved) else Target(owner, visibility)
            )
        return decisions
    raise ValueError("unknown table")


def _recommendation_decisions(
    conn: Connection, workspace_id: UUID, rows: Sequence[Mapping[str, Any]]
) -> dict[UUID, Decision]:
    cited = sorted({e for r in rows for e in (r["evidence_ids"] or [])})
    ref_by_evidence: dict[UUID, str] = {}
    if cited:
        ref_by_evidence = {
            eid: ref
            for eid, ref in conn.execute(
                text(
                    "SELECT id, source_ref FROM pkos_evidence WHERE workspace_id = :ws "
                    "AND id = ANY(:ids) AND source_type = :evidence_source_type"
                ),
                {
                    "ws": workspace_id,
                    "ids": cited,
                    "evidence_source_type": _personal_data_set()[1]["evidence_source_type"],
                },
            ).all()
        }
    owners = _refs_owners(conn, workspace_id, list(ref_by_evidence.values()))
    decisions: dict[UUID, Decision] = {}
    for r in rows:
        mailbox_owner = r["created_by"]
        contradicted = False
        for eid in r["evidence_ids"] or []:
            if eid not in ref_by_evidence:
                continue
            resolved = _evidence_owner(ref_by_evidence[eid], owners)
            if isinstance(resolved, UUID) and resolved != mailbox_owner:
                contradicted = True
        if r["owner_id"] != mailbox_owner or contradicted:
            decisions[r["id"]] = Target(r["owner_id"], "private", "owner_not_mailbox_owner")
        else:
            decisions[r["id"]] = Target(mailbox_owner, "private")
    return decisions


def _step_decisions(
    conn: Connection, workspace_id: UUID, rows: Sequence[Mapping[str, Any]]
) -> dict[UUID, Decision]:
    run_ids = sorted({r["run_id"] for r in rows})
    parents = [
        dict(p._mapping)
        for p in conn.execute(
            text(
                "SELECT id, owner_id, visibility, actor_id, input_ref->>'thread_id' AS thread_ref "
                "FROM ai_runs WHERE workspace_id = :ws AND id = ANY(:ids)"
            ),
            {"ws": workspace_id, "ids": run_ids},
        ).all()
    ]
    # A step follows its parent's decision -- including an unresolved one:
    # never re-own a step to a parent owner the parent rule did not verify.
    parent_decisions = _ai_run_decisions(conn, workspace_id, parents)
    return {r["id"]: parent_decisions[r["run_id"]] for r in rows}


# ---------------------------------------------------------------------------
# Backfill
# ---------------------------------------------------------------------------


@dataclass
class TableStats:
    changed: int = 0
    grants_revoked: int = 0
    unresolved: int = 0
    restored: int = 0


@dataclass(frozen=True)
class UnresolvedRow:
    table: str
    workspace_id: UUID | None  # None: `deleted` restore rows (workspace unknown)
    row_id: UUID
    reason: Reason
    grants_revoked: int = 0  # active grants revoked on this (unresolved) row


@dataclass
class Report:
    stats: dict[str, TableStats] = field(default_factory=lambda: {t: TableStats() for t in TABLES})
    unresolved: list[UnresolvedRow] = field(default_factory=list)


def _set_local_timeouts(conn: Connection) -> None:
    conn.execute(text(f"SET LOCAL statement_timeout = '{_STATEMENT_TIMEOUT}'"))
    conn.execute(text(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT}'"))


def _lock_workspace_shared(conn: Connection, workspace_id: UUID) -> None:
    conn.execute(
        text("SELECT pg_advisory_xact_lock_shared(hashtextextended(:key, 0))"),
        {"key": _membership_lock_key(workspace_id)},
    )


def _active_grants(
    conn: Connection, table: str, workspace_id: UUID, ids: list[UUID]
) -> Counter[UUID]:
    if not ids:
        return Counter()
    rows = conn.execute(
        text(
            "SELECT resource_id, count(*) FROM resource_grants WHERE workspace_id = :ws "
            "AND resource_type = :table AND resource_id = ANY(:ids) AND revoked_at IS NULL "
            "GROUP BY resource_id"
        ),
        {"ws": workspace_id, "table": table, "ids": ids},
    ).all()
    return Counter({rid: int(n) for rid, n in rows})


def _active_members(conn: Connection, workspace_id: UUID, user_ids: set[UUID]) -> set[UUID]:
    """The subset of `user_ids` that are users of `workspace_id` with an
    `active` workspace membership."""
    if not user_ids:
        return set()
    return {
        r[0]
        for r in conn.execute(
            text(
                "SELECT u.id FROM users u JOIN workspace_memberships wm "
                "ON wm.workspace_id = u.workspace_id AND wm.account_id = u.account_id "
                "WHERE u.workspace_id = :ws AND u.id = ANY(:ids) AND wm.status = 'active'"
            ),
            {"ws": workspace_id, "ids": sorted(user_ids)},
        ).all()
    }


def _require_active_alias_owners(
    conn: Connection,
    table: str,
    workspace_id: UUID,
    rows: Sequence[Mapping[str, Any]],
    decisions: dict[UUID, Decision],
) -> None:
    """`entity_aliases` only: a decision that would CHANGE an alias's owner
    is kept only when the new owner is an active member of the workspace;
    otherwise the alias is left unchanged and reported
    `alias_owner_inactive`. Aliases are the only table here that member
    removal re-owns (to the node's owner) while it retains the removed
    member's messages (DS2) -- without this the F1 rule would hand them
    straight back to the removed member. Every other table deliberately
    assigns the mailbox owner even when inactive: a removed member's
    personal rows stay theirs, private (the flag-on steady state)."""
    if table != "entity_aliases":
        return
    current = {r["id"]: r["owner_id"] for r in rows}
    new_owners = {
        d.owner_id
        for row_id, d in decisions.items()
        if isinstance(d, Target) and d.owner_id != current[row_id]
    }
    active = _active_members(conn, workspace_id, new_owners)
    for row_id, d in list(decisions.items()):
        if isinstance(d, Target) and d.owner_id != current[row_id] and d.owner_id not in active:
            decisions[row_id] = Unresolved("alias_owner_inactive")


@dataclass(frozen=True)
class _Change:
    row_id: UUID
    previous_owner: UUID
    previous_visibility: str
    target: Target
    grants: int


def _process_batch(
    conn: Connection,
    table: str,
    workspace_id: UUID,
    after: UUID,
    limit: int,
    *,
    write: bool,
    run_id: UUID,
    report: Report,
) -> UUID | None:
    """One batch; returns the last id seen (keyset cursor) or None when done."""
    rows = [
        dict(r._mapping)
        for r in conn.execute(
            text(_batch_sql(table, lock=write)),
            {
                **_personal_data_set()[1],
                "workspace_id": workspace_id,
                "after": after,
                "limit": limit,
            },
        ).all()
    ]
    if not rows:
        return None
    decisions = _decide(conn, table, workspace_id, rows)
    _require_active_alias_owners(conn, table, workspace_id, rows, decisions)
    # Grants are revoked on every personal-data row -- resolved or not
    # (S1.8(b); S2 class C is superseded by this) -- but never on aliases,
    # which stay workspace knowledge (DS3).
    grant_ids = [] if table == "entity_aliases" else [r["id"] for r in rows]
    grants = _active_grants(conn, table, workspace_id, grant_ids)
    stats = report.stats[table]
    changes: list[_Change] = []
    for r in rows:
        decision = decisions[r["id"]]
        n_grants = grants.get(r["id"], 0)
        if isinstance(decision, Unresolved):
            stats.unresolved += 1
            report.unresolved.append(
                UnresolvedRow(table, workspace_id, r["id"], decision.reason, n_grants)
            )
            if n_grants:
                # Only the grants go; owner/visibility stay as they are (the
                # log's previous values equal the current ones, so a restore
                # treats the row as already restored).
                keep = Target(r["owner_id"], r["visibility"])
                changes.append(_Change(r["id"], r["owner_id"], r["visibility"], keep, n_grants))
            continue
        if decision.reason is not None:  # made private (owner kept) AND reported
            stats.unresolved += 1
            report.unresolved.append(
                UnresolvedRow(table, workspace_id, r["id"], decision.reason, n_grants)
            )
        if (r["owner_id"], r["visibility"]) != (decision.owner_id, decision.visibility) or n_grants:
            changes.append(_Change(r["id"], r["owner_id"], r["visibility"], decision, n_grants))
    stats.changed += len(changes)
    stats.grants_revoked += sum(c.grants for c in changes)
    if write and changes:
        _apply(conn, table, workspace_id, run_id, changes)
    last_id: UUID = rows[-1]["id"]
    return last_id


def _apply(
    conn: Connection, table: str, workspace_id: UUID, run_id: UUID, changes: list[_Change]
) -> None:
    if table not in TABLES:
        raise ValueError("unknown table")
    revoked: Counter[UUID] = Counter()
    grant_ids = [c.row_id for c in changes if c.grants]
    if grant_ids:
        revoked = Counter(
            r[0]
            for r in conn.execute(
                text(
                    "UPDATE resource_grants SET revoked_at = now() WHERE workspace_id = :ws "
                    "AND resource_type = :table AND resource_id = ANY(:ids) "
                    "AND revoked_at IS NULL RETURNING resource_id"
                ),
                {"ws": workspace_id, "table": table, "ids": grant_ids},
            ).all()
        )
    conn.execute(
        text(
            "INSERT INTO personal_visibility_backfill_log (id, run_id, table_name, row_id, "
            "previous_visibility, previous_owner_id, grants_revoked) "
            "VALUES (:id, :run_id, :table_name, :row_id, :previous_visibility, "
            ":previous_owner_id, :grants_revoked) "
            "ON CONFLICT (run_id, table_name, row_id) DO NOTHING"
        ),
        [
            {
                "id": uuid4(),
                "run_id": run_id,
                "table_name": table,
                "row_id": c.row_id,
                "previous_visibility": c.previous_visibility,
                "previous_owner_id": c.previous_owner,
                "grants_revoked": revoked.get(c.row_id, 0),
            }
            for c in changes
        ],
    )
    _update_rows(
        conn,
        table,
        workspace_id,
        [(c.row_id, c.target.owner_id, c.target.visibility) for c in changes],
    )


def _update_rows(
    conn: Connection,
    table: str,
    workspace_id: UUID | None,
    values: list[tuple[UUID, UUID, str]],
) -> list[UUID]:
    """Sets `(owner_id, visibility)` per row id where it differs; bumps
    `version` on versioned tables. Returns the ids actually updated."""
    if table not in TABLES:
        raise ValueError("unknown table")
    version = f", version = {table}.version + 1" if table in _VERSIONED else ""
    rows = conn.execute(
        text(
            f"UPDATE {table} SET owner_id = v.owner_id, visibility = v.visibility{version} "  # noqa: S608
            "FROM (SELECT unnest(CAST(:ids AS uuid[])) AS id, "
            "unnest(CAST(:owners AS uuid[])) AS owner_id, "
            "unnest(CAST(:visibilities AS text[])) AS visibility) AS v "
            f"WHERE {table}.id = v.id "
            f"AND (CAST(:ws AS uuid) IS NULL OR {table}.workspace_id = CAST(:ws AS uuid)) "
            f"AND ({table}.owner_id, {table}.visibility) "
            "IS DISTINCT FROM (v.owner_id, v.visibility) "
            f"RETURNING {table}.id"
        ),
        {
            "ids": [v[0] for v in values],
            "owners": [v[1] for v in values],
            "visibilities": [v[2] for v in values],
            "ws": workspace_id,
        },
    ).all()
    return [r[0] for r in rows]


_ZERO_UUID: Final = UUID(int=0)


def _workspace_ids(conn: Connection, workspace_id: UUID | None) -> list[UUID]:
    if workspace_id is not None:
        return [workspace_id]
    return [r[0] for r in conn.execute(text("SELECT id FROM workspaces ORDER BY id")).all()]


def run_backfill(
    engine: Engine,
    *,
    workspace_id: UUID | None,
    batch_size: int,
    dry_run: bool,
    run_id: UUID,
) -> Report:
    report = Report()
    if dry_run:
        with readonly_connection(engine) as conn:
            for ws in _workspace_ids(conn, workspace_id):
                for table in TABLES:
                    after: UUID | None = _ZERO_UUID
                    while after is not None:
                        after = _process_batch(
                            conn,
                            table,
                            ws,
                            after,
                            batch_size,
                            write=False,
                            run_id=run_id,
                            report=report,
                        )
        return report
    with engine.connect() as conn:
        with conn.begin():
            workspaces = _workspace_ids(conn, workspace_id)
        for ws in workspaces:
            for table in TABLES:
                after = _ZERO_UUID
                while after is not None:
                    with conn.begin():
                        _set_local_timeouts(conn)
                        _lock_workspace_shared(conn, ws)
                        after = _process_batch(
                            conn,
                            table,
                            ws,
                            after,
                            batch_size,
                            write=True,
                            run_id=run_id,
                            report=report,
                        )
    return report


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------


class UnknownRunError(Exception):
    """No log rows for the requested run id."""


def run_restore(
    engine: Engine, *, run_id: UUID, workspace_id: UUID | None, batch_size: int
) -> Report:
    report = Report()
    with engine.connect() as conn:
        with conn.begin():
            exists = conn.execute(
                text(
                    "SELECT EXISTS (SELECT 1 FROM personal_visibility_backfill_log "
                    "WHERE run_id = :r)"
                ),
                {"r": run_id},
            ).scalar_one()
        if not exists:
            raise UnknownRunError
        after = _ZERO_UUID
        while True:
            with conn.begin():
                _set_local_timeouts(conn)
                log_rows = conn.execute(
                    text(
                        "SELECT id, table_name, row_id, previous_visibility, previous_owner_id "
                        "FROM personal_visibility_backfill_log WHERE run_id = :r AND id > :after "
                        "ORDER BY id LIMIT :limit"
                    ),
                    {"r": run_id, "after": after, "limit": batch_size},
                ).all()
                if not log_rows:
                    break
                after = log_rows[-1][0]
                by_table: dict[str, list[tuple[UUID, str, UUID | None]]] = {}
                for _id, table, row_id, prev_vis, prev_owner in log_rows:
                    if table not in TABLES:  # allowlist (plan note N8)
                        raise ValueError("unknown table_name in backfill log")
                    by_table.setdefault(table, []).append((row_id, prev_vis, prev_owner))
                _restore_batch(conn, by_table, workspace_id, report)
    return report


def _restore_select(table: str, *, lock: bool) -> str:
    if table not in TABLES:
        raise ValueError("unknown table")
    lock_clause = f" FOR UPDATE OF {table}" if lock else ""
    return (
        f"SELECT {table}.id, {table}.workspace_id, {table}.owner_id, "  # noqa: S608
        f"{table}.visibility{_EXTRA_COLUMNS[table]} FROM {table} "
        f"WHERE {table}.id = ANY(:ids) AND ({_row_predicate(table)}) "
        f"AND (CAST(:ws AS uuid) IS NULL OR {table}.workspace_id = CAST(:ws AS uuid))"
        f"{lock_clause}"
    )


def _restore_batch(
    conn: Connection,
    by_table: Mapping[str, list[tuple[UUID, str, UUID | None]]],
    workspace_id: UUID | None,
    report: Report,
) -> None:
    """Compare-and-set restore of one batch of log rows (see the module
    docstring, "--restore"): a row is put back only while it still holds
    exactly what the backfill rules assign it, and only to a previous owner
    who is still an active member of the row's workspace."""
    params = {**_personal_data_set()[1], "ws": workspace_id}
    # Advisory locks before row locks (member removal's order), sorted.
    row_workspaces: set[UUID] = set()
    for table, entries in by_table.items():
        row_workspaces.update(
            r[1]
            for r in conn.execute(
                text(_restore_select(table, lock=False)),
                {**params, "ids": [e[0] for e in entries]},
            ).all()
        )
    for ws in sorted(row_workspaces):
        _lock_workspace_shared(conn, ws)
    for table in TABLES:
        entries = by_table.get(table, [])
        if not entries:
            continue
        rows = {
            r["id"]: r
            for r in (
                dict(m._mapping)
                for m in conn.execute(
                    text(_restore_select(table, lock=True)),
                    {**params, "ids": [e[0] for e in entries]},
                ).all()
            )
        }
        by_ws: dict[UUID, list[dict[str, Any]]] = {}
        for r in rows.values():
            by_ws.setdefault(r["workspace_id"], []).append(r)
        decisions: dict[UUID, Decision] = {}
        active: dict[UUID, set[UUID]] = {}
        prev_owners = {e[2] for e in entries if e[2] is not None}
        for ws, ws_rows in by_ws.items():
            decisions.update(_decide(conn, table, ws, ws_rows))
            active[ws] = _active_members(conn, ws, prev_owners)
        # Logged rows the personal-predicate select did not return: still
        # present (in scope) but no longer personal, in another workspace
        # (out of `--workspace-id` scope, skipped), or deleted.
        absent = [e[0] for e in entries if e[0] not in rows]
        present_ws: dict[UUID, UUID] = {}
        if absent:
            present_ws = {
                rid: ws
                for rid, ws in conn.execute(
                    text(f"SELECT id, workspace_id FROM {table} WHERE id = ANY(:ids)"),  # noqa: S608
                    {"ids": absent},
                ).all()
            }
        stats = report.stats[table]
        values: list[tuple[UUID, UUID, str]] = []
        for row_id, prev_vis, prev_owner in entries:
            row = rows.get(row_id)
            if row is None:
                if row_id not in present_ws:
                    gone: UnresolvedRow = UnresolvedRow(table, None, row_id, "deleted")
                elif workspace_id is None or present_ws[row_id] == workspace_id:
                    gone = UnresolvedRow(table, present_ws[row_id], row_id, "no_longer_personal")
                else:
                    continue  # another workspace: outside --workspace-id scope
                stats.unresolved += 1
                report.unresolved.append(gone)
                continue
            row_ws = row["workspace_id"]
            current = (row["owner_id"], row["visibility"])
            if current == (prev_owner, prev_vis):
                continue  # already restored (idempotent re-run)
            decision = decisions[row_id]
            still_backfilled = (
                isinstance(decision, Target)
                and current == (decision.owner_id, decision.visibility)
                # Aliases keep their visibility; the run never changed it.
                and (table != "entity_aliases" or row["visibility"] == prev_vis)
            )
            reason: Reason | None = None
            if not still_backfilled:
                reason = "changed_since_backfill"
            elif prev_owner is None or prev_owner not in active[row_ws]:
                reason = "previous_owner_inactive"
            if reason is not None or prev_owner is None:
                stats.unresolved += 1
                report.unresolved.append(
                    UnresolvedRow(table, row_ws, row_id, reason or "previous_owner_inactive")
                )
                continue
            values.append((row_id, prev_owner, prev_vis))
        if values:
            stats.restored += len(_update_rows(conn, table, None, values))


# ---------------------------------------------------------------------------
# Connections, output, CLI
# ---------------------------------------------------------------------------


@contextmanager
def readonly_connection(engine: Engine) -> Iterator[Connection]:
    """One `REPEATABLE READ, READ ONLY` transaction, always rolled back;
    refuses to proceed unless the server confirms it is read-only."""
    with engine.connect() as conn:
        try:
            conn.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
            if conn.execute(text("SHOW transaction_read_only")).scalar_one() != "on":
                raise RuntimeError("transaction is not read-only")
            _set_local_timeouts(conn)
            yield conn
        finally:
            conn.rollback()


CSV_HEADER: Final[tuple[str, ...]] = (
    "record",
    "table_name",
    "workspace_id",
    "row_id",
    "reason",
    "rows_changed",
    "grants_revoked",
    "rows_unresolved",
)


def render_csv(report: Report, *, restore: bool) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(CSV_HEADER)
    for table in TABLES:
        s = report.stats[table]
        changed = s.restored if restore else s.changed
        writer.writerow(["count", table, "", "", "", changed, s.grants_revoked, s.unresolved])
    for u in sorted(report.unresolved, key=lambda u: (u.table, str(u.workspace_id), str(u.row_id))):
        writer.writerow(
            [
                "unresolved",
                u.table,
                u.workspace_id or "",
                u.row_id,
                u.reason,
                "",
                u.grants_revoked,
                "",
            ]
        )
    return buffer.getvalue()


def _describe_target(database_url: str) -> str:
    """Host, port and database name only -- never the user or password."""
    url = make_url(database_url)
    return f"database: host={url.host or '-'} port={url.port or '-'} name={url.database or '-'}"


def _report_error(exc: BaseException) -> None:
    """Class + SQLSTATE only: driver messages can carry row values and the
    connection string (with its password)."""
    orig = getattr(exc, "orig", None)
    source = orig if orig is not None else exc
    sqlstate = getattr(source, "sqlstate", None)
    print(
        f"backfill_personal_visibility: error class={type(source).__name__} "
        f"sqlstate={sqlstate if isinstance(sqlstate, str) else '-'}",
        file=sys.stderr,
    )


def isolation_flag_enabled(environ: Mapping[str, str]) -> bool:
    """`ECC_PERSONAL_DATA_ISOLATION` from this process's environment only
    (never `.env`), parsed with the same boolean rules as the app's
    settings (pydantic). Unset or unparseable -> False."""
    from pydantic import TypeAdapter, ValidationError  # noqa: PLC0415

    raw = environ.get(ISOLATION_FLAG_ENV)
    if raw is None:
        return False
    try:
        return TypeAdapter(bool).validate_python(raw.strip())
    except ValidationError:
        return False


def _batch_size(value: str) -> int:
    size = int(value)
    if not 1 <= size <= _MAX_BATCH_SIZE:
        raise argparse.ArgumentTypeError(f"must be between 1 and {_MAX_BATCH_SIZE}")
    return size


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Personal-data visibility/owner backfill (Spec A S1.8(b)).",
        epilog="Exit codes: 0 done, 1 unresolved rows reported, 2 error.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Report counts; write nothing.")
    mode.add_argument(
        "--restore",
        type=UUID,
        default=None,
        metavar="RUN_ID",
        help="Restore previous visibility and owner logged by RUN_ID (grants are NOT restored).",
    )
    parser.add_argument(
        "--workspace-id", type=UUID, default=None, help="Only this workspace (must exist)."
    )
    parser.add_argument("--batch-size", type=_batch_size, default=DEFAULT_BATCH_SIZE)
    return parser.parse_args(argv)


def _summarize(report: Report, *, mode: str, run_id: UUID, target: str) -> None:
    print(target, file=sys.stderr)
    print(f"mode: {mode} run_id: {run_id}", file=sys.stderr)
    for table in TABLES:
        s = report.stats[table]
        if mode == "restore":
            detail = f"restored={s.restored} not_restored={s.unresolved}"
        else:
            verb = "would_change" if mode == "dry-run" else "changed"
            detail = (
                f"{verb}={s.changed} grants_revoked={s.grants_revoked} unresolved={s.unresolved}"
            )
        print(f"{table}: {detail}", file=sys.stderr)
    print(f"unresolved rows: {len(report.unresolved)}", file=sys.stderr)
    if mode == "restore":
        print("revoked resource_grants were NOT restored", file=sys.stderr)
    if mode != "dry-run":
        print(
            "REMINDER: rebuild retrieval documents and embeddings now "
            f"(plan note N29(3)): {REBUILD_COMMAND}",
            file=sys.stderr,
        )


def _workspace_exists(engine: Engine, workspace_id: UUID) -> bool:
    with engine.connect() as conn:
        exists = conn.execute(
            text("SELECT EXISTS (SELECT 1 FROM workspaces WHERE id = :id)"), {"id": workspace_id}
        ).scalar_one()
        conn.rollback()
    return bool(exists)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    database_url = os.environ.get(DATABASE_URL_ENV, "").strip()
    if not database_url:
        print(
            f"backfill_personal_visibility: {DATABASE_URL_ENV} must be set explicitly "
            "in the environment",
            file=sys.stderr,
        )
        return EXIT_ERROR
    mode = "dry-run" if args.dry_run else "restore" if args.restore is not None else "backfill"
    if mode == "backfill" and not isolation_flag_enabled(os.environ):
        print(
            f"backfill_personal_visibility: refusing to run: {ISOLATION_FLAG_ENV} must be "
            "enabled (enable it on the application first; --dry-run and --restore do not "
            "need it)",
            file=sys.stderr,
        )
        return EXIT_ERROR
    run_id: UUID = args.restore if args.restore is not None else uuid4()
    report: Report | None = None
    try:
        target = _describe_target(database_url)
        # Surfaces any ecc import/settings error here, and refuses to run if
        # the personal data set has grown a table this command does not know.
        uncovered = set(_personal_data_set()[0]) - set(TABLES)
        if uncovered:
            raise RuntimeError("personal data set has tables this backfill does not handle")
        engine = create_engine(database_url, poolclass=NullPool, hide_parameters=True)
        try:
            if args.workspace_id is not None and not _workspace_exists(engine, args.workspace_id):
                print("backfill_personal_visibility: workspace not found", file=sys.stderr)
                return EXIT_ERROR
            print(f"backfill_personal_visibility: mode={mode} run_id={run_id}", file=sys.stderr)
            if mode == "restore":
                report = run_restore(
                    engine,
                    run_id=run_id,
                    workspace_id=args.workspace_id,
                    batch_size=args.batch_size,
                )
            else:
                report = run_backfill(
                    engine,
                    workspace_id=args.workspace_id,
                    batch_size=args.batch_size,
                    dry_run=args.dry_run,
                    run_id=run_id,
                )
        finally:
            engine.dispose()
    except UnknownRunError:
        print("backfill_personal_visibility: no log rows for that run_id", file=sys.stderr)
        return EXIT_ERROR
    except Exception as exc:  # never let a traceback print row data, a setting or the URL
        _report_error(exc)
        if mode == "backfill":
            print(
                f"backfill_personal_visibility: batches committed before the error are "
                f"logged under run_id={run_id} (re-run to continue, or --restore {run_id})",
                file=sys.stderr,
            )
        return EXIT_ERROR
    sys.stdout.write(render_csv(report, restore=mode == "restore"))
    sys.stdout.flush()
    _summarize(report, mode=mode, run_id=run_id, target=target)
    return EXIT_UNRESOLVED if report.unresolved else EXIT_CLEAN


if __name__ == "__main__":
    raise SystemExit(main())
