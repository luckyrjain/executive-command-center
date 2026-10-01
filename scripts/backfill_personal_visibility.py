"""Personal-data visibility/owner backfill (Spec A S1.8(b)) -- an ops command,
not a migration.

While `ECC_PERSONAL_DATA_ISOLATION` is off the application writes Gmail-derived
rows `visibility='workspace'`, and several of them owned by someone other than
the mailbox owner (the default-owner trigger's "workspace's earliest user",
or the member who ran a sync). This command makes every row of Spec A's
personal data set (`ecc.platform.connector_security.PERSONAL_ROW_PREDICATES`,
the single source of truth) private to its mailbox owner, revokes the active
`resource_grants` on those rows, and fixes the owner of Gmail-derived
`entity_aliases` (which stay workspace knowledge, DS3 (a')). It also covers
the rows DERIVED from that set (`connector_security.PERSONAL_DERIVED_
PREDICATES`, same single source): it makes private the tasks / commitments /
risks created by confirming an email recommendation while the flag was off
(plan note N23's write-time rule, applied to the rows written before it
existed), and revokes the active grants on feedback given on email
recommendations / attention items.

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
                         row's owner and visibility are left UNCHANGED and
                         it is reported (its active grants are still
                         revoked, see below). Never assigned to the
                         fallback earliest user.
    entity_aliases       whose `source_id` is `gmail_sync` evidence: owner =
                         that evidence's owner by the same rule; visibility
                         unchanged (workspace knowledge). Unresolved ->
                         unchanged + reported. Grants are not revoked (not
                         personal data).
    tasks, commitments,  rows derived from an email recommendation
    risks                (`PERSONAL_DERIVED_PREDICATES`, the rows FX3
                         share-refuses and does not count against member
                         removal): the row is the `execution_result.
                         target_id` (with the matching `target_type`) of an
                         `email_action_detected` recommendation with an
                         `execution_result` whose `operation` is `create`
                         (only the confirm writes `execution_result`).
                         Owner = that recommendation's owner (after the
                         recommendation rule above -- which never changes
                         it), `private`, active grants revoked (the row
                         copies email-derived content; with the flag on the
                         confirm writes it exactly so, plan note N23).
                         Only when the row's owner is still the one the
                         confirm wrote -- the recommendation's owner or the
                         row's `created_by` (the confirming member, the
                         flag-off owner) -- is the owner changed; once an
                         earlier run has applied this rule to the row
                         (its log snapshot's `derived_rule`; an entry made
                         while the source recommendation was
                         `owner_not_mailbox_owner` does not count) and it
                         is still `private`, only the recommendation's
                         owner counts (the rule was applied once: an
                         operator's owner fix or a transfer since STICKS). A row re-owned
                         since (an ownership transfer, member removal
                         moving it away, a manual fix) is made `private`
                         with its CURRENT owner and reported
                         (`derived_owner_changed`, every run). A row of a
                         reported recommendation (`owner_not_mailbox_
                         owner`) is made `private` with its CURRENT owner
                         and reported with that reason. Removed-owner rule
                         (DS2, as FX3 treats these rows on removal): when
                         the recommendation's owner is no longer an active
                         member, the row is STILL re-owned to them and made
                         `private` -- a removed member's personal rows stay
                         theirs, private; never handed to an admin -- and
                         reported `owner_inactive` on EVERY run while a
                         backfill (this run, or an earlier one per the log)
                         re-owned it and that member stays inactive -- so a
                         failed run loses no report. The log keeps a
                         snapshot of the row (migration 0084, see
                         `--restore`); `updated_at` is never changed. Its
                         attention items
                         (`attention_items` of entity type task/commitment/
                         risk, which copy the entity's owner, visibility
                         and title) are set to the row's new owner and
                         visibility in the same batch (and back on
                         restore). Recommendations of any other
                         type, other operations, and targets that no longer
                         exist are not touched (a deleted target exposes
                         nothing). The batch's source recommendations are
                         resolved by one set query per batch
                         (`connector_security.email_derived_sources_sql`,
                         migration 0083's index).
    recommendation_      feedback on an email recommendation / on an email
    feedback,            attention item (`PERSONAL_DERIVED_PREDICATES`):
    attention_feedback   owner (the member who gave it) and visibility
                         UNCHANGED, active grants revoked (FX3 share-refuses
                         them). Not made private: the flag-on writers still
                         write them `workspace`, owned by the actor
                         (`recommendation_events.record_feedback`,
                         `attention.record_attention_feedback`), no read
                         path serves them, and they hold the actor's own
                         label/reason, not mailbox content -- the backfill
                         only brings flag-off rows to the flag-on state.

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
private (DS2) -- for tasks/commitments/risks reported `owner_inactive`.

Unresolved reasons (CSV `reason`) -- meaning -> operator action:

    evidence_owner_ambiguous   the message id has several owners (per-thread
    evidence_owner_unknown     ids collide) / none (message and purge-log
    evidence_source_ref_       entry gone) / the `source_ref` is not a Gmail
      unrecognised             message ref. Row stays as written (usually
                               workspace) except that its active grants are
                               revoked. -> Manual review: re-own and make
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
                               the owner manually (or accept). Reported on
                               every run; a row whose cited evidence or
                               thread belongs to another owner stays
                               reported even after the fix -- accepting
                               does not clear it.
    connector_owner_unknown    a run/cursor whose connector owner could not
                               be read (unreachable today). Made private
                               with its current owner -> fix the owner
                               manually.
    derived_owner_changed      a task/commitment/risk created from an email
                               recommendation is owned by someone the rule
                               would not assign (re-owned since the confirm,
                               or since an earlier run applied the rule --
                               e.g. an operator's fix). Made private with its
                               current owner -> confirm the owner should keep
                               it, or change the owner via SQL (the app
                               refuses transfers of email-derived rows with
                               the flag on; docs/SETUP.md). Reported on every
                               run while it holds.
    (A task/commitment/risk derived from a recommendation reported
    `owner_not_mailbox_owner` is made private with its current owner and
    reported with that reason too.)
    derived_source_missing     a task/commitment/risk matched the derived-row
                               predicate but its source recommendation could
                               not be read in the batch (not reachable
                               through the app: executed recommendations are
                               redacted, never deleted). Only its grants are
                               revoked -> investigate.
    owner_inactive             a task/commitment/risk re-owned by a backfill
                               run to its recommendation's owner, who is now
                               not an active member (removed or suspended
                               before or after that run): it is
                               private to them, like the rest of their
                               personal data (DS2). The confirming member
                               loses sight of it and, with the flag on, the
                               app cannot transfer it back (share-refused);
                               docs/SETUP.md gives the per-row SQL remedy. A
                               suspended member sees it again on
                               reactivation. Reported on every run while it
                               holds (rebuilt from the log and the row).
    changed_since_backfill:    (restore) the row no longer holds what the
      rule|owner|visibility|   backfill left: `rule` -- the rules no longer
      content|attention|       yield it (reclassified, no longer
      no_snapshot              resolvable); `owner` / `visibility` --
                               changed since (transfer, re-share);
                               `content` -- a task/commitment/risk edited
                               since (its `version`/`updated_at` differ from
                               the snapshot); `attention` -- a member
                               dismissed/deferred/restored its attention
                               item since (or a regenerate re-scored it
                               after a content change); `no_snapshot` -- a
                               log row from before migration 0084 on a
                               snapshot table -> expected; no action, or
                               restore manually (docs/SETUP.md has the SQL).
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
data set and of its derived rows -- including rows already private and rows left unresolved (for
those only the grants go: they are logged with previous = current values and
their `unresolved` CSV record carries `grants_revoked`). Never on
`entity_aliases` (workspace knowledge, DS3). `rows_changed` counts logged
rows, i.e. includes grant-only changes. A row made private AND reported (an
unverified owner kept) counts in both `rows_changed` and `rows_unresolved`.

Transactions: one short transaction per batch (at most `--batch-size` rows of
one table in one workspace, keyset-paginated by id), holding the shared side
of the workspace's membership-mutation advisory lock (the key member removal
takes exclusively) and, for tasks/commitments/risks, the exclusive
`attention-regenerate:<workspace>` advisory lock `POST /attention/regenerate`
takes (so a concurrent regenerate cannot write the pre-batch owner/
visibility back onto their attention items; regenerate takes no membership
lock, so no lock cycle). The batch reads its candidate ids unlocked, then
locks their active `resource_grants` (`ORDER BY id`), then the rows
(`FOR UPDATE`, predicate re-checked) -- a grant revoke's own order (grant,
then resource row), so a revoke racing a batch waits instead of
deadlocking. Safe to interrupt
and re-run: every batch commits on its own, and a re-run skips rows that are
already right, so a second run changes nothing (exit 0 unless unresolved
rows remain). Rows that remain unresolved are reported on every run.
Timeouts: `lock_timeout` 5 s, `statement_timeout` 120 s per batch. A batch
that fails with a deadlock (40P01), lock timeout (55P03) or statement
timeout (57014) is rolled back and retried from the same keyset cursor
after 0.5/1/2/4 s (5 attempts), then the run stops (exit 2; re-run to
continue). Retrying is safe: decisions are recomputed from the rows as they
are then, the log insert is `ON CONFLICT DO NOTHING`, and a batch's counts
are added to the report only once it has committed.
Batches walk each table's primary key filtered by `workspace_id`;
`entity_aliases` has no `(workspace_id, id)` index, so its batches walk
`entity_aliases_pkey` -- fine at the measured scale (plan note N21: 100k
aliases); add that index if alias volume grows.

`--dry-run`: one `REPEATABLE READ, READ ONLY` transaction, always rolled
back; reports the same counts and unresolved ids, writes nothing.

`--restore <run_id>`: puts back the previous visibility AND owner of every
row that run logged (and sets the attention items of a restored task/
commitment/risk to match it). **Revoked grants are NOT restored** (docs/
SETUP.md has the SQL listing the grants a run revoked; re-grant manually if
needed). It refuses to run (exit 2) while `ECC_PERSONAL_DATA_ISOLATION` is
enabled in its environment -- the application would keep writing these rows
private while the restore republishes them; turn the flag off on the
application (and here) first -- unless `--allow-with-isolation` is given,
which prints a warning. It also refuses when the flag is enabled in the
application's settings (`.env` included; the message names the source).
It is a compare-and-set. The owner/visibility the run set is reconstructed
by re-applying this command's own rules to the row as it is now: where the
rules derive the owner from other rows (mailbox, connector, thread,
evidence, parent run, source recommendation) that owner; where they keep the
row's own owner (`connector_accounts`, every reported "made private with its
current owner" row, feedback, an attention item whose thread is gone) the
LOGGED previous owner -- which is what the run left there -- so an ownership
transfer made after the backfill never matches; and for aliases and
feedback (visibility never changed) the logged previous visibility. A row
is restored only while its current `(owner_id, visibility)` still equals
that reconstruction, else `changed_since_backfill:owner|visibility` (or
`:rule` when the rules no longer yield a value). Content (migration 0084's
`previous_state` snapshot, written in the batch's transaction with the rows
locked): for tasks/commitments/risks `version`, `updated_at`, whether the
backfill bumped `version` (`version_bump`, 1 when it changed owner/
visibility), and the member fields (`override_reason` as its SHA-256
digest, `dismissed_at`, `deferred_until`) of their attention items; for
email attention items those member fields. Timestamps are written in UTC
(every transaction sets `TimeZone = 'UTC'`) and compared parsed, so an
operator's `PGTZ` changes nothing. The row is unchanged only if `version == snapshot
version + version_bump`, `updated_at` equals the snapshot (the backfill never
sets it), and every attention item's member fields equal the snapshot (all
NULL for an item created since) -- else `changed_since_backfill:content` /
`:attention`. Exact and clock-free: the same SQL expression builds both
sides, so timestamps compare as identical JSON strings; owner/visibility
changes (transfer, delegation) do not move `version` and are caught by the
check above. Restoring such a row would republish what its owner wrote
believing it private. A restored row keeps its pre-backfill `updated_at`.
Log rows written before migration 0084 have no snapshot: for these
(snapshot) tables they cannot be verified and are refused as
`changed_since_backfill:no_snapshot` (restore by hand, docs/SETUP.md);
tables without a snapshot never had one and use the owner/visibility
compare-and-set alone. The remaining tables hold
system-written rows (sync state, detector output, evidence, feedback kept
as written) and rely on that check alone. The previous owner must still be
an `active` member of the row's workspace, else `previous_owner_inactive`.
Rows that no longer match the personal predicate (`no_longer_personal`) or no
longer exist (`deleted`) are itemized too, also under `--workspace-id`.
Rows already holding their previous values are skipped, so a second restore
of the same run changes nothing -- which is also why `restored` can be lower
than the number of logged rows: grant-only entries (unresolved rows whose
owner and visibility the run never changed) are skipped. Restore cannot tell
an operator's manual owner fix from the backfill's own value when the fix
set the same owner the rules assign (e.g. after a `derived_owner_changed` or
`owner_not_mailbox_owner` report) -- unless the fix also bumped `version`
(docs/SETUP.md's remedy SQL does, for tasks/commitments/risks): otherwise
such a row is restored to its logged previous owner like any other --
re-apply the fix after restoring. Each page of the log is one transaction,
retried like a backfill batch (deadlock, lock or statement timeout; 5
attempts with 0.5/1/2/4 s backoff; counted once).

Restore exit code: 0 only if every logged row within scope was restored (or
already was; `deleted` rows are always listed, even under `--workspace-id`),
1 if any row was reported, 2 on error.

Rollback runbook: every invocation gets its own run id (printed first on
stderr), and a re-run after a flag re-enable logs its changes under a new one.
To roll back, restore EVERY run id, newest first:

    SELECT run_id, min(at) FROM personal_visibility_backfill_log
    GROUP BY 1 ORDER BY 2 DESC;

Flag guard: a real run refuses (exit 2) unless `ECC_PERSONAL_DATA_ISOLATION`
is enabled in this command's own environment (set it on the command line;
the real run does not read `.env`, and says so when `.env` enables it) --
the operator's explicit confirmation that the application now writes these
rows private. Backfilling while the application still writes them
`workspace` would leave a half-private data set and a log whose restore
point keeps moving. `--dry-run` (read-only; R5 asks for it before the real
run) is allowed without it; `--restore` (the rollback path) requires it OFF,
in its environment and in the application's settings (see above).

After a real run (or a restore) the knowledge projections must be rebuilt:
`retrieval_documents` / `embedding_projections` built before the backfill may
still carry content backed by now-private evidence (plan note N29(3)). This
command does not do it itself (a separate, heavy rebuild over every node that
may load the embedding model and uses the app's own session settings); it
prints the command to run. The rebuild honours `ECC_PERSONAL_DATA_ISOLATION`
from ITS OWN process's settings (its environment, else the `.env` file,
else the default off -- it prints which): run without it, it writes claims
backed by private evidence back into the shared search text -- so after a
backfill it must run with the flag on (with the flag off it refuses while any `private` evidence
exists in scope, unless given `--allow-without-isolation`). It rebuilds in
one transaction per invocation, so run it one workspace at a time:

    PYTHONPATH=backend ECC_DATABASE_URL=<url> ECC_PERSONAL_DATA_ISOLATION=true \
        uv run python scripts/rebuild_knowledge_projections.py --workspace-id <UUID>

After a rollback (flag off on the application, every run id restored) run it
with the flag off: the evidence the restores put back is `workspace` again.
Evidence stays `private`, and keeps the rebuild's guard on, where a restore
reported the row instead (`previous_owner_inactive`, `changed_since_
backfill:*`) and where it was written while the flag was on (restore never
touches it); the guard lists those workspaces with counts -- docs/SETUP.md
covers the decision.

Retention of `personal_visibility_backfill_log` (plan notes N8/N9): it holds
ids, owners and timestamps only (table name, row id, previous owner user id,
visibility, grant count, run id, time; the 0084 snapshot adds versions,
timestamps and a SHA-256 digest of member-written attention text) -- never
content or member-written text itself. Keep every run's rows until rollout step
R7 (flags removed after two clean weeks), since they are the only input for
`--restore`. Then purge them explicitly, e.g.

    DELETE FROM personal_visibility_backfill_log WHERE run_id = '<run_id>';
    -- or, after R7: DELETE FROM personal_visibility_backfill_log WHERE at < '<R7 date>';

The table has no `workspace_id` and no foreign keys: deleting a workspace or
user leaves its log rows behind as dangling ids (restore then reports them as
`previous_owner_inactive` / `deleted`); purge them with the rest.
Back the table up (`pg_dump -t personal_visibility_backfill_log`) before
downgrading past migration 0084 (drops the snapshots: restore then refuses
snapshot tables' rows as `changed_since_backfill:no_snapshot`) or 0082 (drops
the table).

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
Several reasons are reported on every run while they hold
(`owner_not_mailbox_owner`, `derived_owner_changed`, `owner_inactive`, the
evidence/alias reasons), so exit 1 with only rows already reviewed is the
accepted steady state. docs/SETUP.md ("Backfill reasons and exit codes") is
the single operator reference for every reason and exit code.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sys
import time
from collections import Counter
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Final, Literal
from uuid import UUID, uuid4

from sqlalchemy import Connection, Engine, create_engine, make_url, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.pool import NullPool

# STD-001: a module this size "requires justification". This is an
# operator-run CLI whose runbook -- the per-table rules, every reported
# reason and what to do about it, restore semantics -- is the module
# docstring, read by the operator (with docs/SETUP.md, its summary) during
# an incident; `--help` prints a one-screen summary pointing there. It is
# one self-contained script (`scripts/` is not an importable package), and
# the backfill rules and the restore that must reconstruct and undo them are
# kept together on purpose (rule/restore symmetry: each owner rule is also
# restore's compare-and-set). Splitting it would scatter that single
# reference across modules the operator never sees.

EXIT_CLEAN: Final = 0
EXIT_UNRESOLVED: Final = 1
EXIT_ERROR: Final = 2

DATABASE_URL_ENV: Final = "ECC_DATABASE_URL"
ISOLATION_FLAG_ENV: Final = "ECC_PERSONAL_DATA_ISOLATION"

_STATEMENT_TIMEOUT: Final = "120s"
_LOCK_TIMEOUT: Final = "5s"
DEFAULT_BATCH_SIZE: Final = 500
_MAX_BATCH_SIZE: Final = 10_000

# The rebuild honours the flag from its own process's settings (environment,
# else `.env`, else off): without it, it writes private-evidence-backed claims
# back into shared search text. The command sets it explicitly.
REBUILD_COMMAND: Final = (
    "PYTHONPATH=backend ECC_DATABASE_URL=<url> ECC_PERSONAL_DATA_ISOLATION=true "
    "uv run python scripts/rebuild_knowledge_projections.py --workspace-id <UUID>"
)
# After a rollback (flag off on the application, every run id restored):
# once no `private` evidence is left in the workspace the rebuild's guard is
# released; evidence written while the flag was on keeps it on (see
# docs/SETUP.md for the decision).
REBUILD_AFTER_RESTORE_COMMAND: Final = (
    "PYTHONPATH=backend ECC_DATABASE_URL=<url> "
    "uv run python scripts/rebuild_knowledge_projections.py --workspace-id <UUID>"
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
    "tasks",
    "commitments",
    "risks",
    "recommendation_feedback",
    "attention_feedback",
)
_VERSIONED: Final[frozenset[str]] = frozenset(
    {"connector_accounts", "recommendations", "entity_aliases", "tasks", "commitments", "risks"}
)
# Rows derived from the personal data set
# (`connector_security.PERSONAL_DERIVED_PREDICATES`, the single source): the
# tasks/commitments/risks created by confirming an email recommendation
# (plan note N23; `connector_security.EMAIL_DERIVED_TARGET_TYPES`) and the
# feedback rows on an email recommendation / attention item. `main()`
# refuses to run if that map names a table not listed here.
_DERIVED_TABLES: Final[tuple[str, ...]] = ("tasks", "commitments", "risks")
_FEEDBACK_TABLES: Final[tuple[str, ...]] = ("recommendation_feedback", "attention_feedback")
# Tables whose visibility the backfill never changes (restore's
# compare-and-set expects the logged previous visibility there).
_VISIBILITY_KEPT: Final[frozenset[str]] = frozenset({"entity_aliases", *_FEEDBACK_TABLES})
# Snapshot of the row as the backfill found it (`personal_visibility_
# backfill_log.previous_state`, migration 0084), for tables holding
# member-authored content: tasks/commitments/risks (`version`, `updated_at`,
# and the member fields of their attention items) and email attention items
# (`override_reason`, `dismissed_at`, `deferred_until`). A JSONB expression
# over the unaliased table row; `--restore` compares it, exactly, with the
# same expression evaluated on the row as it is then (see `_snapshot_change`).
# Member-authored free text is never stored in the log: `override_reason`
# is kept as its SHA-256 digest (NULL stays NULL), enough for the restore's
# equality check (deep review SEC-8).
_MEMBER_ATTENTION_FIELDS: Final = (
    "'override_reason_sha256', CASE WHEN {ai}.override_reason IS NULL THEN NULL "
    "ELSE encode(sha256(convert_to({ai}.override_reason, 'UTF8')), 'hex') END, "
    "'dismissed_at', {ai}.dismissed_at, 'deferred_until', {ai}.deferred_until"
)


def _derived_snapshot_sql(table: str, entity_type: str) -> str:
    fields = _MEMBER_ATTENTION_FIELDS.format(ai="ai")
    return (
        f"jsonb_build_object('version', {table}.version, 'updated_at', {table}.updated_at, "
        f"'attention', COALESCE((SELECT jsonb_agg(jsonb_build_object('id', ai.id, {fields}) "
        f"ORDER BY ai.id) FROM attention_items ai WHERE ai.workspace_id = {table}.workspace_id "
        f"AND ai.entity_type = '{entity_type}' AND ai.entity_id = {table}.id), '[]'::jsonb))"
    )


_SNAPSHOT_SQL: Final[Mapping[str, str]] = {
    "attention_items": (
        "jsonb_build_object(" + _MEMBER_ATTENTION_FIELDS.format(ai="attention_items") + ")"
    ),
    **{
        table: _derived_snapshot_sql(table, entity_type)
        for table, entity_type in (
            ("tasks", "task"),
            ("commitments", "commitment"),
            ("risks", "risk"),
        )
    },
}

_DETECT_ACTION_PREFIX: Final = "gmail:detect_action:"
_SYNC_PREFIX: Final = "gmail:"

Reason = Literal[
    "evidence_owner_unknown",
    "evidence_owner_ambiguous",
    "evidence_source_ref_unrecognised",
    "owner_not_mailbox_owner",
    "connector_owner_unknown",
    "alias_owner_inactive",
    "derived_owner_changed",
    "owner_inactive",
    "changed_since_backfill:rule",
    "changed_since_backfill:owner",
    "changed_since_backfill:visibility",
    "changed_since_backfill:content",
    "changed_since_backfill:attention",
    "changed_since_backfill:no_snapshot",
    "previous_owner_inactive",
    "no_longer_personal",
    "deleted",
    "derived_source_missing",
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


def _derived_predicates() -> Mapping[str, str]:
    from ecc.platform.connector_security import PERSONAL_DERIVED_PREDICATES  # noqa: PLC0415

    return PERSONAL_DERIVED_PREDICATES


def _derived_sources_sql(table: str) -> str:
    from ecc.platform.connector_security import email_derived_sources_sql  # noqa: PLC0415

    return email_derived_sources_sql(table)


def _uncovered_tables() -> set[str]:
    """Tables of the personal data set or of its derived rows this command
    has no rule for (it refuses to run while any exist)."""
    from ecc.platform.connector_security import EMAIL_DERIVED_TARGET_TYPES  # noqa: PLC0415

    uncovered = (set(_personal_data_set()[0]) | set(_derived_predicates())) - set(TABLES)
    if set(EMAIL_DERIVED_TARGET_TYPES) != set(_DERIVED_TABLES):
        uncovered |= set(EMAIL_DERIVED_TARGET_TYPES) ^ set(_DERIVED_TABLES)
    return uncovered


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
    # The source recommendation is resolved per batch (`_derived_row_
    # decisions`), never as a per-row subquery here.
    **{table: f", {table}.created_by" for table in _DERIVED_TABLES},
    **{table: "" for table in _FEEDBACK_TABLES},
}


def _row_predicate(table: str) -> str:
    if table == "entity_aliases":
        return _GMAIL_ALIAS_PREDICATE
    if table in _DERIVED_TABLES or table in _FEEDBACK_TABLES:
        # One top-level `EXISTS` (planned as a semi-join, index-probed via
        # migration 0083 for the derived tables), never a per-row SubPlan.
        return _derived_predicates()[table]
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


def _locked_rows_sql(table: str) -> str:
    """The batch's rows re-read by id under `FOR UPDATE`, the predicate
    re-checked (a row may have left the set since the unlocked read)."""
    if table not in TABLES:  # allowlist before any interpolation
        raise ValueError("unknown table")
    return (
        f"SELECT {table}.id, {table}.owner_id, {table}.visibility{_EXTRA_COLUMNS[table]} "  # noqa: S608
        f"FROM {table} WHERE {table}.workspace_id = :workspace_id "
        f"AND {table}.id = ANY(:ids) AND ({_row_predicate(table)}) "
        f"ORDER BY {table}.id FOR UPDATE OF {table}"
    )


# ---------------------------------------------------------------------------
# Owner resolution
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Target:
    owner_id: UUID
    visibility: str
    # Set when the row is still changed (made private) but must also be
    # reported: its owner could not be verified as the mailbox owner, or
    # (`owner_inactive`) it is re-owned to a member who is no longer active.
    reason: Reason | None = None
    # True when `owner_id` is the row's own current owner by construction
    # ("owner unchanged"), not an owner the rules derive from other rows.
    # `--restore`'s compare-and-set then expects the logged previous owner
    # (what the backfill actually left there), so a transfer made after the
    # backfill is detected instead of trivially matching.
    keeps_owner: bool = False


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
            decisions[row["id"]] = Target(
                row["owner_id"], "private", "owner_not_mailbox_owner", keeps_owner=True
            )
        else:
            decisions[row["id"]] = Target(mailbox_owner, "private", keeps_owner=True)
    return decisions


def _decide(
    conn: Connection,
    table: str,
    workspace_id: UUID,
    rows: Sequence[Mapping[str, Any]],
    history: History,
) -> dict[UUID, Decision]:
    if table == "connector_accounts":
        return {r["id"]: Target(r["owner_id"], "private", keeps_owner=True) for r in rows}
    if table in _FEEDBACK_TABLES:
        # Owner (the member who gave the feedback) and visibility unchanged:
        # the flag-on writers still write them so (see the module
        # docstring); only their active grants are revoked.
        return {r["id"]: Target(r["owner_id"], r["visibility"], keeps_owner=True) for r in rows}
    if table in ("sync_runs", "sync_cursors"):
        return {
            r["id"]: (
                Target(r["mailbox_owner"], "private")
                if r["mailbox_owner"] is not None
                # Defensive; unreachable today: the personal predicate needs
                # the connector row, whose `owner_id` is NOT NULL.
                else Target(r["owner_id"], "private", "connector_owner_unknown", keeps_owner=True)
            )
            for r in rows
        }
    if table == "attention_items":
        return {
            r["id"]: (
                Target(r["thread_owner"], "private")
                if r["thread_owner"] is not None
                else Target(r["owner_id"], "private", keeps_owner=True)
            )
            for r in rows
        }
    if table == "recommendations":
        return _recommendation_decisions(conn, workspace_id, rows)
    if table == "ai_runs":
        return _ai_run_decisions(conn, workspace_id, rows)
    if table == "ai_run_steps":
        return _step_decisions(conn, workspace_id, rows)
    if table in _DERIVED_TABLES:
        return _derived_row_decisions(conn, table, workspace_id, rows, history)
    if table in ("pkos_evidence", "entity_aliases"):
        return _evidence_decisions(conn, table, workspace_id, rows)
    raise ValueError("unknown table")


def _evidence_decisions(
    conn: Connection, table: str, workspace_id: UUID, rows: Sequence[Mapping[str, Any]]
) -> dict[UUID, Decision]:
    """Evidence owner rule F1 (`pkos_evidence`: private; aliases keep their
    visibility)."""
    owners = _refs_owners(conn, workspace_id, [r["source_ref"] for r in rows])
    decisions: dict[UUID, Decision] = {}
    for r in rows:
        owner = _evidence_owner(r["source_ref"], owners)
        visibility = "private" if table == "pkos_evidence" else r["visibility"]
        decisions[r["id"]] = owner if isinstance(owner, Unresolved) else Target(owner, visibility)
    return decisions


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
            decisions[r["id"]] = Target(
                r["owner_id"], "private", "owner_not_mailbox_owner", keeps_owner=True
            )
        else:
            decisions[r["id"]] = Target(mailbox_owner, "private", keeps_owner=True)
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
    # A step follows its parent's decision -- including a reported one:
    # private with the parent's current owner, reported with its reason.
    # That owner is the parent's, not the step's own, so for the step it is
    # a derived owner (`keeps_owner` False).
    parent_decisions = _ai_run_decisions(conn, workspace_id, parents)
    decisions: dict[UUID, Decision] = {}
    for r in rows:
        parent = parent_decisions[r["run_id"]]
        decisions[r["id"]] = (
            replace(parent, keeps_owner=False) if isinstance(parent, Target) else parent
        )
    return decisions


@dataclass
class History:
    """Which `personal_visibility_backfill_log` entries count as EARLIER
    runs' history for the derived-row rule: every run but `exclude_run`,
    logged before `before` (None: no bound). A backfill passes its own run
    id; a restore passes the restored run and that run's first `at`, to
    rebuild the decision that run made. The earlier runs' ids are read once,
    on first use, so the per-batch lookup can use the log's `(run_id,
    table_name, row_id)` unique index with equality on every column."""

    exclude_run: UUID
    before: datetime | None = None
    _runs: list[UUID] | None = field(default=None, repr=False)

    def runs(self, conn: Connection) -> list[UUID]:
        if self._runs is None:
            self._runs = [
                r[0]
                for r in conn.execute(
                    text(
                        "SELECT DISTINCT run_id FROM personal_visibility_backfill_log "
                        "WHERE run_id <> :run AND (CAST(:before AS timestamptz) IS NULL "
                        "OR at < CAST(:before AS timestamptz))"
                    ),
                    {"run": self.exclude_run, "before": self.before},
                ).all()
            ]
        return self._runs


def _derived_sources(
    conn: Connection, table: str, workspace_id: UUID, rows: Sequence[Mapping[str, Any]]
) -> dict[UUID, UUID]:
    """Row id -> its source recommendation id, for the whole batch in ONE
    uncorrelated set query (`connector_security.email_derived_sources_sql`,
    migration 0083's index), never a per-row subquery. Several sources for
    one target cannot happen through the app (the create writes a new row);
    the lowest recommendation id wins, deterministically."""
    source_of: dict[UUID, UUID] = {}
    if not rows:
        return source_of
    for rec_id, target_id in conn.execute(
        text(_derived_sources_sql(table)),
        {"workspace_id": workspace_id, "target_ids": [str(r["id"]) for r in rows]},
    ).all():
        row_id = UUID(target_id)
        if row_id not in source_of or rec_id < source_of[row_id]:
            source_of[row_id] = rec_id
    return source_of


def _source_decisions(
    conn: Connection, workspace_id: UUID, source_ids: set[UUID]
) -> dict[UUID, Decision]:
    """The recommendation rule applied to the batch's source recommendations."""
    sources = [
        dict(s._mapping)
        for s in conn.execute(
            text(
                "SELECT id, owner_id, visibility, created_by, evidence_ids "
                "FROM recommendations WHERE workspace_id = :ws AND id = ANY(:ids)"
            ),
            {"ws": workspace_id, "ids": sorted(source_ids)},
        ).all()
    ]
    return _recommendation_decisions(conn, workspace_id, sources)


@dataclass(frozen=True)
class _Earlier:
    """One earlier run's log entry for a derived row."""

    previous_owner: UUID | None
    # Whether the derived-row rule itself decided it (snapshot key
    # `derived_rule`); an entry without the key counts as True.
    derived_rule: bool


def _derived_history(
    conn: Connection, table: str, row_ids: list[UUID], history: History
) -> dict[UUID, list[_Earlier]]:
    """Row id -> what earlier runs logged for it (equality on all three
    columns of the log's `(run_id, table_name, row_id)` unique index)."""
    earlier: dict[UUID, list[_Earlier]] = {}
    runs = history.runs(conn)
    if not runs:
        return earlier
    for row_id, previous_owner, rule in conn.execute(
        text(
            "SELECT row_id, previous_owner_id, previous_state -> 'derived_rule' "
            "FROM personal_visibility_backfill_log "
            "WHERE run_id = ANY(:runs) AND table_name = :table AND row_id = ANY(:ids) "
            "AND (CAST(:before AS timestamptz) IS NULL OR at < CAST(:before AS timestamptz))"
        ),
        {"runs": runs, "table": table, "ids": row_ids, "before": history.before},
    ).all():
        earlier.setdefault(row_id, []).append(_Earlier(previous_owner, rule is not False))
    return earlier


def _derived_decision(
    row: Mapping[str, Any], source: Decision | None, earlier: list[_Earlier]
) -> Decision:
    """The rule for one derived row (see the module docstring). `earlier`:
    what earlier runs logged for it. A row the rule was already applied to
    by an earlier run (an entry the derived rule decided -- not one made
    private while its source recommendation was unverified,
    `owner_not_mailbox_owner`) and that is still `private`: its owner is no
    longer re-derived -- an operator's owner fix or a transfer since sticks
    (reported `derived_owner_changed`)."""
    if source is None:
        return Unresolved("derived_source_missing")
    if isinstance(source, Unresolved):  # never today: recommendations always get a Target
        return source
    if source.reason is not None:
        # Never re-own to an owner the recommendation rule did not verify.
        return Target(row["owner_id"], "private", source.reason, keeps_owner=True)
    processed = row["visibility"] == "private" and any(e.derived_rule for e in earlier)
    confirm_owners = {source.owner_id} if processed else {source.owner_id, row["created_by"]}
    if row["owner_id"] not in confirm_owners:
        return Target(row["owner_id"], "private", "derived_owner_changed", keeps_owner=True)
    return Target(source.owner_id, "private")


def _derived_row_decisions(
    conn: Connection,
    table: str,
    workspace_id: UUID,
    rows: Sequence[Mapping[str, Any]],
    history: History,
) -> dict[UUID, Decision]:
    """Decisions for a batch of tasks/commitments/risks created from email
    recommendations. A row a backfill re-owned (this run, or an earlier one
    per the log) to a member who is no longer active stays theirs, private
    (DS2 -- never handed to an admin), and is reported `owner_inactive` on
    EVERY run while that holds, so the report survives a failed run.

    The log is read only for the rows where history can change the answer:
    a `private` row the rule would re-own (an earlier run may have applied
    it already), or a row whose rule owner is not active (was it re-owned
    by a backfill?) -- usually none, so the common batch never scans it."""
    source_of = _derived_sources(conn, table, workspace_id, rows)
    source_decisions = _source_decisions(conn, workspace_id, set(source_of.values()))
    sources = {
        r["id"]: source_decisions.get(source_of[r["id"]]) if r["id"] in source_of else None
        for r in rows
    }
    fresh = {r["id"]: _derived_decision(r, sources[r["id"]], []) for r in rows}
    rule_owners = {d.owner_id for d in fresh.values() if isinstance(d, Target) and d.reason is None}
    active = _active_members(conn, workspace_id, rule_owners)
    candidates = [
        r["id"]
        for r in rows
        if isinstance(d := fresh[r["id"]], Target)
        and d.reason is None
        and (
            d.owner_id not in active
            or (r["visibility"] == "private" and d.owner_id != r["owner_id"])
        )
    ]
    earlier = _derived_history(conn, table, candidates, history) if candidates else {}
    decisions: dict[UUID, Decision] = {}
    for r in rows:
        row_earlier = earlier.get(r["id"], [])
        decision = _derived_decision(r, sources[r["id"]], row_earlier)
        if isinstance(decision, Target) and decision.reason is None:
            owners = [r["owner_id"], *(e.previous_owner for e in row_earlier)]
            reowned = any(o != decision.owner_id for o in owners)
            if reowned and decision.owner_id not in active:
                decision = Target(decision.owner_id, "private", "owner_inactive")
        decisions[r["id"]] = decision
    return decisions


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

    def merge(self, other: Report) -> None:
        """Adds a committed batch's report (a retried batch is counted once)."""
        for table, s in other.stats.items():
            mine = self.stats[table]
            mine.changed += s.changed
            mine.grants_revoked += s.grants_revoked
            mine.unresolved += s.unresolved
            mine.restored += s.restored
        self.unresolved.extend(other.unresolved)


def _set_local_timeouts(conn: Connection) -> None:
    """Per-transaction settings of every backfill, dry-run and restore
    transaction: the timeouts, and `TimeZone = 'UTC'` so the snapshot
    (`_SNAPSHOT_SQL`, whose timestamps `jsonb_build_object` renders in the
    session time zone) is written the same whatever `PGTZ` the operator has."""
    conn.execute(text(f"SET LOCAL statement_timeout = '{_STATEMENT_TIMEOUT}'"))
    conn.execute(text(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT}'"))
    conn.execute(text("SET LOCAL TimeZone = 'UTC'"))


def _lock_attention_regenerate(conn: Connection, workspace_id: UUID) -> None:
    """The advisory lock `POST /attention/regenerate` takes (exclusive,
    `attention.py`) for the whole of its read-and-upsert. Held by a batch
    that sets attention items' owner/visibility (`_mirror_attention`) so a
    concurrent regenerate -- which reads entities without row locks and
    upserts the owner/visibility it read -- cannot write back the
    pre-batch values (deep review INT-4). Taken after the membership lock
    and before any row lock; regenerate takes no membership lock."""
    conn.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"attention-regenerate:{workspace_id}"},
    )


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
    # Derived tables: the derived-row owner rule itself decided this change
    # (logged as `derived_rule` in the snapshot; see `_derived_decision`).
    derived_rule: bool = False


# Decisions of the derived-row rule itself (not a verification failure of
# the source recommendation, nor an unresolvable row).
_DERIVED_RULE_REASONS: Final[frozenset[Reason | None]] = frozenset(
    {None, "owner_inactive", "derived_owner_changed"}
)


def _changes(
    table: str,
    workspace_id: UUID,
    rows: Sequence[Mapping[str, Any]],
    decisions: Mapping[UUID, Decision],
    grants: Counter[UUID],
    report: Report,
) -> list[_Change]:
    """The batch's changes (owner/visibility and/or grants); reported rows
    are added to `report`."""
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
            rule = table in _DERIVED_TABLES and decision.reason in _DERIVED_RULE_REASONS
            changes.append(
                _Change(r["id"], r["owner_id"], r["visibility"], decision, n_grants, rule)
            )
    return changes


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
    history: History | None = None,
) -> UUID | None:
    """One batch; returns the keyset cursor to continue from, or None when
    done."""
    rows, cursor = _batch_rows(conn, table, workspace_id, after, limit, write=write)
    if not rows:
        return cursor
    decisions = _decide(conn, table, workspace_id, rows, history or History(run_id))
    _require_active_alias_owners(conn, table, workspace_id, rows, decisions)
    # Grants are revoked on every personal-data row -- resolved or not
    # (S1.8(b); S2 class C is superseded by this) -- but never on aliases,
    # which stay workspace knowledge (DS3).
    grant_ids = [] if table == "entity_aliases" else [r["id"] for r in rows]
    grants = _active_grants(conn, table, workspace_id, grant_ids)
    stats = report.stats[table]
    changes = _changes(table, workspace_id, rows, decisions, grants, report)
    stats.changed += len(changes)
    stats.grants_revoked += sum(c.grants for c in changes)
    bumped = _apply(conn, table, workspace_id, run_id, changes) if write and changes else []
    if write:
        _mirror_attention(conn, table, [r["id"] for r in rows], bumped)
    return cursor


def _batch_rows(
    conn: Connection, table: str, workspace_id: UUID, after: UUID, limit: int, *, write: bool
) -> tuple[list[dict[str, Any]], UUID | None]:
    """The batch's rows and the keyset cursor to continue from (None: done).
    Writing (deep review INT-3): the candidate ids are read unlocked, then
    their active grants are locked (`ORDER BY id`), and only THEN the rows
    -- the order a grant revoke takes (grant, then resource row), so a
    revoke racing the batch waits instead of deadlocking -- re-read under
    the lock with the predicate re-checked."""
    params = {
        **_personal_data_set()[1],
        "workspace_id": workspace_id,
        "after": after,
        "limit": limit,
    }
    rows = [
        dict(r._mapping) for r in conn.execute(text(_batch_sql(table, lock=False)), params).all()
    ]
    if not rows:
        return [], None
    cursor: UUID = rows[-1]["id"]
    if not write:
        return rows, cursor
    ids = [r["id"] for r in rows]
    if table != "entity_aliases":  # aliases: grants never revoked, never locked
        _lock_grants(conn, table, workspace_id, ids)
    locked = [
        dict(r._mapping)
        for r in conn.execute(text(_locked_rows_sql(table)), {**params, "ids": ids}).all()
    ]
    _lock_attention_items(conn, table, [r["id"] for r in locked])
    return locked, cursor


def _lock_grants(conn: Connection, table: str, workspace_id: UUID, ids: list[UUID]) -> None:
    """Locks the rows' active grants, `ORDER BY id` (a stable order shared
    by every batch), BEFORE the rows themselves (see `_batch_rows`)."""
    conn.execute(
        text(
            "SELECT id FROM resource_grants WHERE workspace_id = :ws "
            "AND resource_type = :table AND resource_id = ANY(:ids) "
            "AND revoked_at IS NULL ORDER BY id FOR UPDATE"
        ),
        {"ws": workspace_id, "table": table, "ids": ids},
    )


def _apply(
    conn: Connection, table: str, workspace_id: UUID, run_id: UUID, changes: list[_Change]
) -> list[UUID]:
    """Revokes the grants, logs and applies `changes`; returns the ids whose
    owner/visibility (and `version`) changed."""
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
    _log_changes(conn, table, run_id, changes, revoked)
    return _update_rows(
        conn,
        table,
        workspace_id,
        [(c.row_id, c.target.owner_id, c.target.visibility) for c in changes],
    )


def _snapshots(conn: Connection, table: str, row_ids: list[UUID]) -> dict[UUID, dict[str, Any]]:
    """`_SNAPSHOT_SQL` evaluated on the (locked) rows; empty for tables
    without one."""
    expression = _SNAPSHOT_SQL.get(table)
    if expression is None or not row_ids:
        return {}
    return {
        r[0]: r[1]
        for r in conn.execute(
            text(f"SELECT {table}.id, {expression} FROM {table} WHERE {table}.id = ANY(:ids)"),  # noqa: S608
            {"ids": row_ids},
        ).all()
    }


def _log_changes(
    conn: Connection, table: str, run_id: UUID, changes: list[_Change], revoked: Counter[UUID]
) -> None:
    """One log row per change: previous owner/visibility, grants revoked and
    (migration 0084) the pre-change snapshot, with `version_bump` -- 1 when
    this change bumps a versioned row's `version` (its owner/visibility
    change), else 0 -- so a restore can tell the backfill's own change from
    a later one."""
    snapshots = _snapshots(conn, table, [c.row_id for c in changes])
    rows = []
    for c in changes:
        state = snapshots.get(c.row_id)
        if state is not None and table in _VERSIONED:
            moved = (c.previous_owner, c.previous_visibility) != (
                c.target.owner_id,
                c.target.visibility,
            )
            state = {**state, "version_bump": 1 if moved else 0}
        if state is not None and table in _DERIVED_TABLES:
            state = {**state, "derived_rule": c.derived_rule}
        rows.append(
            {
                "id": uuid4(),
                "run_id": run_id,
                "table_name": table,
                "row_id": c.row_id,
                "previous_visibility": c.previous_visibility,
                "previous_owner_id": c.previous_owner,
                "grants_revoked": revoked.get(c.row_id, 0),
                "previous_state": None if state is None else json.dumps(state),
            }
        )
    conn.execute(
        text(
            "INSERT INTO personal_visibility_backfill_log (id, run_id, table_name, row_id, "
            "previous_visibility, previous_owner_id, grants_revoked, previous_state) "
            "VALUES (:id, :run_id, :table_name, :row_id, :previous_visibility, "
            ":previous_owner_id, :grants_revoked, CAST(:previous_state AS jsonb)) "
            "ON CONFLICT (run_id, table_name, row_id) DO NOTHING"
        ),
        rows,
    )


# Derived table -> the `attention_items.entity_type` of its attention items,
# which copy the entity's owner/visibility at regenerate time
# (`attention.py`'s upsert).
_ATTENTION_ENTITY_TYPES: Final[Mapping[str, str]] = {
    "tasks": "task",
    "commitments": "commitment",
    "risks": "risk",
}


def _lock_attention_items(conn: Connection, table: str, row_ids: list[UUID]) -> None:
    """Locks the attention items of locked tasks/commitments/risks (`ORDER
    BY id`) before their snapshot is read or they are mirrored, so a
    member's dismiss/defer cannot slip in between (deep review INT-7). The
    item endpoints lock only the item, so no cycle."""
    entity_type = _ATTENTION_ENTITY_TYPES.get(table)
    if entity_type is None or not row_ids:
        return
    conn.execute(
        text(
            "SELECT ai.id FROM attention_items ai "  # noqa: S608
            f"JOIN {table} t ON ai.workspace_id = t.workspace_id AND ai.entity_id = t.id "
            "WHERE t.id = ANY(:ids) AND ai.entity_type = :entity_type "
            "ORDER BY ai.id FOR UPDATE OF ai"
        ),
        {"ids": row_ids, "entity_type": entity_type},
    )


# A dismissal is kept by regenerate only while `dismissed_entity_version`
# equals the entity's current version (`attention.py`'s upsert). The
# backfill's and the restore's owner/visibility change bumps `version`, so
# a dismissal made at the pre-bump version is carried to the new one
# (deep review INT-6) -- otherwise the next regenerate would un-dismiss it.
_CARRY_DISMISSAL: Final = (
    "(t.id = ANY(:bumped) AND ai.dismissed_entity_version = ai.source_entity_version "
    "AND ai.source_entity_version = t.version - 1)"
)


def _mirror_attention(
    conn: Connection, table: str, row_ids: list[UUID], bumped: Sequence[UUID] = ()
) -> None:
    """Deep review SEC-2: the attention items of a derived task/commitment/
    risk carry the entity's owner/visibility (and its title in
    `explanation`) until the next regenerate -- set them to the entity's
    current values in the same transaction (backfill and restore alike),
    carrying a dismissal across the version bump of the `bumped` rows.
    Attention is regenerable, so these updates are not logged."""
    entity_type = _ATTENTION_ENTITY_TYPES.get(table)
    if entity_type is None or not row_ids:
        return
    conn.execute(
        text(
            "UPDATE attention_items ai SET owner_id = t.owner_id, visibility = t.visibility, "  # noqa: S608
            f"source_entity_version = CASE WHEN {_CARRY_DISMISSAL} THEN t.version "
            "ELSE ai.source_entity_version END, "
            f"dismissed_entity_version = CASE WHEN {_CARRY_DISMISSAL} THEN t.version "
            "ELSE ai.dismissed_entity_version END "
            f"FROM {table} t WHERE t.id = ANY(:ids) AND ai.workspace_id = t.workspace_id "
            "AND ai.entity_type = :entity_type AND ai.entity_id = t.id "
            "AND ((ai.owner_id, ai.visibility) IS DISTINCT FROM (t.owner_id, t.visibility) "
            f"OR {_CARRY_DISMISSAL})"
        ),
        {"ids": row_ids, "entity_type": entity_type, "bumped": list(bumped)},
    )


def _update_rows(
    conn: Connection,
    table: str,
    workspace_id: UUID | None,
    values: list[tuple[UUID, UUID, str]],
) -> list[UUID]:
    """Sets `(owner_id, visibility)` per row id where it differs; bumps
    `version` on versioned tables (never `updated_at`: the backfill is not a
    content edit, and lists sort by it). Returns the ids actually updated."""
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
    history = History(run_id)
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
                            history=history,
                        )
        return report
    with engine.connect() as conn:
        with conn.begin():
            workspaces = _workspace_ids(conn, workspace_id)
        for ws in workspaces:
            for table in TABLES:
                after = _ZERO_UUID
                while after is not None:
                    after = _write_batch(
                        conn, table, ws, after, batch_size, run_id, report, history
                    )
    return report


# Deadlock, lock_timeout, statement_timeout: the batch is rolled back and
# retried from the same keyset cursor (it is idempotent: decisions are
# recomputed from the rows as they are now, the log insert is
# ON CONFLICT DO NOTHING, and its counts are only merged once committed).
RETRYABLE_SQLSTATES: Final[frozenset[str]] = frozenset({"40P01", "55P03", "57014"})
RETRY_DELAYS_SECONDS: Final[tuple[float, ...]] = (0.5, 1.0, 2.0, 4.0)  # 5 attempts


def _sqlstate(exc: BaseException) -> str | None:
    orig = getattr(exc, "orig", None)
    sqlstate = getattr(orig if orig is not None else exc, "sqlstate", None)
    return sqlstate if isinstance(sqlstate, str) else None


def _with_retries[T](
    conn: Connection, label: str, report: Report, attempt: Callable[[Report], T]
) -> T:
    """Runs `attempt` in its own transaction (with this command's timeouts),
    retried on `RETRYABLE_SQLSTATES` with backoff before giving up. Each try
    reports into a fresh `Report`, merged into `report` only once committed,
    so a retried batch or page is counted once."""
    for attempt_no in range(len(RETRY_DELAYS_SECONDS) + 1):
        attempt_report = Report()
        try:
            with conn.begin():
                _set_local_timeouts(conn)
                result = attempt(attempt_report)
        except DBAPIError as exc:
            sqlstate = _sqlstate(exc)
            if sqlstate not in RETRYABLE_SQLSTATES or attempt_no == len(RETRY_DELAYS_SECONDS):
                raise
            print(
                f"backfill_personal_visibility: {label} rolled back (sqlstate={sqlstate}); "
                f"retry {attempt_no + 1}/{len(RETRY_DELAYS_SECONDS)}",
                file=sys.stderr,
            )
            time.sleep(RETRY_DELAYS_SECONDS[attempt_no])
            continue
        report.merge(attempt_report)
        return result
    raise AssertionError("unreachable")  # pragma: no cover


def _write_batch(
    conn: Connection,
    table: str,
    workspace_id: UUID,
    after: UUID,
    batch_size: int,
    run_id: UUID,
    report: Report,
    history: History,
) -> UUID | None:
    """One committed batch (own transaction, timeouts, shared membership
    lock), retried by `_with_retries`."""

    def attempt(batch_report: Report) -> UUID | None:
        _lock_workspace_shared(conn, workspace_id)
        if table in _ATTENTION_ENTITY_TYPES:
            _lock_attention_regenerate(conn, workspace_id)
        return _process_batch(
            conn,
            table,
            workspace_id,
            after,
            batch_size,
            write=True,
            run_id=run_id,
            report=batch_report,
            history=history,
        )

    return _with_retries(conn, f"batch (table={table})", report, attempt)


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------


class UnknownRunError(Exception):
    """No log rows for the requested run id."""


@dataclass(frozen=True)
class _LogEntry:
    row_id: UUID
    previous_visibility: str
    previous_owner: UUID | None
    at: datetime
    previous_state: dict[str, Any] | None  # migration 0084; None before it


def run_restore(
    engine: Engine, *, run_id: UUID, workspace_id: UUID | None, batch_size: int
) -> Report:
    report = Report()
    with engine.connect() as conn:
        with conn.begin():
            started = conn.execute(
                text("SELECT min(at) FROM personal_visibility_backfill_log WHERE run_id = :r"),
                {"r": run_id},
            ).scalar_one()
        if started is None:
            raise UnknownRunError
        history = History(run_id, started)
        after: UUID | None = _ZERO_UUID
        while after is not None:
            page_after = after

            def attempt(page_report: Report, page_after: UUID = page_after) -> UUID | None:
                return _restore_page(
                    conn, run_id, page_after, batch_size, workspace_id, page_report, history
                )

            after = _with_retries(conn, "restore page", report, attempt)
    return report


def _restore_page(
    conn: Connection,
    run_id: UUID,
    after: UUID,
    batch_size: int,
    workspace_id: UUID | None,
    report: Report,
    history: History,
) -> UUID | None:
    """One page of the run's log (keyset by log id); returns the next
    cursor, or None when done. Retried as a whole by `_with_retries`."""
    log_rows = conn.execute(
        text(
            "SELECT id, table_name, row_id, previous_visibility, previous_owner_id, at, "
            "previous_state FROM personal_visibility_backfill_log WHERE run_id = :r "
            "AND id > :after ORDER BY id LIMIT :limit"
        ),
        {"r": run_id, "after": after, "limit": batch_size},
    ).all()
    if not log_rows:
        return None
    by_table: dict[str, list[_LogEntry]] = {}
    for _id, table, row_id, prev_vis, prev_owner, at, state in log_rows:
        if table not in TABLES:  # allowlist (plan note N8)
            raise ValueError("unknown table_name in backfill log")
        by_table.setdefault(table, []).append(_LogEntry(row_id, prev_vis, prev_owner, at, state))
    _restore_batch(conn, by_table, workspace_id, report, history)
    next_after: UUID = log_rows[-1][0]
    return next_after


def _restore_select(table: str, *, lock: bool) -> str:
    if table not in TABLES:
        raise ValueError("unknown table")
    # Locked in id order, as the backfill's batches lock.
    lock_clause = f" ORDER BY {table}.id FOR UPDATE OF {table}" if lock else ""
    return (
        f"SELECT {table}.id, {table}.workspace_id, {table}.owner_id, "  # noqa: S608
        f"{table}.visibility{_EXTRA_COLUMNS[table]} FROM {table} "
        f"WHERE {table}.id = ANY(:ids) AND ({_row_predicate(table)}) "
        f"AND (CAST(:ws AS uuid) IS NULL OR {table}.workspace_id = CAST(:ws AS uuid))"
        f"{lock_clause}"
    )


def _value_set_by_backfill(
    table: str, decision: Target, prev_owner: UUID | None, prev_vis: str
) -> tuple[UUID | None, str]:
    """The `(owner_id, visibility)` the backfill left on a row, rebuilt from
    the rules applied to the row as it is now plus the logged previous
    values: an owner the rules keep (`keeps_owner`) was the previous owner,
    and a table whose visibility the backfill never changes kept the
    previous visibility. A row whose owner was transferred since therefore
    no longer matches, even where the rules would now keep that new owner."""
    owner = prev_owner if decision.keeps_owner else decision.owner_id
    visibility = prev_vis if table in _VISIBILITY_KEPT else decision.visibility
    return owner, visibility


def _restore_batch(
    conn: Connection,
    by_table: Mapping[str, list[_LogEntry]],
    workspace_id: UUID | None,
    report: Report,
    history: History,
) -> None:
    """Compare-and-set restore of one batch of log rows (see the module
    docstring, "--restore"): a row is put back only while it still holds
    exactly what the backfill left there, and only to a previous owner who
    is still an active member of the row's workspace."""
    params = {**_personal_data_set()[1], "ws": workspace_id}
    # Advisory locks before row locks (member removal's order), sorted.
    row_workspaces: set[UUID] = set()
    for table, entries in by_table.items():
        row_workspaces.update(
            r[1]
            for r in conn.execute(
                text(_restore_select(table, lock=False)),
                {**params, "ids": [e.row_id for e in entries]},
            ).all()
        )
    for ws in sorted(row_workspaces):
        _lock_workspace_shared(conn, ws)
    if any(by_table.get(table) for table in _ATTENTION_ENTITY_TYPES):
        for ws in sorted(row_workspaces):
            _lock_attention_regenerate(conn, ws)
    for table in TABLES:
        entries = by_table.get(table, [])
        if entries:
            _restore_table(conn, table, entries, workspace_id, report, history)


def _restore_table(
    conn: Connection,
    table: str,
    entries: list[_LogEntry],
    workspace_id: UUID | None,
    report: Report,
    history: History,
) -> None:
    params = {**_personal_data_set()[1], "ws": workspace_id}
    rows = {
        m._mapping["id"]: dict(m._mapping)
        for m in conn.execute(
            text(_restore_select(table, lock=True)), {**params, "ids": [e.row_id for e in entries]}
        ).all()
    }
    # Attention items locked before the snapshot is read (INT-7).
    _lock_attention_items(conn, table, list(rows))
    for row_id, state in _snapshots(conn, table, list(rows)).items():
        rows[row_id]["current_state"] = state
    by_ws: dict[UUID, list[dict[str, Any]]] = {}
    for r in rows.values():
        by_ws.setdefault(r["workspace_id"], []).append(r)
    decisions: dict[UUID, Decision] = {}
    active: dict[UUID, set[UUID]] = {}
    prev_owners = {e.previous_owner for e in entries if e.previous_owner is not None}
    for ws, ws_rows in by_ws.items():
        decisions.update(_decide(conn, table, ws, ws_rows, history))
        active[ws] = _active_members(conn, ws, prev_owners)
    _report_absent(
        conn, table, [e.row_id for e in entries if e.row_id not in rows], workspace_id, report
    )
    values = _restore_values(table, entries, rows, decisions, active, report)
    if values:
        restored = _update_rows(conn, table, None, values)
        report.stats[table].restored += len(restored)
        _mirror_attention(conn, table, restored, restored)


def _restore_values(
    table: str,
    entries: list[_LogEntry],
    rows: Mapping[UUID, Mapping[str, Any]],
    decisions: Mapping[UUID, Decision],
    active: Mapping[UUID, set[UUID]],
    report: Report,
) -> list[tuple[UUID, UUID, str]]:
    """`(row id, previous owner, previous visibility)` to put back; refused
    rows are added to `report`."""
    values: list[tuple[UUID, UUID, str]] = []
    for entry in entries:
        row = rows.get(entry.row_id)
        if row is None:
            continue
        if (row["owner_id"], row["visibility"]) == (
            entry.previous_owner,
            entry.previous_visibility,
        ):
            continue  # already restored (idempotent re-run)
        reason = _restore_refusal(
            table, row, decisions[entry.row_id], entry, active[row["workspace_id"]]
        )
        if reason is not None or entry.previous_owner is None:
            report.stats[table].unresolved += 1
            report.unresolved.append(
                UnresolvedRow(
                    table, row["workspace_id"], entry.row_id, reason or "previous_owner_inactive"
                )
            )
            continue
        values.append((entry.row_id, entry.previous_owner, entry.previous_visibility))
    return values


def _restore_refusal(
    table: str,
    row: Mapping[str, Any],
    decision: Decision,
    entry: _LogEntry,
    active: set[UUID],
) -> Reason | None:
    """Why a logged row that is not already restored must not be restored:
    it no longer holds what the backfill left there (`changed_since_
    backfill:<cause>` -- `rule`: the backfill's rule no longer yields that
    value (reclassified, no longer resolvable); `owner` / `visibility`:
    changed since; `content` / `attention`: the snapshot differs, see
    `_snapshot_change`), or its previous owner is no longer an active
    member."""
    if not isinstance(decision, Target):
        return "changed_since_backfill:rule"
    owner, visibility = _value_set_by_backfill(
        table, decision, entry.previous_owner, entry.previous_visibility
    )
    if row["owner_id"] != owner:
        return "changed_since_backfill:owner"
    if row["visibility"] != visibility:
        return "changed_since_backfill:visibility"
    change = _snapshot_change(table, entry.previous_state, row.get("current_state"))
    if change is not None:
        return change
    if entry.previous_owner is None or entry.previous_owner not in active:
        return "previous_owner_inactive"
    return None


_ATTENTION_MEMBER_KEYS: Final = ("override_reason_sha256", "dismissed_at", "deferred_until")
_TIMESTAMP_KEYS: Final = frozenset({"updated_at", "dismissed_at", "deferred_until"})


def _normalised(key: str, value: Any) -> Any:
    """A snapshot value in comparable form: timestamps parsed and converted
    to UTC, so a snapshot rendered under another session time zone (before
    `_set_local_timeouts` pinned UTC) still compares equal."""
    if key in _TIMESTAMP_KEYS and isinstance(value, str):
        return datetime.fromisoformat(value).astimezone(UTC)
    return value


def _same(before: Mapping[str, Any], now: Mapping[str, Any], keys: Sequence[str]) -> bool:
    return all(_normalised(k, before.get(k)) == _normalised(k, now.get(k)) for k in keys)


def _snapshot_change(
    table: str, snapshot: Mapping[str, Any] | None, current: Mapping[str, Any] | None
) -> Reason | None:
    """Exact, clock-free comparison of the logged snapshot (migration 0084)
    with the row now (both built by the same `_SNAPSHOT_SQL` expression;
    timestamps compared parsed, in UTC). A row is unchanged only if:
    - versioned rows: `version == snapshot version + version_bump` (the
      backfill's own bump) and `updated_at` equals the snapshot (the
      backfill never sets it) -- else `content`;
    - attention member fields (the row's own, or each attention item of a
      task/commitment/risk; `override_reason` as its digest): equal to the
      snapshot for items that existed then, all NULL for items created
      since -- else `attention`.
    A log row written before 0084 has no snapshot: for a snapshot table it
    cannot be verified -- `no_snapshot` (restore it by hand, docs/SETUP.md);
    other tables need none (owner/visibility compare-and-set only)."""
    if table not in _SNAPSHOT_SQL:
        return None
    if snapshot is None:
        return "changed_since_backfill:no_snapshot"
    if current is None:
        return None
    if "version" in snapshot:
        expected = {**snapshot, "version": snapshot["version"] + snapshot.get("version_bump", 0)}
        if not _same(expected, current, ("version", "updated_at")):
            return "changed_since_backfill:content"
        before = {item["id"]: item for item in snapshot["attention"]}
        empty = dict.fromkeys(_ATTENTION_MEMBER_KEYS)
        for item in current["attention"]:
            if not _same(before.get(item["id"], empty), item, _ATTENTION_MEMBER_KEYS):
                return "changed_since_backfill:attention"
        return None
    if not _same(snapshot, current, _ATTENTION_MEMBER_KEYS):
        return "changed_since_backfill:attention"
    return None


def _report_absent(
    conn: Connection,
    table: str,
    absent: list[UUID],
    workspace_id: UUID | None,
    report: Report,
) -> None:
    """Logged rows the personal-predicate select did not return: still
    present (in scope) but no longer personal, in another workspace (out of
    `--workspace-id` scope, skipped), or deleted."""
    if not absent:
        return
    present_ws = {
        rid: ws
        for rid, ws in conn.execute(
            text(f"SELECT id, workspace_id FROM {table} WHERE id = ANY(:ids)"),  # noqa: S608
            {"ids": absent},
        ).all()
    }
    for row_id in absent:
        if row_id not in present_ws:
            gone = UnresolvedRow(table, None, row_id, "deleted")
        elif workspace_id is None or present_ws[row_id] == workspace_id:
            gone = UnresolvedRow(table, present_ws[row_id], row_id, "no_longer_personal")
        else:
            continue  # another workspace: outside --workspace-id scope
        report.stats[table].unresolved += 1
        report.unresolved.append(gone)


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


_HELP_SUMMARY: Final = """\
Personal-data visibility/owner backfill (Spec A S1.8(b)): makes Gmail-derived
rows and the tasks/commitments/risks created from email recommendations
private to their mailbox owner, revokes their grants, and logs every change
so `--restore <run_id>` can undo it.

Modes (from a repository checkout):

  PYTHONPATH=backend ECC_DATABASE_URL=<url> \\
      uv run python scripts/backfill_personal_visibility.py --dry-run
  PYTHONPATH=backend ECC_DATABASE_URL=<url> ECC_PERSONAL_DATA_ISOLATION=true \\
      uv run python scripts/backfill_personal_visibility.py
  PYTHONPATH=backend ECC_DATABASE_URL=<url> \\
      uv run python scripts/backfill_personal_visibility.py --restore <run_id>

The real run requires the flag on its command line; --restore refuses while
the flag is on (environment or .env) unless --allow-with-isolation.

Operator reference: docs/SETUP.md ("Personal-data isolation rollout notes":
order, durations, every reported reason, remedies) and this script's module
docstring (per-table rules, restore semantics)."""


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=_HELP_SUMMARY,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Exit codes: 0 done, nothing reported; 1 done, rows reported (see "
        "docs/SETUP.md for each reason); 2 error or refusal.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Report counts; write nothing.")
    mode.add_argument(
        "--restore",
        type=UUID,
        default=None,
        metavar="RUN_ID",
        help="Restore previous visibility and owner logged by RUN_ID (grants are NOT restored). "
        "Refused while ECC_PERSONAL_DATA_ISOLATION is enabled -- in this command's "
        "environment or in the application's settings (.env included) -- unless "
        "--allow-with-isolation.",
    )
    parser.add_argument(
        "--allow-with-isolation",
        action="store_true",
        help="--restore only: restore even though ECC_PERSONAL_DATA_ISOLATION is enabled "
        "in this command's environment or in the application's settings (.env included); "
        "the application would keep writing these rows private.",
    )
    parser.add_argument(
        "--workspace-id", type=UUID, default=None, help="Only this workspace (must exist)."
    )
    parser.add_argument(
        "--batch-size",
        type=_batch_size,
        default=DEFAULT_BATCH_SIZE,
        help=f"Rows per transaction (1-{_MAX_BATCH_SIZE}, default {DEFAULT_BATCH_SIZE}): "
        "one table in one workspace for a backfill, one page of the run's log for a restore.",
    )
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
    if mode == "backfill":
        print(
            "REMINDER: rebuild retrieval documents and embeddings now, one workspace "
            "at a time and WITH the isolation flag in the rebuild's own settings (its "
            "environment, else .env; without it the rebuild writes private-evidence-backed "
            f"claims back into shared search text): {REBUILD_COMMAND}",
            file=sys.stderr,
        )
    elif mode == "restore":
        print(
            "REMINDER: after restoring EVERY run id (flag off on the application), rebuild "
            "retrieval documents and embeddings, one workspace at a time: "
            f"{REBUILD_AFTER_RESTORE_COMMAND} (it refuses while `private` evidence remains, "
            "e.g. evidence written while the flag was on -- see docs/SETUP.md); revoked "
            "resource_grants were NOT restored (docs/SETUP.md lists them)",
            file=sys.stderr,
        )


def _workspace_exists(engine: Engine, workspace_id: UUID) -> bool:
    with engine.connect() as conn:
        exists = conn.execute(
            text("SELECT EXISTS (SELECT 1 FROM workspaces WHERE id = :id)"), {"id": workspace_id}
        ).scalar_one()
        conn.rollback()
    return bool(exists)


def _refusal(args: argparse.Namespace, mode: str) -> str | None:
    """Why this invocation must not start (flag guard), or None."""
    flag_on = isolation_flag_enabled(os.environ)
    if mode == "backfill" and not flag_on:
        dotenv = (
            f" ({ISOLATION_FLAG_ENV} is enabled in .env, but the real run reads only this "
            "command's own environment)"
            if _dotenv_enables_flag()
            else ""
        )
        return (
            f"refusing to run: set {ISOLATION_FLAG_ENV}=true on this command line -- the "
            "operator's confirmation that the application already runs with it (enable it "
            f"there and restart first); --dry-run does not need it{dotenv}"
        )
    if mode == "restore" and flag_on and not args.allow_with_isolation:
        return _restore_refusal_message("environment")
    if args.allow_with_isolation and mode != "restore":
        return "--allow-with-isolation applies to --restore only"
    return None


def _dotenv_enables_flag() -> bool:
    """Whether the application's settings enable the flag from `.env` (for
    the refusal message only; never raises)."""
    try:
        from ecc.config import get_settings, setting_source  # noqa: PLC0415

        return bool(get_settings().personal_data_isolation) and (
            setting_source(ISOLATION_FLAG_ENV) == ".env file"
        )
    except Exception:  # noqa: BLE001 -- a message hint only
        return False


def _restore_refusal_message(source: str) -> str:
    return (
        f"refusing to restore: {ISOLATION_FLAG_ENV} is enabled (from {source}) -- turn it "
        "off on the application (and here) first, or pass --allow-with-isolation"
    )


def _settings_flag_source() -> str | None:
    """Where the application's settings enable the flag (`.env` included --
    deep review SEC-5), or None when they do not."""
    from ecc.config import get_settings, setting_source  # noqa: PLC0415

    if not get_settings().personal_data_isolation:
        return None
    return setting_source(ISOLATION_FLAG_ENV)


def _run_mode(engine: Engine, args: argparse.Namespace, mode: str, run_id: UUID) -> Report:
    if mode == "restore":
        return run_restore(
            engine, run_id=run_id, workspace_id=args.workspace_id, batch_size=args.batch_size
        )
    return run_backfill(
        engine,
        workspace_id=args.workspace_id,
        batch_size=args.batch_size,
        dry_run=args.dry_run,
        run_id=run_id,
    )


def _execute(database_url: str, args: argparse.Namespace, mode: str, run_id: UUID) -> Report:
    """Runs the mode; raises `_Refused` for a refusal found on the way."""
    # Surfaces any ecc import/settings error here, and refuses to run if the
    # personal data set has grown a table this command does not know.
    if mode == "restore" and not args.allow_with_isolation:
        source = _settings_flag_source()
        if source is not None:
            raise _Refused(_restore_refusal_message(source))
    uncovered = _uncovered_tables()
    if uncovered:
        raise _Refused(
            "refusing to run: the personal data set (or its derived rows) names tables "
            "this backfill has no rule for: " + ", ".join(sorted(uncovered))
        )
    engine = create_engine(database_url, poolclass=NullPool, hide_parameters=True)
    try:
        if args.workspace_id is not None and not _workspace_exists(engine, args.workspace_id):
            raise _Refused("workspace not found")
        print(f"backfill_personal_visibility: mode={mode} run_id={run_id}", file=sys.stderr)
        if mode == "restore" and args.allow_with_isolation:
            print(
                f"backfill_personal_visibility: WARNING: restoring with {ISOLATION_FLAG_ENV} "
                "enabled (--allow-with-isolation): the application writes new rows private "
                "while these become workspace-visible again",
                file=sys.stderr,
            )
        return _run_mode(engine, args, mode, run_id)
    finally:
        engine.dispose()


class _Refused(Exception):
    """A refusal detected after argument checks (message is code-defined)."""


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    database_url = os.environ.get(DATABASE_URL_ENV, "").strip()
    mode = "dry-run" if args.dry_run else "restore" if args.restore is not None else "backfill"
    refusal = (
        f"{DATABASE_URL_ENV} must be set explicitly in the environment"
        if not database_url
        else _refusal(args, mode)
    )
    if refusal is not None:
        print(f"backfill_personal_visibility: {refusal}", file=sys.stderr)
        return EXIT_ERROR
    run_id: UUID = args.restore if args.restore is not None else uuid4()
    try:
        target = _describe_target(database_url)
        report = _execute(database_url, args, mode, run_id)
    except _Refused as exc:
        print(f"backfill_personal_visibility: {exc}", file=sys.stderr)
        return EXIT_ERROR
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
