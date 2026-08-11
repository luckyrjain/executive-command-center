"""On-demand Gmail thread reading and per-thread "forget this" (Phase 10
Gmail Connector Task 6, `docs/superpowers/plans/2026-08-04-phase-10-
gmail-connector.md`, Task 6).

`GET /api/v1/personal/gmail/threads/{thread_id}`: fetches any still-`body
IS NULL` message in the caller's own thread via the exact same
fetch-and-store mechanism Task 5's proactive sync-time detection uses
(`GmailAdapter.fetch_and_store_body`, extracted from `_detect_action_for_
message` specifically so this task reuses it rather than duplicating it),
then returns the thread's full decrypted content via `email_action_tools.
get_thread_content_tool` -- the exact same tool `email.detect_action`'s
own AI runtime call uses to read a thread, reused here for the
human-facing HTTP surface for the identical reason: one rendering of "what
does this thread contain," not two. No `trigger_message_id` is passed
(that field exists solely for `email.detect_action`'s own "the message
that triggered this run must survive the cap" guarantee -- an explicit
human thread-open has no analogous single triggering message), so this
call gets the tool's own plain most-recent-`MAX_THREAD_MESSAGES` behavior.
A disconnected connector account skips the live-fetch attempt entirely
(Loop 2 round 3 review finding: `sync_connector_endpoint`'s own `409
CONNECTOR_DISCONNECTED` gate in `connector_accounts.py` has no
equivalent here otherwise) rather than decrypting and using a credential
Google has likely already revoked -- but, unlike that endpoint, does not
fail the whole request over it, since a disconnected connector should not
block reading a thread's already-cached content.

`POST /api/v1/personal/gmail/threads/{thread_id}/forget`: the plan's own
"forget this" scope is narrower than Phase 7's whole-domain delete
(`export_deletion.py`) -- it "deletes the cached body/message content for
that thread only, not the whole `email` domain." Nulls `snippet`/`body`/
`body_fetched_at` for every message in the targeted thread, back to
exactly the state Task 2's own metadata-only sync leaves a never-opened
message in -- deliberately does NOT delete the `email_messages` rows
themselves (unlike `export_deletion.py`'s per-table `DELETE`s), preserving
`external_message_id`'s own dedup/idempotency role for a future
incremental sync and the thread's own chronology; a "forgotten" thread can
still be re-opened later, which re-fetches its content exactly like a
never-opened thread would. Also does NOT delete the `email_threads` row --
`export_deletion.py`'s own domain-delete precedent (that module's own
docstring) keeps the *existence* record (there, `personal_domains`; here,
`email_threads`) while removing the *content* underneath it, for the
identical reason: a `deletion_jobs` audit row and any future re-open both
need something to point at. Records the action in `deletion_jobs` with
`scope='thread'`, `resource_id=<thread_id>` (migration `0075`, this
task's own schema addition), mirroring `export_deletion.py`'s own
audit-trail shape at the narrower granularity.

`GET` checks `_email_consent_active` (imported from `gmail_adapter.py`,
the identical check Task 5's own per-message recheck uses), not `require_
enabled_domain` -- consistent with Task 5's own precedent (that function
never separately checks `personal_domains.enabled` either), since a live
Gmail fetch is exactly what `domain_consents` governs; `personal_domains.
enabled` is a separate "is this data domain turned on at all" concern this
task does not newly enforce.

`POST .../forget` deliberately does NOT check `_email_consent_active`:
unlike `GET`, it makes no live Gmail call, and a deletion/forget action
should stay available even without an active consent grant (e.g. right
after revoking consent, when there is no active grant to check) --
gating a deletion capability behind the very consent state a user might
be in the middle of revoking would be backwards. Both endpoints stay
scoped to the caller's own `workspace_id`/`owner_id` regardless.

`GET /api/v1/personal/gmail/threads` (Task 8): lists the caller's own
threads, newest-`last_message_at`-first, capped by an optional `limit`
(default 50, max 200) -- no offset/cursor, matching `connector_accounts.
py`'s own list endpoints (`list_sync_runs_endpoint`, `list_repositories_
endpoint`), none of which paginate at this activation's expected data
volume. Gated on `_require_email_consent`, the identical check `GET .../
{thread_id}` uses, even though this endpoint makes no live Gmail call
itself -- it still returns subject lines and sender addresses, the same
sensitivity class as thread content, and gating it identically lets the
frontend reuse one `EMAIL_CONSENT_NOT_ACTIVE` error-handling path for
both. Each row's `last_sender`/`last_direction`/`message_count`/
`body_cached` are computed from a single `LEFT JOIN LATERAL` per thread
rather than three separate correlated subqueries -- the window functions
(`COUNT(*) OVER ()`, `BOOL_OR(...) OVER ()`) evaluate over the lateral
subquery's own full `WHERE`-filtered row set before its `ORDER BY ...
LIMIT 1` trims it down to the single most-recent message, so one row
carries the most-recent message's own `sender`/`direction` alongside
aggregates computed over every message in the thread.
"""

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text
from sqlalchemy.orm import Session

