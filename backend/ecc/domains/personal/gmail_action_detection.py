"""Phase 10 Task 5: proactive `email.detect_action` AI-task-type wiring for
Gmail. Split out of `gmail_adapter.py` (deepening candidate from an
architecture review) -- these three names (`detect_actions_since`,
`_detect_action_for_message`, `_register_message_evidence`) and
`_MAX_ACTION_DETECTIONS_PER_CALL` were the genuinely detection-exclusive
slice of that file; `fetch_and_store_body` and `resolve_or_create_person`
stayed there since both are also used by code that has nothing to do with
AI detection (`gmail_threads.py`'s on-demand thread-open fetch, and the
ordinary sync path's own participant resolution, respectively) --
importing them from a module named for AI detection would be the wrong
dependency direction.

`detect_actions_since` stays a thin, delegating `GmailAdapter` method
(not deleted) rather than becoming a free function callers reach for
directly: `connector_accounts.py`'s own dispatch is duck-typed
(`getattr(adapter, "detect_actions_since", None)`), not a typed Protocol
member, so removing the method would silently stop action detection in
production with no error anywhere. `_detect_action_for_message` has no
caller outside this module (confirmed by a repo-wide grep before this
split), so it carries no such constraint and is a plain function here,
called by `detect_actions_since` with the same `adapter` it was given.
"""

from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.orm import Session

from ecc.auth import AuthContext
from ecc.config import get_settings
from ecc.database import SessionFactory
from ecc.domains.ai_runtime.ollama_client import OllamaAdapter
from ecc.domains.ai_runtime.runtime import execute_run
from ecc.domains.engineering.connectors import ConnectorAccountContext
from ecc.domains.governance.recommendation_models import RecommendationCreate
from ecc.domains.governance.recommendation_mutations import create_recommendation, synthetic_request

# Plain, real (not TYPE_CHECKING-guarded) imports -- no cycle to avoid;
# `gmail_adapter.py` only ever reaches back into this module from inside
# a method body, deferred to call time (see `detect_actions_since`'s own
# comment there). `_bearer_headers`/`_email_consent_active` come from
# `gmail_shared.py`, not `gmail_adapter.py` -- see that module's own
# docstring.
from .gmail_adapter import GmailAdapter, owner_id_for_account, resolve_or_create_person
from .gmail_shared import _bearer_headers, _email_consent_active

# Task 5's own, much smaller bound -- deliberately not `_MAX_MESSAGES_PER_
# CALL`. `connector_accounts.py`'s own module docstring justifies its
# synchronous, non-worker-dispatched `/sync` handler on the premise that
# "a sync call is a single adapter method call" -- true for `backfill`/
# `incremental_sync` themselves (a `messages.get` round trip per message,
# cheap), but `detect_actions_since` adds a *second*, much more expensive
# round trip per candidate on top of that: a live Gmail body fetch *and* a
# live `execute_run` Ollama inference call (`router.py`'s own
# `timeout_seconds=40.0` per model call), run sequentially, still inside
# that same synchronous request handler. Reusing `_MAX_MESSAGES_PER_CALL`
# (200, `gmail_adapter.py`'s own bound) here would let one `/sync` call
# block for up to 200 sequential Gmail-plus-Ollama round trips before the
# HTTP response returns -- realistically minutes, worst-case well past
# most reverse-proxy/gateway timeouts -- found by round 2 review. A
# smaller bound reduces, but does not eliminate, that risk; it is not a
# substitute for moving this hook to a durable background dispatch
# (`ecc.domains.automation.worker`) before `email_action_detection_enabled`
# is ever turned on outside a controlled test, which remains the real fix
# and is out of scope for this task.
_MAX_ACTION_DETECTIONS_PER_CALL = 5


