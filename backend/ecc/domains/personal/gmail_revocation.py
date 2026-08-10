"""Phase 10 Gmail Connector Task 7: consent revocation cascade
(`docs/superpowers/plans/2026-08-04-phase-10-gmail-connector.md`, Task 7:
"Revoking the `email` domain's `domain_consents` row calls `GmailAdapter.
disconnect()` ... and runs Phase 7's existing deletion-job pipeline ...
-- one action, both effects, no partial 'disconnected but data remains'
state reachable through this path").

**Why `email` disabling/revoking now purges data, unlike every other
domain.** For `habits`/`learning`/`travel`/`relationships`/`health`/
`finance`, `domains.py:_disable_domain` only flips `personal_domains.
enabled` and closes the open `domain_consents` row -- the underlying
records stay intact, and a separate, explicit `POST .../delete`
(`export_deletion.py`) is required to actually purge anything. `email` is
different in kind, not degree: its data is a synced *mirror of a live,
externally-connected Google account*, not a self-reported record a user
typed in. Leaving a full inbox mirror and an active OAuth grant behind
after "disabling" the domain would defeat consent revocation's entire
purpose for this domain specifically -- `PRIVACY-CONSENT-CONTRACT.md`'s
own "Unsupported" section (pre-Task-7) named exactly this gap as a
production blocker. This module is called from *both* `domains.py:_
disable_domain` (the single write path `disable_domain_endpoint` and
`revoke_consent_endpoint` both already funnel through) and `export_
deletion.py:_delete_domain_data`, so every code path that disables or
deletes the `email` domain reaches the identical cascade -- matching this
codebase's own "one real write path per state transition" convention
rather than duplicating this logic at each HTTP entry point.

**No new `deletion_jobs` schema for "retry"/"partial failure."**
`TEST-PLAN.md`'s "Required Task 7-8" bullet names "retry, partial
failure" as needed evidence; `export_deletion.py`'s own module docstring
predicted this task as the natural point to introduce a real `pending ->
worker -> completed` `deletion_jobs` lifecycle. On inspection, that
machinery is not actually needed: every write below runs inside the
*same* transaction `_disable_domain`/`_delete_domain_data` already opens
(`with session.begin():`), so a failure at any point rolls back
everything written so far in this cascade too -- there is no reachable
partially-purged state, and no `deletion_jobs` row is ever written for a
failed attempt (the existing `domain="personal_domains"`/`"personal_
domain_deletion"` idempotency-key guard in each caller means a client
retry with the *same* `Idempotency-Key` simply re-attempts the whole
cascade from scratch, since every `DELETE`/`UPDATE` here is independently
idempotent -- deleting already-gone rows or updating an already-
`disconnected` status is a no-op). Building a new `pending`/`failed`
status now, with nothing that would ever leave a row in either state for
longer than one transaction, would be exactly the kind of premature
machinery `export_deletion.py`'s own docstring already warns against.

**Deferred, best-effort Google-side revoke.** Matches `connector_
accounts.py:disable_connector_endpoint`'s own established split exactly,
for the identical reason (that endpoint's own round-23 review comment):
credential decryption is fast and local, so it happens *inside* the
caller's transaction; the actual outbound HTTPS call to Google's
`/revoke` endpoint is slow and blocking, so it must happen only *after*
that transaction commits and the session's pooled connection is released
-- `cascade_email_revocation` below returns the decrypted context(s) for
the caller to pass to `finish_gmail_revocation` post-commit, rather than
calling `disconnect()` itself.

**Loop 2 round 1 review (PR #128) -- an owner can hold more than one
`gmail` connector account.** `connector_accounts` has no owner-scoped
uniqueness (only `uq_connector_accounts_workspace_provider_external_id`,
scoped to `workspace_id`+`provider`+`external_account_id`, migration
`0044`), and `gmail_oauth.py`'s OAuth-callback `INSERT` places no
restriction on a second, distinct `external_account_id` for the same
`owner_id` -- an owner who has connected two separate Google accounts has
two simultaneously-`active` rows. The original version of this function
used `.one_or_none()`, which raised `MultipleResultsFound` in that case,
uncaught, aborting the entire cascade transaction with a 500 and leaving
the email domain impossible to disable at all for that owner. Fixed by
disconnecting every matching row, not just one -- matching the task's own
"connector_accounts row(**s**)" (plural) language.

**Loop 2 round 1 review (PR #128) -- `disable_connector_endpoint` was a
third, cascade-bypassing write path.** `connector_accounts.py`'s generic,
provider-agnostic `POST /connectors/{account_id}/disable` could disconnect
a `gmail`-provider row (including revoking the live Google OAuth grant)
without ever touching `personal_domains`/`domain_consents` or purging any
of the data this module purges -- the exact "disconnected but data
remains" state this task's own docstring above says is unreachable
"through this path" was in fact reachable through a *different* path.
Fixed by rejecting `gmail`-provider disables at that endpoint (`409
GMAIL_DISABLE_REQUIRES_DOMAIN_ENDPOINT`) and directing callers to the
domain-level endpoints that funnel through this module instead, restoring
the "one real write path" property for real.

**Loop 2 round 8 review (PR #128) -- the cross-owner ambiguity check (round
4, above) only defended against a *concurrent* collision, not a
*sequential* one.** Owner A and owner B, same workspace, coincidentally
share one message's raw `external_message_id`. A disables `email` first:
A's own check correctly sees B's still-live `email_messages` row and
leaves the shared `pkos_evidence` row alone -- but A's *own* `email_
messages` row for that id is still deleted (correctly; it is A's own
data), which also erases the only signal that made the id ambiguous. B
later disables `email` independently: B's own check now finds no *other*
owner's live row for that id, concludes it is safe, and deletes the
shared evidence row after all. Fixed by `email_message_id_purge_log`
(migration `0076`): every cascade logs every `external_message_id` it
processes, unconditionally, before this owner's own `email_messages` rows
are deleted; the ambiguity check now also treats an id as ambiguous if a
*different* owner has a log entry for it, not only a still-live row --
see migration `0076`'s own docstring for the full reasoning, including
why that log is itself never purged by any code path in this codebase.

**Loop 2 round 8 review (PR #128) -- `revoke_consent_endpoint` could
replay a stale `consent_id`, fixed round 9 for an idempotency-key
regression the first fix introduced.** `domains.py`'s consent lookup did
not check whether a `consent_id` had since been superseded by a later
grant before resolving `domain_key` and triggering this module's cascade
-- for every domain except `email` that is harmless (disable/re-enable is
fully reversible), but for `email` a stale, revoked-then-re-granted-over
`consent_id` would still successfully trigger a full disable and data-
purging cascade against the *current* state. Round 8's own first fix
(`AND revoked_at IS NULL`) closed that but broke a legitimate case: an
`Idempotency-Key` retry of a revoke that already succeeded, no re-grant
in between, now 404'd instead of returning the cached response, since the
lookup ran before `_disable_domain`'s own idempotency-cache check ever
got a chance to fire. Round 9's fix distinguishes the two: a revoked
consent is only rejected if a *later* grant for the same domain actually
superseded it. Fixed at the lookup site (`domains.py:revoke_consent_
endpoint`), not here -- see that function's own updated comment.

**What is deliberately NOT deleted.**
- `pkos_nodes` (the resolved person entities themselves): `gmail_adapter.
  py:_resolve_or_create_person` deduplicates at *workspace* scope, not
  owner scope -- a node this owner's Gmail sync first created may be the
  same node a different owner's own Gmail sync, or an entirely different
  domain's entity resolution, has since reused. Deleting it here could
  destroy state another owner or domain still depends on. Only the
  `gmail_sync`-sourced `pkos_evidence` rows this owner's own email
  produced are removed; an evidence-less node (a bare `canonical_name`,
  no content) carries no privacy exposure of its own.
- `recommendations` rows already `status = 'executed'`: Task 4's own
  create-path confirms these into a real, independent `tasks`/
  `commitments`/`risks` row via the same internal creation function every
  other caller uses -- that downstream row is the owner's own work
  product, not Gmail data, and survives. The recommendation row itself is
  redacted in place (its own `rationale`/`proposed_action`/`evidence_ids`
  nulled), matching `gmail_threads.py:forget_thread_endpoint`'s own "null
  the content, keep the audit skeleton" convention exactly -- the fact
  that a recommendation once existed, was confirmed, and led to that task
  remains visible; the Gmail-derived narrative behind it does not.
- A `pkos_evidence` row whose `source_ref` id ambiguously collides with a
  *different* owner's own message (`email_messages.external_message_id`
  is only unique per `(workspace_id, thread_id)`, not per-workspace, Loop
  2 round 4 review finding): left alone rather than purged, since deleting
  it could destroy the other owner's own still-live evidence -- see the
  ambiguity check's own inline comments below for the full mechanism,
  including the sequential-disable case migration `0076` closes.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

from ecc.auth import AuthContext
from ecc.domains.engineering.connector_accounts import _get_encrypted_credential
from ecc.domains.engineering.connectors import ConnectorAccountContext
from ecc.domains.engineering.crypto import decrypt_credential

from .gmail_adapter import GmailAdapter

# Module-level singleton, mirroring `gmail_threads.py`'s own identical
# "constructed with no arguments, reads `ECC_GMAIL_OAUTH_*` lazily" shape.
_adapter = GmailAdapter()

PendingGmailRevoke = tuple[GmailAdapter, ConnectorAccountContext]

_REDACTED_RATIONALE = "Source email no longer available -- email consent was revoked."


def cascade_email_revocation(
    session: Session, auth: AuthContext, now: datetime
) -> list[PendingGmailRevoke]:
    """Purges this owner's email-derived records (subject to the three
    deliberate exceptions the module docstring's "What is deliberately
    NOT deleted" section names) and marks the owner's `gmail` connector
    account(s) `disconnected` -- called from within an already-open
    transaction, so every write here commits or rolls back atomically
    with whatever domain-level state change triggered it. Returns pending
    Google-side revoke info for the caller to pass to `finish_gmail_
    revocation` after that transaction commits (see module docstring).
    """
    params = {"workspace_id": auth.workspace_id, "owner_id": auth.user_id}

    # `attention_items.owner_id` (migration `0063`'s Phase 8 authz
    # widening) is explicitly set at write time from the underlying
    # entity's own owner (`attention.py`'s email-thread branch selects
    # `et.owner_id` directly into the row), so it is a reliable scoping
    # key here -- unlike `pkos_evidence`/`pkos_nodes` below, whose own
    # `owner_id` (added by the same migration) is backfilled/defaulted to
    # a generic "workspace's earliest-created user" fallback with no
    # correlation to which owner's Gmail sync actually produced a given
    # row, and so is deliberately NOT trusted for scoping there.
    session.execute(
        text(
            "DELETE FROM attention_items WHERE workspace_id = :workspace_id "
            "AND owner_id = :owner_id AND entity_type = 'email_thread'"
        ),
        params,
    )

    # `recommendations.owner_id` (migration `0063`) is backfilled/
    # defaulted from `created_by`, and Task 5's own `email.detect_action`
    # always creates under the run's own owner-scoped `AuthContext`
    # (`created_by = created_at actor = the email's owner`), so `owner_id`
    # here is reliable too. See module docstring for why `executed` rows
    # are redacted in place rather than deleted.
    session.execute(
        text(
            "UPDATE recommendations SET rationale = :redacted, "
            "proposed_action = '{}'::jsonb, evidence_ids = '{}' "
            "WHERE workspace_id = :workspace_id AND owner_id = :owner_id "
            "AND recommendation_type = 'email_action_detected' AND status = 'executed'"
        ),
        {"redacted": _REDACTED_RATIONALE, **params},
    )
    session.execute(
        text(
            "DELETE FROM recommendations WHERE workspace_id = :workspace_id "
            "AND owner_id = :owner_id AND recommendation_type = 'email_action_detected' "
            "AND status != 'executed'"
        ),
        params,
    )

    # `pkos_evidence` gained an `owner_id` column from the same migration
    # `0063`, but -- unlike `attention_items`/`recommendations` above --
    # it is never set at write time here (`gmail_adapter.py`'s own two
    # `INSERT INTO pkos_evidence` sites list no `owner_id` column at all)
    # and so falls back to that migration's generic "workspace's
    # earliest-created user" default, unrelated to which owner's Gmail
    # sync actually produced a given evidence row -- deliberately NOT
    # trusted for scoping. `external_message_id`s must be captured BEFORE
    # `email_messages` is deleted below (no FK from `pkos_evidence` back
    # to it -- the identical "an audit/derived record must remain
    # resolvable independent of the content it once pointed at" reasoning
    # migration `0075`'s own docstring already gives for `deletion_jobs.
    # resource_id`), matched by the exact `source_ref` strings `gmail_
    # adapter.py`'s two write sites deterministically construct from
    # `external_message_id` (`_resolve_or_create_person`: `f"gmail:
    # {external_message_id}"`; `_register_message_evidence`: `f"gmail:
    # detect_action:{external_message_id}"`). Does NOT touch `pkos_
    # nodes` -- see module docstring's "what is deliberately NOT
    # deleted" section.
    external_message_ids: list[str] = [
        row[0]
        for row in session.execute(
            text(
                "SELECT external_message_id FROM email_messages "
                "WHERE workspace_id = :workspace_id AND owner_id = :owner_id"
            ),
            params,
        )
    ]
    if external_message_ids:
        # `email_messages.external_message_id` (Gmail's own opaque message
        # id) is only uniqueness-constrained per `(workspace_id, thread_id)`
        # (`uq_email_messages_thread_external_id`, migration `0069`), not
        # per-workspace -- so nothing in the schema rules out two different
        # owners in the same workspace, each with their own connected
        # Gmail account, happening to have a message sharing the same raw
        # id (Loop 2 round 4 review finding). Since `source_ref` matching
        # below has no owner qualifier of its own (unlike every other
        # DELETE/UPDATE in this function), such a collision would silently
        # purge a *different* owner's evidence too. Excluding any id that
        # also belongs to another owner -- either a still-live `email_
        # messages` row, or a `email_message_id_purge_log` row a *previous*
        # cascade left behind for it (Loop 2 round 8 review: a live-row-only
        # check misses the *sequential* case, where the other owner's own
        # cascade already ran and deleted the very row that would have
        # proven the collision -- see migration `0076`'s own docstring) --
        # closes that window without trusting `pkos_evidence.owner_id`
        # (still not reliable -- see above): this owner's own cascade
        # simply skips evidence for an ambiguous id rather than risk
        # deleting across the boundary; that one row is left behind rather
        # than purged, a strictly safer failure mode than the alternative.
        #
        # Loop 2 round 17 review finding (MEDIUM): the round-4/round-8 fixes
        # above only ever defend against a colliding id that is already
        # *committed* -- either still live or logged by a *finished* prior
        # cascade. Two owners' cascades genuinely overlapping in time (both
        # in flight, neither committed yet) each run this ambiguity check
        # under default READ COMMITTED isolation, so each can independently
        # see "not ambiguous" and both proceed to the owner-unscoped
        # `pkos_evidence` DELETE below -- the one statement in this
        # function with no `owner_id` qualifier of its own (`pkos_evidence.
        # owner_id` is never trustworthy, see above). Nothing else in this
        # cascade serializes two *different* owners against each other: the
        # `personal_domains`/`connector_accounts` row locks and the
        # idempotency advisory lock are all scoped per-owner. Closed here
        # with a `pg_advisory_xact_lock` keyed on `(workspace_id, id)` for
        # every candidate id, acquired in sorted order to avoid a deadlock
        # if two colliding owners' candidate sets overlap on more than one
        # id -- the same underlying `pg_advisory_xact_lock(hashtextextended(
        # ...))` primitive this codebase's own idempotency locks already use
        # (e.g. `calendar/events.py:_lock_idempotency`), but a genuinely
        # different shape: those lock exactly one Python-built key per call;
        # this needs a *set* of keys locked together, so the concatenation
        # and the per-id loop both move into one SQL statement (`unnest(...)
        # ... ORDER BY id`) rather than a Python-side helper.
        #
        # Whichever cascade acquires the lock for a given id first is now
        # authoritative for it; the loser's own ambiguity check, once
        # unblocked, correctly observes the winner's now-committed purge-
        # log entry (or live row) and treats the id as ambiguous.
        session.execute(
            text(
                "SELECT pg_advisory_xact_lock(hashtextextended(:workspace_id || ':' || id, 0)) "
                "FROM unnest(CAST(:candidate_ids AS text[])) AS id ORDER BY id"
            ),
            {"workspace_id": str(auth.workspace_id), "candidate_ids": external_message_ids},
        )

        # Every id this cascade processes (ambiguous or not) is logged
        # first, unconditionally -- a *future* other-owner cascade needs
        # this owner's own entry to still exist after this owner's own
        # `email_messages` rows are gone below.
        session.execute(
            text(
                "INSERT INTO email_message_id_purge_log "
                "(workspace_id, external_message_id, owner_id, purged_at) "
                "SELECT :workspace_id, id, :owner_id, :now "
                "FROM unnest(CAST(:candidate_ids AS text[])) AS id "
                "ON CONFLICT (workspace_id, external_message_id, owner_id) DO NOTHING"
            ),
            {
                "workspace_id": auth.workspace_id,
                "owner_id": auth.user_id,
                "candidate_ids": external_message_ids,
                "now": now,
            },
        )
        ambiguous_ids = {
            row[0]
            for row in session.execute(
                text(
                    "SELECT DISTINCT external_message_id FROM email_messages "
                    "WHERE workspace_id = :workspace_id AND owner_id != :owner_id "
                    "AND external_message_id = ANY(:candidate_ids) "
                    "UNION "
                    "SELECT DISTINCT external_message_id FROM email_message_id_purge_log "
                    "WHERE workspace_id = :workspace_id AND owner_id != :owner_id "
                    "AND external_message_id = ANY(:candidate_ids)"
                ),
                {
                    "workspace_id": auth.workspace_id,
                    "owner_id": auth.user_id,
                    "candidate_ids": external_message_ids,
                },
            )
        }
        safe_ids = [mid for mid in external_message_ids if mid not in ambiguous_ids]
        if safe_ids:
            refs = [f"gmail:{mid}" for mid in safe_ids] + [
                f"gmail:detect_action:{mid}" for mid in safe_ids
            ]
            session.execute(
                text(
                    "DELETE FROM pkos_evidence WHERE workspace_id = :workspace_id "
                    "AND source_type = 'gmail_sync' AND source_ref = ANY(:refs)"
                ),
                {"workspace_id": auth.workspace_id, "refs": refs},
            )

    # `email_messages` before `email_threads` -- Task 1's own migration
    # `0069` leaves the child->parent relationship with no `ondelete`
    # cascade (matching every other Phase 7 domain's own explicit-order
    # convention, `export_deletion.py:_CHILD_TABLES_DELETE_ORDER`, rather
    # than relying on a cascade), so the order here is what keeps this
    # FK-safe.
    session.execute(
        text(
            "DELETE FROM email_messages WHERE workspace_id = :workspace_id AND owner_id = :owner_id"
        ),
        params,
    )
    session.execute(
        text(
            "DELETE FROM email_threads WHERE workspace_id = :workspace_id AND owner_id = :owner_id"
        ),
        params,
    )

    # `connector_accounts.owner_id` (migration `0063`) is set explicitly
    # at write time by `gmail_oauth.py`'s own OAuth-callback `INSERT`
    # (the connecting user, `auth.user_id`), so it is reliable here too.
    # `FOR UPDATE`: mirrors `connector_accounts.py:get_connector_account
    # (..., for_update=True)`'s own established reason exactly (serializes
    # a concurrent mutator of the same account -- e.g. a racing generic
    # `disable_connector_endpoint` call, or another concurrent revocation
    # request) -- Loop 2 round 1 review flagged this SELECT as an
    # unacknowledged deviation from the pattern the module docstring says
    # it matches "exactly". No matching rows is the common case (every
    # domain-level disable/delete after the first already left the
    # connector `disconnected`), not an error.
    #
    # `.all()`, not `.one_or_none()`: see module docstring's own "Loop 2
    # round 1 review -- an owner can hold more than one `gmail` connector
    # account" paragraph for why more than one matching row here is a real,
    # expected case rather than a data-integrity error (Loop 2 round 8
    # review: this paragraph used to restate that same narrative in full a
    # second time here rather than cross-referencing it).
    # `ORDER BY id`: Loop 2 round 2 review -- a multi-row `FOR UPDATE`
    # with no deterministic order risks a classic lock-order deadlock
    # against a second concurrent cascade for the same owner (e.g. a
    # racing `disable_domain_endpoint` and `revoke_consent_endpoint` call)
    # if Postgres ever visited the matching rows in different orders for
    # the two transactions. Ordering by `id` guarantees every concurrent
    # caller acquires these locks in the same sequence.
    account_rows = session.execute(
        text(
            "SELECT id, external_account_id FROM connector_accounts "
            "WHERE workspace_id = :workspace_id AND provider = 'gmail' "
            "AND owner_id = :owner_id AND status != 'disconnected' "
            "ORDER BY id FOR UPDATE"
        ),
        params,
    ).all()
    pending_revokes: list[PendingGmailRevoke] = []
    for account_id, external_account_id in account_rows:
        # Matches `connector_accounts.py:disable_connector_endpoint`'s
        # own established split exactly -- see module docstring.
        try:
            encrypted = _get_encrypted_credential(session, auth.workspace_id, account_id)
            pending_revokes.append(
                (
                    _adapter,
                    ConnectorAccountContext(
                        workspace_id=auth.workspace_id,
                        connector_account_id=account_id,
                        external_account_id=external_account_id,
                        credential=decrypt_credential(encrypted),
                    ),
                )
            )
        except Exception:  # noqa: BLE001 -- best-effort, never blocks the cascade
            pass
        session.execute(
            text(
                "UPDATE connector_accounts SET status = 'disconnected', "
                "disconnected_at = :now, updated_at = :now, updated_by = :actor_id, "
                "version = version + 1 WHERE id = :id"
            ),
            {"now": now, "actor_id": auth.user_id, "id": account_id},
        )

    return pending_revokes


def finish_gmail_revocation(pending: list[PendingGmailRevoke]) -> None:
    """Call only after the transaction `cascade_email_revocation` ran
    inside has committed and the caller's session connection has been
    released (`session.close()`) -- see module docstring. Revokes every
    pending entry (an owner may have had more than one connected Gmail
    account) -- each is independently best-effort, so one failing does not
    stop the rest from being attempted.
    """
    for adapter, context in pending:
        try:
            adapter.disconnect(context)
        except Exception:  # noqa: BLE001 -- best-effort revocation, never raises to the caller
            pass