from ecc.auth import AuthContext, AuthDep, CsrfDep
from ecc.database import get_session
from ecc.domains.ai_runtime.tools import ToolNotFound
from ecc.domains.engineering.connector_accounts import _get_encrypted_credential
from ecc.domains.engineering.crypto import decrypt_credential

from .domains import (
    IdempotencyHeader,
    load_cached,
    lock_idempotency,
    request_hash,
    store_idempotency,
)
from .email_action_tools import MAX_THREAD_MESSAGES, get_thread_content_tool
from .gmail_adapter import GmailAdapter, _bearer_headers, _email_consent_active

router = APIRouter(prefix="/api/v1/personal/gmail", tags=["personal"])
SessionDep = Annotated[Session, Depends(get_session)]

# Module-level singleton, mirroring `gmail_oauth.py`'s own identical
# "constructed with no arguments, reads `ECC_GMAIL_OAUTH_*` lazily" shape.
_adapter = GmailAdapter()


class _EmptyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ThreadMessageResponse(BaseModel):
    id: str
    sender: str
    sent_at: str
    direction: str
    body: str


class ThreadContentResponse(BaseModel):
    subject: str | None
    messages: list[ThreadMessageResponse]


class ThreadForgetResponse(BaseModel):
    id: UUID
    thread_id: UUID
    status: str
    requested_at: datetime
    completed_at: datetime | None


class ThreadSummaryResponse(BaseModel):
    id: UUID
    subject: str | None
    last_message_at: datetime
    last_sender: str | None
    last_direction: str | None
    message_count: int
    body_cached: bool


class ThreadListResponse(BaseModel):
    threads: list[ThreadSummaryResponse]


def _require_email_consent(session: Session, auth: AuthContext) -> None:
    if not _email_consent_active(session, auth.workspace_id, auth.user_id):
        raise HTTPException(status_code=403, detail="EMAIL_CONSENT_NOT_ACTIVE")


def _thread_connector_account(
    session: Session, auth: AuthContext, thread_id: UUID
) -> tuple[UUID, str] | None:
    row = session.execute(
        text(
            "SELECT ca.id, ca.status FROM email_threads et "
            "JOIN connector_accounts ca ON ca.workspace_id = et.workspace_id "
            "AND ca.id = et.connector_account_id "
            "WHERE et.workspace_id = :workspace_id AND et.owner_id = :owner_id "
            "AND et.id = :id"
        ),
        {"workspace_id": auth.workspace_id, "owner_id": auth.user_id, "id": thread_id},
    ).one_or_none()
    return (row[0], row[1]) if row is not None else None