def _register_message_evidence(
    session: Session, *, workspace_id: UUID, node_id: UUID, external_message_id: str, now: datetime
) -> UUID:
    """One fresh `pkos_evidence` row citing the specific message that
    triggered a detection run -- distinct from whatever (possibly much
    older) `pkos_evidence` row `resolve_or_create_person` created the
    first time this sender was ever seen (see that function's own
    docstring): a recommendation's `evidence_ids` must point at the email
    actually reasoned about, not merely at "this sender is a known
    person." `source_ref` is deliberately namespaced (`gmail:detect_
    action:...`, not `resolve_or_create_person`'s own bare `gmail:...`)
    so the two purposes never collide on the same `sha256`.
    """
    evidence_id = uuid4()
    source_ref = f"gmail:detect_action:{external_message_id}"
    session.execute(
        text(
            """
            INSERT INTO pkos_evidence (
                id, workspace_id, node_id, source_type, source_ref, sha256,
                captured_at, evidence_state
            ) VALUES (
                :id, :workspace_id, :node_id, 'gmail_sync', :source_ref, :sha256,
                :now, 'available'
            )
            """
        ),
        {
            "id": evidence_id,
            "workspace_id": workspace_id,
            "node_id": node_id,
            "source_ref": source_ref,
            "sha256": sha256(source_ref.encode()).hexdigest(),
            "now": now,
        },
    )
    return evidence_id