@router.get("/threads", response_model=ThreadListResponse)
def list_threads_endpoint(
    auth: AuthDep,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> ThreadListResponse:
    _require_email_consent(session, auth)
    rows = (
        session.execute(
            text(
                """
                SELECT et.id, et.subject, et.last_message_at,
                       lm.sender AS last_sender, lm.direction AS last_direction,
                       COALESCE(lm.message_count, 0) AS message_count,
                       COALESCE(lm.body_cached, false) AS body_cached
                FROM email_threads et
                LEFT JOIN LATERAL (
                    SELECT em.sender, em.direction,
                           COUNT(*) OVER () AS message_count,
                           BOOL_OR(em.body IS NOT NULL) OVER () AS body_cached
                    FROM email_messages em
                    WHERE em.workspace_id = et.workspace_id AND em.thread_id = et.id
                    ORDER BY em.sent_at DESC
                    LIMIT 1
                ) lm ON true
                WHERE et.workspace_id = :workspace_id AND et.owner_id = :owner_id
                ORDER BY et.last_message_at DESC
                LIMIT :limit
                """
            ),
            {"workspace_id": auth.workspace_id, "owner_id": auth.user_id, "limit": limit},
        )
        .mappings()
        .all()
    )
    return ThreadListResponse(
        threads=[ThreadSummaryResponse.model_validate(dict(row)) for row in rows]
    )


@router.get("/threads/{thread_id}", response_model=ThreadContentResponse)
def get_thread_endpoint(
    thread_id: UUID, auth: AuthDep, session: SessionDep
) -> ThreadContentResponse:
    account = _thread_connector_account(session, auth, thread_id)
    if account is None:
        raise HTTPException(status_code=404, detail="THREAD_NOT_FOUND")
    connector_account_id, connector_status = account
    _require_email_consent(session, auth)

    # Every message in this thread that Task 5's own proactive path, or an
    # earlier open of this same thread, has not already fetched -- fetched
    # here, on this explicit open, rather than waiting for a future sync
    # call to happen to select it (Task 6's own "on explicit user open"
    # scope, distinct from Task 5's own account-wide proactive scan).
    # Bounded at `MAX_THREAD_MESSAGES` (newest first): `get_thread_content_
    # tool`'s own response below never returns more than that many messages
    # regardless, so fetching more would be wasted live Gmail calls -- and,
    # unbounded, a thread an external sender can grow arbitrarily long
    # (e.g. a reply-all/notification thread) would otherwise make one `GET`
    # trigger one sequential live Gmail round trip per unfetched message,
    # the same request-blocking failure mode `_MAX_ACTION_DETECTIONS_PER_
    # CALL` in `gmail_adapter.py` already exists to prevent for the
    # proactive path.
    unfetched = (
        session.execute(
            text(
                "SELECT id, external_message_id FROM email_messages "
                "WHERE workspace_id = :workspace_id AND owner_id = :owner_id "
                "AND thread_id = :thread_id AND body IS NULL "
                "ORDER BY sent_at DESC LIMIT :limit"
            ),
            {
                "workspace_id": auth.workspace_id,
                "owner_id": auth.user_id,
                "thread_id": thread_id,
                "limit": MAX_THREAD_MESSAGES,
            },
        )
        .mappings()
        .all()
    )
    # `connector_status != "disconnected"`: matches `sync_connector_endpoint`'s
    # own `409 CONNECTOR_DISCONNECTED` gate (`connector_accounts.py`) in
    # spirit, not status code -- a disconnected connector's credential is
    # best-effort-revoked at Google already and should not be decrypted and
    # used for a fresh live call here. Unlike that endpoint, this one does
    # not fail the whole request over it: skipping the live-fetch attempt
    # still lets already-cached content render below, which a disconnected
    # connector should not block reading.
    if unfetched and connector_status != "disconnected":
        encrypted = _get_encrypted_credential(session, auth.workspace_id, connector_account_id)
        credential = decrypt_credential(encrypted)
        headers = _bearer_headers(credential)
        for row in unfetched:
            # Best-effort per message, matching `detect_actions_since`'s
            # own "one message's failure never stops the rest" discipline
            # -- a message this call could not fetch (a transient Gmail
            # error) simply stays `body IS NULL`, omitted from the
            # response below rather than failing this whole request. Without
            # this `try/except`, `fetch_and_store_body`'s own `httpx.
            # HTTPError` -> `RuntimeError` re-raise on a genuine transient
            # failure (timeout, connection reset) would propagate out of
            # this loop and turn one message's hiccup into a 500 for the
            # whole thread -- including content already fetched, in this
            # call or an earlier one, for every other message in it.
            try:
                _adapter.fetch_and_store_body(
                    workspace_id=auth.workspace_id,
                    message_id=row["id"],
                    external_message_id=row["external_message_id"],
                    headers=headers,
                )
            except Exception:  # noqa: BLE001 -- one message's failure never stops the batch
                continue

    result = get_thread_content_tool(session, auth, thread_id)
    if isinstance(result, ToolNotFound):
        # Reached only via a genuine TOCTOU race (the thread was deleted or
        # transferred between the lookup above and this call) -- both
        # checks are scoped identically (`workspace_id`/`owner_id`/`id`),
        # so this is not a normal path, just defense in depth.
        raise HTTPException(status_code=404, detail="THREAD_NOT_FOUND")
    return ThreadContentResponse.model_validate(result.output)


@router.post("/threads/{thread_id}/forget", response_model=ThreadForgetResponse)
def forget_thread_endpoint(
    thread_id: UUID,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
    idempotency_key: IdempotencyHeader,
) -> ThreadForgetResponse:
    """Idempotency-key handling matches `export_deletion.py:delete_domain_
    endpoint` exactly (same rationale: the underlying `UPDATE`s are
    individually idempotent, but the `deletion_jobs` audit `INSERT` is
    not -- a retry without this guard would insert a second `'completed'`
    row for the same thread and moment).
    """
    req_hash = request_hash(_EmptyBody(), f"forget_thread:{thread_id}")
    now = datetime.now(UTC)
    with session.begin():
        lock_idempotency(session, auth, idempotency_key)
        cached = load_cached(session, auth, idempotency_key, req_hash, domain="gmail_thread_forget")
        if cached is not None:
            return ThreadForgetResponse.model_validate(cached)

        thread_row = session.execute(
            text(
                "SELECT id FROM email_threads WHERE workspace_id = :workspace_id "
                "AND owner_id = :owner_id AND id = :id"
            ),
            {"workspace_id": auth.workspace_id, "owner_id": auth.user_id, "id": thread_id},
        ).one_or_none()
        if thread_row is None:
            raise HTTPException(status_code=404, detail="THREAD_NOT_FOUND")

        session.execute(
            text(
                "UPDATE email_messages SET snippet = NULL, body = NULL, body_fetched_at = NULL, "
                "updated_at = :now WHERE workspace_id = :workspace_id AND owner_id = :owner_id "
                "AND thread_id = :thread_id"
            ),
            {
                "now": now,
                "workspace_id": auth.workspace_id,
                "owner_id": auth.user_id,
                "thread_id": thread_id,
            },
        )

        job_id = uuid4()
        session.execute(
            text(
                """
                INSERT INTO deletion_jobs (
                    id, workspace_id, owner_id, domain_key, scope, resource_id, status,
                    requested_at, completed_at, created_by
                ) VALUES (
                    :id, :workspace_id, :owner_id, 'email', 'thread', :resource_id, 'completed',
                    :now, :now, :actor_id
                )
                """
            ),
            {
                "id": job_id,
                "workspace_id": auth.workspace_id,
                "owner_id": auth.user_id,
                "resource_id": thread_id,
                "now": now,
                "actor_id": auth.user_id,
            },
        )
        response = ThreadForgetResponse(
            id=job_id, thread_id=thread_id, status="completed", requested_at=now, completed_at=now
        )
        store_idempotency(
            session, auth, idempotency_key, req_hash, response.model_dump(mode="json"), now
        )
        return response