def detect_actions_since(
    adapter: GmailAdapter,
    context: ConnectorAccountContext,
    *,
    since: datetime,
    ollama_adapter: OllamaAdapter | None = None,
) -> None:
    """Called from `connector_accounts.sync_connector_endpoint`'s phase
    3, after a successful `backfill`/`incremental_sync` (see that
    module's own call-site comment). Feature-flagged off by default
    (`config.py:email_action_detection_enabled`) -- Task 1-4's own
    sync/OAuth/create-path machinery is unaffected either way.

    For up to `_MAX_ACTION_DETECTIONS_PER_CALL` inbound messages still
    `body IS NULL` (`gmail.metadata`-only sync never populates it) in
    this account's own threads: fetches the full body (`gmail.
    readonly`, `format=full`), stores it encrypted, registers it as
    `pkos_evidence`, and runs it through the `email.detect_action` AI
    task type. A `has_action: true` result becomes a `source="ai"`
    recommendation via Task 4's create-path (`create_recommendation`)
    -- this function never writes a `tasks`/`commitments`/`risks` row
    directly.

    `body IS NULL`, not `created_at >= since`, is the eligibility
    filter -- `since` only orders which eligible messages this call
    prioritizes (this call's own newly-synced messages first, via
    `ORDER BY (created_at >= since) DESC`), never which ones it
    excludes. Round 2 review's own smaller `_MAX_ACTION_DETECTIONS_
    PER_CALL` bound made a latent bug in the original `created_at >=
    since` *filter* design certain to hit in ordinary use, found by
    round 3 review: any candidate left over after one call (more
    eligible messages existed than the bound allowed) would have its
    `created_at` permanently fall behind every later call's own,
    strictly-later `since` -- silently and permanently excluding it
    from every future call, not merely delaying it, with no error or
    trace anywhere. `body IS NULL` never regresses once a message's
    body is actually fetched, so using it alone as the filter (with
    `since` demoted to an ordering hint) makes a message's eligibility
    durable across calls: a backlog beyond one call's own bound is
    worked down over subsequent calls instead of being lost.

    Best-effort per message: one message's failure (a transient Gmail
    API error, a malformed response, a grounding-check rejection)
    does not prevent the next message in the same batch from being
    tried -- the same "skip only this one, keep going" discipline
    `_process_message`'s own participant-resolution loop already
    establishes for a different per-item failure.

    That "skip and keep going" guarantee is per-*batch*, not per-
    *message* across calls: `_detect_action_for_message`'s own body-
    fetch UPDATE commits (`body IS NULL` no longer matches) before
    evidence registration/`execute_run`/`create_recommendation` run,
    so a failure in any of those *later* steps leaves the message
    permanently ineligible for a future `detect_actions_since` call's
    own `WHERE ... AND m.body IS NULL` selection -- not retried, just
    quietly never proactively scanned again. A deliberate trade-off
    (retrying would mean re-selecting on some signal other than "body
    not yet fetched," which nothing here currently tracks), not an
    oversight -- found and documented by round 1 review.

    `_detect_action_for_message` applies that same "commit the body
    UPDATE, accept no further retry" treatment to one case *before*
    evidence registration too, as of round 4 review's fix: a message
    whose Gmail response parsed successfully (200, valid JSON, a dict
    `payload`) but yielded no `text/plain` part is written with an
    empty-string body rather than left `body IS NULL` -- otherwise it
    would be re-selected, re-fetched, and re-rejected by every future
    call forever (round 4's finding: this was the one path through
    `_detect_action_for_message` that could never make a message
    ineligible, unlike every other early return, which stays `body IS
    NULL` because the failure genuinely may be transient -- a rate-
    limited or non-200 response, an unparseable response body, a
    missing `payload` key). An empty-string body is exactly `email_
    action_tools.py`'s own "genuinely empty" case, not "not yet
    fetched" -- that tool renders it as an empty message rather than
    omitting it.

    Builds its own `AuthContext` from the account's `owner_id`, not a
    caller-supplied one -- the created recommendation must belong to
    the connected account's own owner (`owner_id_for_account`),
    regardless of which user's own request happened to trigger this
    particular sync call.

    `ollama_adapter`: `None` in production (`execute_run`'s own
    default, a real network-hitting `OllamaAdapter()`) -- threaded
    through only so tests can inject a mocked transport, the same
    `OllamaAdapterDep`-style dependency-injection shape `ai_insights.py`'s
    own HTTP endpoint already uses for the identical purpose.
    """
    if not get_settings().email_action_detection_enabled:
        return

    with SessionFactory() as session, session.begin():
        owner_id = owner_id_for_account(session, context.workspace_id, context.connector_account_id)
    if owner_id is None:
        return
    with SessionFactory() as session, session.begin():
        if not _email_consent_active(session, context.workspace_id, owner_id):
            return

    with SessionFactory() as session, session.begin():
        rows = (
            session.execute(
                text(
                    """
                    SELECT m.id, m.thread_id, m.external_message_id, m.sender
                    FROM email_messages m
                    JOIN email_threads t
                      ON t.id = m.thread_id AND t.workspace_id = m.workspace_id
                    WHERE m.workspace_id = :workspace_id AND m.owner_id = :owner_id
                      AND t.connector_account_id = :connector_account_id
                      AND m.direction = 'inbound' AND m.body IS NULL
                    ORDER BY (m.created_at >= :since) DESC, m.sent_at ASC
                    LIMIT :limit
                    """
                ),
                {
                    "workspace_id": context.workspace_id,
                    "owner_id": owner_id,
                    "connector_account_id": context.connector_account_id,
                    "since": since,
                    # `_MAX_ACTION_DETECTIONS_PER_CALL`, not `_MAX_
                    # MESSAGES_PER_CALL` -- see that constant's own
                    # comment. Round 1 review flagged this query's
                    # missing bound as defense-in-depth against
                    # `backfill`/`incremental_sync`'s own budget
                    # someday growing; round 2 review found the
                    # sharper reason this needs its own, much smaller
                    # bound regardless: every row here costs a live
                    # Gmail `messages.get` *and* a live Ollama
                    # inference call, sequentially, inside `/sync`'s
                    # own synchronous request handler.
                    "limit": _MAX_ACTION_DETECTIONS_PER_CALL,
                },
            )
            .mappings()
            .all()
        )

    headers = _bearer_headers(context.credential)
    for row in rows:
        # Re-checked per message, not merely once above -- round 7
        # review found this loop never repeated the same recheck
        # `_sync_messages`/`_sync_history` already do before every one
        # of their own writes, despite `SYNC-CONTRACT.md`'s own general
        # contract ("An active `email` domain consent is checked
        # before external fetch and again before each message write.
        # Revocation during a call stops further writes.") covering
        # this write path too. Consent-revocation disconnect/purge is
        # not yet implemented (Task 7, per this phase's own status
        # doc) -- today, this per-message recheck is the *only*
        # enforcement standing between a user revoking `email` domain
        # consent and this loop continuing to fetch, decrypt-and-store,
        # and run their mail through an AI model regardless. `return`,
        # not `continue`, on revocation -- stops the whole remaining
        # batch, matching `_sync_messages`' identical "halts the call"
        # semantics, not merely this one row.
        with SessionFactory() as session, session.begin():
            if not _email_consent_active(session, context.workspace_id, owner_id):
                return
        try:
            _detect_action_for_message(
                adapter,
                workspace_id=context.workspace_id,
                owner_id=owner_id,
                message_id=row["id"],
                thread_id=row["thread_id"],
                external_message_id=row["external_message_id"],
                sender=row["sender"],
                headers=headers,
                ollama_adapter=ollama_adapter,
            )
        except Exception:  # noqa: BLE001 -- one message's failure never stops the batch
            continue


def _detect_action_for_message(
    adapter: GmailAdapter,
    *,
    workspace_id: UUID,
    owner_id: UUID,
    message_id: UUID,
    thread_id: UUID,
    external_message_id: str,
    sender: str,
    headers: dict[str, str],
    ollama_adapter: OllamaAdapter | None,
) -> None:
    plain_text = adapter.fetch_and_store_body(
        workspace_id=workspace_id,
        message_id=message_id,
        external_message_id=external_message_id,
        headers=headers,
    )
    if not plain_text:
        return
    now = datetime.now(UTC)

    node_id = resolve_or_create_person(
        workspace_id=workspace_id,
        owner_id=owner_id,
        email=sender,
        display_name=sender,
        source_ref=f"gmail:{external_message_id}",
        now=now,
    )
    with SessionFactory() as session, session.begin():
        evidence_id = _register_message_evidence(
            session,
            workspace_id=workspace_id,
            node_id=node_id,
            external_message_id=external_message_id,
            now=now,
        )

    auth = AuthContext(workspace_id=workspace_id, user_id=owner_id, timezone="UTC")
    # Bare session, not wrapped in its own `with session.begin():` --
    # `execute_run`/`create_recommendation` each manage their own
    # transaction boundaries and call `session.commit()` internally
    # (see `execute_run`'s own docstring on why it is "deliberately
    # not wrapped"; `ai_insights.py:generate_insight_endpoint`'s
    # identical three-phase shape is the precedent this mirrors).
    with SessionFactory() as session:
        run = execute_run(
            "email.detect_action",
            "restricted",
            # `message_id` (round 14 review): the specific message this
            # whole call exists to evaluate -- threaded through so
            # `email.get_thread_content`'s own cap can never silently
            # exclude it from an oversized thread (see that tool's own
            # docstring for the mechanism this closes).
            {"thread_id": str(thread_id), "message_id": str(message_id)},
            session=session,
            auth=auth,
            ollama_adapter=ollama_adapter,
        )
        if run.status != "completed" or run.output is None:
            return
        output = run.output
        if not output.get("has_action"):
            return

        # `evidence_ids=[evidence_id]` -- only the message that
        # triggered *this* detection run, not every id in the model's
        # own `output["cited_message_ids"]` (which may also name
        # earlier messages in the same thread, already grounded and
        # already fetched by a prior run of this same function). Those
        # earlier messages each got their own `pkos_evidence` row the
        # call that first fetched *their* body, but this function keeps
        # no message-id -> evidence-id index to look that back up by,
        # and re-registering a fresh evidence row per cited id here
        # would duplicate evidence already on record for the same
        # message. A human confirming this recommendation still sees
        # the full cited thread content in `rationale`/the prompt this
        # ran against; `evidence_ids` itself is a narrower pointer to
        # "the message that made this run happen," not an exhaustive
        # citation index -- a deliberate scope decision, not an
        # oversight, found and documented by round 1 review.
        recommendation_payload = RecommendationCreate(
            recommendation_type="email_action_detected",
            target_type=output["target_type"],
            proposed_action={"operation": "create", "value": None},
            proposed_fields=output["proposed_fields"],
            rationale=output["rationale"],
            confidence=output["confidence"],
            evidence_ids=[evidence_id],
            source="ai",
        )
        create_recommendation(
            session,
            auth,
            recommendation_payload,
            synthetic_request(uuid4(), uuid4()),
            f"email-detect-action:{external_message_id}",
        )
