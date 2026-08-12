"""Phase 10 Gmail Connector Task 2: backfill, incremental sync and entity
linking (`docs/superpowers/plans/2026-08-04-phase-10-gmail-connector.md`
Task 2, `backend/ecc/domains/personal/gmail_adapter.py`).

Covers, per this task's own scope:

1. `backfill` syncs messages within the requested window (`since` bounds
   the Gmail `q=after:...` search), writing `email_threads`/`email_
   messages` and resolving sender/recipients into `pkos_nodes` via
   `entity_aliases` (a person entity is created once, then reused on a
   second message from the same participant).
2. Re-invoking `backfill` re-verifies `domain_consents` is still active,
   both before starting and mid-call -- a consent revoked between two
   messages in the same call halts the call (`status="partial"`), and a
   consent that was never active at all raises outright.
3. `incremental_sync` resumes from a `historyId` cursor via Gmail's
   `history.list`, defers to a fresh `backfill` when `cursor is None`
   (the Protocol's own contract) and when Gmail reports the cursor has
   expired (`history.list` returning 404).
4. Rate-limit handling (a Gmail `429`, and a `403` carrying a
   `rateLimitExceeded`-family `reason`) degrades to `status="partial"`
   with the sync's own accumulated progress preserved, distinguishing
   that from an ordinary `403` (a real, non-transient rejection this
   adapter must not silently retry-then-swallow).
5. Idempotent re-sync: re-running `backfill` over an overlapping window
   does not duplicate `email_threads`/`email_messages` rows (`ON CONFLICT
   DO NOTHING`/`DO UPDATE`), and does not re-run entity resolution for an
   already-seen message's participants.
6. `ResourceType` widened to accept `"message"`; every other resource
   type still zero-item-succeeds for a `gmail` account, matching every
   other adapter's "not-yet-implemented resource type" contract.
7. An oversized `From`/`To` address (`_MAX_EMAIL_ADDRESS_LENGTH`) is
   dropped like any other unparseable header, not left to crash the sync
   call on `email_messages.sender`/`.recipients`' own `VARCHAR(320)`
   bound -- found by round 1 review's security/correctness lens.
8. `_MAX_MESSAGES_PER_CALL` hit mid-page (not merely mid-multi-page-walk)
   correctly reports `status="partial"`, not a false `"succeeded"` that
   would silently strand the page's own unfetched messages -- found by
   round 1 review's architecture lens; both `_sync_messages`/`_sync_
   history`'s structurally identical loops are covered.
9. `messages.list`/`history.list` pagination genuinely walks a real
   multi-page response (every test above 8 uses a single page) -- round 1
   review coverage gap, closed.
10. `_process_message`'s own guard clauses (missing `id`/`threadId`/
    `From` header/recipients, non-numeric `internalDate`) each skip that
    one message gracefully rather than raising, called directly rather
    than only exercised incidentally -- round 1 review coverage gap,
    closed.
11. An unpaired UTF-16 surrogate (valid JSON, but not `.encode("utf-8")`-
    able -- distinct from an embedded NUL byte) in a `From` display name,
    a `Subject` header, or Gmail's own `id`/`threadId` fields is dropped/
    skips the message the same way an oversized address or an embedded
    NUL already does, not left to crash the sync call with an uncaught
    `UnicodeEncodeError` -- found by round 2 review's security/
    correctness lens.
12. Gmail's own `id`/`threadId` fields are length/NUL/surrogate-validated
    before reaching `email_threads.external_thread_id`/`email_messages.
    external_message_id`'s `VARCHAR(255)` bound, the same way `sender`/
    `recipients` already are -- found by round 2 review (previously
    entirely unguarded).
13. A `To` header whose display name legitimately contains a comma
    (`'"Doe, John" <john@example.com>'`) is parsed as one recipient via
    `email.utils.getaddresses`, not split into two bogus fragments by a
    naive `to_header.split(",")` -- the naive split silently lost the
    real address and fabricated a non-email "recipient" from the
    fragment, corrupting the entity graph -- found by round 2 review.
14. An extreme recipient count in one message's `To` header is capped to
    `_MAX_RECIPIENTS_PER_MESSAGE` rather than making a single message do
    an unbounded amount of synchronous per-recipient entity-resolution DB
    work -- a resource-exhaustion angle found by round 4 review, distinct
    from every prior malformed-input crash/corruption finding.
15. A `To` header ending in a bare trailing comma
    (`"alice@x.test, bob@x.test,"`) recovers both real recipients instead
    of the whole message silently vanishing -- `_getaddresses_resilient`,
    round 3 review's own fix, this item added by round 4 review since it
    had never had a "Covers" entry of its own despite always having its
    own dedicated tests (below); paired with a companion test proving the
    genuinely ambiguous no-comma shape `getaddresses(strict=True)` exists
    to reject is still correctly dropped, not weakened by this recovery.
16. The same ambiguous no-comma shape item 15's companion test covers
    (`'bob@example.test<carol@example.test>'`) is *not* still safely
    dropped once a trailing comma is appended to it
    (`'bob@example.test<carol@example.test>,'`) -- `getaddresses`' own
    aggregate `1 + comma_count` address-count check (see
    `_getaddresses_resilient`'s own docstring) cannot tell that shape
    apart from a genuine two-recipient list, so it silently accepts both
    fabricated addresses as real recipients, resolving each into its own
    `pkos_nodes` person entity -- found by round 4 review, the same
    "attacker-controlled `To` header corrupts the entity graph" impact
    item 13 already closed once, reached here via a different mechanism
    (a comma-count collision, not a naive split). Closed with
    `_to_header_has_grammar_defect` (an `email.headerregistry`-based
    cross-check `getaddresses`' own count heuristic cannot provide from
    within itself), applied ahead of `_getaddresses_resilient` so a
    defect anywhere in the header drops the whole `To` header rather than
    only the colliding field.
17. A `To` header with a benign interior double comma
    (`"bob@example.test,,carol@example.test"`) recovers both real
    recipients rather than the whole message silently vanishing --
    `_to_header_has_grammar_defect`'s own first cut (item 16) gated the
    recipient loop on *any* `HeaderRegistry` defect, not specifically the
    `InvalidHeaderDefect` the round 4 collision actually raises, so a
    harmless `ObsoleteHeaderDefect`-only shape (an interior double comma,
    a comma-plus-whitespace empty entry) was wrongly treated the same as
    a genuine collision -- found by round 5 review, a false-positive
    regression in round 4's own fix. Narrowed to `InvalidHeaderDefect`
    specifically; item 16's collision case is unaffected (still raises
    `InvalidHeaderDefect`, independently confirmed).
18. An embedded NUL byte (`\\x00`, from a JSON `\\u0000` escape in Gmail's
    own response body) in a `From` address, a `From` display name, or a
    `Subject` header is dropped/skips the message the same way an
    oversized address already does, not left to crash the sync call with
    an uncaught `psycopg.DataError` -- found by round 1 review's security/
    correctness lens, the same review pass that added item 7's oversized-
    address coverage, this item added by round 5 review since it had never
    had a "Covers" entry of its own despite always having its own three
    dedicated tests (`test_nul_byte_in_from_address_is_skipped_not_a_crash`,
    `test_nul_byte_in_from_display_name_is_skipped_not_a_crash`,
    `test_nul_byte_in_subject_is_dropped_but_message_still_syncs`) since
    round 1.
19. A `From`/`To` header built of deeply nested RFC 5322 `(...)` comments
    around an otherwise ordinary address is dropped/skips the message the
    same way an oversized address already does, not left to crash the sync
    call with an uncaught `RecursionError` -- `parseaddr`/`getaddresses`/
    `HeaderRegistry` all share the same recursive RFC 5322 grammar with no
    depth cap of its own -- found by round 6 review, the same "single
    crafted message reaches an uncaught crash and permanently wedges
    `incremental_sync`" failure class every guard above closes for its own
    failure mode, reached here via stdlib recursion depth instead.
20. `_to_header_has_grammar_defect`'s own narrow `except` (round 4's fix,
    catching `UnicodeEncodeError` among others) had never actually been
    exercised by a surrogate-carrying `To` header -- every existing
    surrogate test puts the surrogate in `From`/`Subject`/`id`/`threadId`
    instead, none of which reach this function at all. Not a live bug (the
    guard has been correct since round 4), a coverage gap only -- found by
    round 6 review's architecture/quality lens, mutation-confirmed by
    narrowing the guard's `except` tuple down to `RecursionError` alone
    and observing this was the only test in the file that then failed.
21. A `To` header built of two adjacent RFC 5322 `group` constructs (or a
    group immediately followed by a bare address) with no comma between
    them -- invalid grammar `HeaderRegistry`'s own legacy fallback still
    tries to recover from -- previously reached `_to_header_has_grammar_
    defect` uncaught as an `AttributeError: 'Group' object has no
    attribute 'local_part'`, a distinct crash from every prior stdlib
    hazard this file has closed on this same call (not `RecursionError`,
    not `UnicodeEncodeError`, not any of the other five caught types) --
    found by round 7 review, dropped like any other unparseable `To`
    header rather than crashing the whole sync call.
22. A `To` header long enough (`_MAX_TO_HEADER_LENGTH`, 50,000 characters)
    to make `_to_header_has_grammar_defect`'s own `HeaderRegistry`
    cross-check cost multiple seconds of pure CPU per message (independently
    profiled: roughly quadratic in the number of comma-separated fields,
    reaching ~10s at 16,000 addresses) is rejected by one cheap `len(...)`
    check before either expensive parser ever runs on it -- a resource-
    exhaustion angle distinct from `_MAX_RECIPIENTS_PER_MESSAGE`'s own
    downstream-DB-cost guard (which only ever runs *after* this parse would
    already have paid its cost), found by round 7 review.
23. A failure resolving one participant into `pkos_nodes`
    (`_resolve_or_create_person` raising -- realistically a transient DB
    error, not malformed input; unlike every prior finding in this file,
    reachable with no crafted header at all) previously propagated
    uncaught out of `_process_message`, aborting the entire sync call --
    stranding every message still queued behind it in the same call, and
    for `incremental_sync`, wedging that connector account permanently
    since the cursor never advanced past it. Worse: because `_insert_
    message_if_new`'s `ON CONFLICT DO NOTHING` had already committed the
    message row in its own, separate transaction before the failure, a
    later retry found the message already existed and silently skipped
    entity resolution for it entirely -- permanent, silent data loss in
    the entity graph, with the sync call itself reporting `"succeeded"`.
    Found by round 9 review; caught per-participant so one failure is
    isolated to that one message-participant pair (the person entity
    itself is not permanently lost -- a later message naming the same
    participant still resolves them normally) rather than cascading into
    every other message and account still queued in the same call.
24. `_upsert_thread`'s `ON CONFLICT DO UPDATE` clause's own SQL --
    `GREATEST(...)` for `last_message_at`, `COALESCE(...)` for `subject`
    -- is exercised (structurally) by every multi-message-per-thread test
    above, but none of them ever assert on the *values* it produces:
    every one of them happens to process its messages in ascending
    chronological order, so a naive unconditional `EXCLUDED.*` overwrite
    would have produced the identical result and gone unnoticed.
    Mutation-confirmed (replacing both clauses with a plain `EXCLUDED.*`
    overwrite left the combined suite's then-161 tests -- across this
    file, `test_gmail_connector_postgres.py`, and `test_engineering_
    connectors_postgres.py` -- passing). Gmail's own
    `messages.list` returns messages newest-first, so this task's own
    sync loop processes a thread's newest message *before* its older
    ones -- without `GREATEST`, `last_message_at` would regress backward
    once an older same-thread message is processed second; without
    `COALESCE`, `subject` would flip to whichever message happens to be
    processed last instead of staying pinned to the thread's original
    subject -- found by round 11 review's architecture/quality lens.
25. `_sync_messages`' own rate-limited/consent-revoked `partial` returns
    (the `list_response is None`/`get_response is None`/mid-loop
    consent-check branches) previously handed `incremental_sync` a
    `next_cursor` computed as `max()` over every `historyId` already
    processed *this call* -- safe only if Gmail's `messages.list` returns
    results in strictly ascending `historyId` order, which it does not
    (its default, and this task's own, sort is newest-received-first, and
    `historyId` does not track receipt order closely enough to guarantee
    monotonicity either). A message still queued behind the one that
    triggered the partial return, but with a *lower* `historyId` than the
    cursor just handed out, is silently, permanently unreachable by any
    subsequent `incremental_sync` call: `history.list(startHistoryId=...)`
    only ever returns entries with a strictly greater `historyId` than the
    cursor -- found by round 11 review's security/correctness lens, a
    cursor-value-correctness bug distinct from every prior finding in this
    file (none of which touched whether a *given* cursor value was itself
    safe, only whether a `partial` result should hand one out at all).
    Fixed by returning `next_cursor=None` from all three sites, matching
    the already-correct `budget_exhausted` partial return in this same
    method and (at the time) `_sync_history`'s own sibling partial
    branches, which returned `next_cursor=start_history_id`
    unconditionally -- since revised by round 12 review (item 26 below) to
    resume from `resumable_history_id` once available instead, safe there
    only because `history.list`'s own strictly-ascending record order
    gives it a resume point this method's unordered `messages.list` has no
    equivalent for -- found stale by round 13 review, this paragraph
    previously still described `_sync_history`'s pre-round-12 behavior as
    current.
26. `_sync_history`'s own `partial`-status returns (budget-exhausted mid-
    page, rate-limited, consent-revoked mid-loop) previously reported
    `next_cursor=start_history_id` unconditionally, regardless of how many
    `history[]` records this call had already fully processed before
    returning. Since `history.list(startHistoryId=X)` deterministically
    returns the identical records in the identical order for the identical
    `X`, a subsequent `incremental_sync` call resuming from that same,
    never-advanced cursor re-walked from page 1 and re-hit the identical
    budget boundary on the identical records every time -- a livelock, not
    merely delayed progress: any message past position `_MAX_MESSAGES_
    PER_CALL` in that cursor's own ascending-`historyId` ordering was
    silently, permanently unreachable no matter how many times
    `incremental_sync` was retried -- found by round 12 review's security/
    correctness lens, the very question round 11's own fix (item 25 above)
    opened: whether *other* cursor computations in this file shared its
    ordering-assumption flaw. `_sync_messages`' own equivalent gap (a fresh
    `backfill` restarting at `messages.list`'s own newest-first page 1 on
    every retry, permanently unable to reach the tail of a backfill window
    larger than `_MAX_MESSAGES_PER_CALL`) was investigated the same round
    and found to be real but not safely fixable the same way: `messages.
    list` carries no ordering guarantee `next_cursor` could safely resume
    from mid-walk (the exact fact round 11's own fix already established),
    so closing it fully needs a resume-state channel wider than this
    module's single historyId-shaped `next_cursor` string -- disclosed as a
    follow-up, not fixed this round. `_sync_history` itself has the
    ordering guarantee `_sync_messages` lacks (`history.list` walks
    strictly ascending by record `id`), so it does have a fix within the
    existing cursor contract: `resumable_history_id` tracks the last fully-
    processed record's own `id` and every partial-return site resumes from
    it instead of `start_history_id`. Reproduced directly against real
    Postgres before the fix: four history records behind one cursor,
    budget monkeypatched to 2 -- five consecutive `incremental_sync` calls
    each processed only the same first two records and reported the same
    cursor back every time; the remaining two were never written by any of
    them.
27. `resumable_history_id` (item 26) only resumes `_sync_history` at
    `history[]` *record* granularity -- a single record whose own
    `messagesAdded` list by itself exceeds `_MAX_MESSAGES_PER_CALL` could
    still never finish, no matter how many retries, since `history.
    list(startHistoryId=X)` has no way to return that one record
    partially, only ever whole again for the identical `X` -- found by
    round 13 review's security/correctness lens, the newest,
    least-reviewed logic in the file (round 12's own restructuring) not
    yet scrutinized past its own author. Reproduced directly against real
    Postgres: one record with five `messagesAdded` entries, budget
    monkeypatched to 2 -- six consecutive `incremental_sync` calls each
    processed only the same first two messages and reported the same
    `next_cursor` every time; the remaining three were never written by
    any of them. Closed by extending `next_cursor` to optionally carry
    "and skip the first N messages of record M" as a compound string
    (`_parse_history_cursor`/`_build_history_cursor`), safe for the same
    reason `resumable_history_id` itself is: `history.list` walks
    strictly ascending by record `id` for a fixed `startHistoryId`, and a
    `messagesAdded` list is itself a fixed, already-happened fact, so both
    a record's own position and its own message list are stable and
    replayable across retries. Layers on top of, not instead of,
    `resumable_history_id` -- a bare historyId cursor (a fully-caught-up
    record boundary, or a cursor from before this fix) still parses
    correctly as the plain, no-skip case.
28. `_parse_history_cursor` (item 27) had never had a direct unit test of
    its own -- found by round 14 review's architecture/quality lens, the
    same "newest, least-reviewed logic" pattern item 27 itself names, this
    time a coverage gap rather than a livelock. Closed with seven direct
    tests (no HTTP mocking or DB fixture, mirroring this file's own
    `_process_message`-guard-clause tests below) covering the plain-cursor
    case, a well-formed compound cursor, and every documented malformed-
    state fallback. Writing them surfaced a real, if narrow, discrepancy
    between `_parse_history_cursor`'s own docstring (malformed state --
    "the wrong number of `:`-separated fields, or a non-integer/negative
    record id or skip count" -- degrades to the plain-cursor fallback) and
    its actual guard, which checked `skip_count < 0` but not the symmetric
    `record_id < 0`: `_parse_history_cursor("100:-5:3")` returned `("100",
    -5, 3)` as a live compound parse rather than degrading. Harmless in
    practice (a real Gmail `history[]` record id is never negative, so a
    negative `pending_stuck_record_id` could only come from already-
    corrupted persisted state, and would then simply never match any real
    record `_sync_history` walks -- equivalent in effect, though not in the
    `list_start_history_id` value handed to `history.list`, to the
    documented fallback), but a genuine contract violation, not merely a
    hypothetical one. Fixed by adding the missing `record_id < 0` check.
    Mutation-confirmed: reverting just that one condition reproduces
    `("100", -5, 3)` and fails the new negative-record-id test for exactly
    that reason; the other six new tests are unaffected by the same
    revert, confirming they document already-correct behavior rather than
    padding the count.
29. `_sync_messages`'s/`_sync_history`'s "fully caught up" success returns
    each computed `next_cursor` via a bare truthiness check on an `int |
    None` (`if highest_history_id else None` / `if latest_history_id else
    list_start_history_id`) rather than `is not None` -- found by round 16
    review. `_coerce_int` (what both variables are ultimately assigned
    from -- a message's own `historyId` field, the `users.getProfile`
    fallback, or `history.list`'s own page-level `historyId` field) can
    return `0`, which is falsy in Python; a `historyId` of exactly `0` was
    silently treated identically to "no historyId was ever observed at
    all," discarding a real cursor value (`_sync_messages`) or falling back
    to the stale starting cursor instead of the fresh value Gmail actually
    returned (`_sync_history`) on an otherwise genuinely `"succeeded"`,
    fully-caught-up sync. Real Gmail `historyId` values are not documented
    as ever being exactly `0` in practice -- the same "confirmed harmless
    today, but a real, mutation-testable contract violation" framing item
    28 above used for `_parse_history_cursor`'s own `record_id < 0` gap --
    but nothing on the read side validates that assumption, and every
    sibling check on these same two variables elsewhere in the same
    methods already uses `is None`/`is not None` correctly. Fixed by
    switching both sites to `is not None`. Closed with
    `test_backfill_reports_next_cursor_zero_not_none_when_history_id_is_zero`
    and
    `test_incremental_sync_reports_next_cursor_zero_not_the_stale_start_when_history_id_is_zero`,
    each mutation-confirmed against the corresponding site reverted to a
    bare truthiness check.
30. `_upsert_thread`'s `ON CONFLICT DO UPDATE` bumped `email_threads.
    updated_at` unconditionally, even on the exact no-op resync item 5's
    own `test_backfill_rerun_over_an_overlapping_window_does_not_
    duplicate_rows` already exercises -- that test only ever asserted row
    *counts*, never `updated_at` itself, so it passed both before and
    after this bug existed and never caught it. `attention.py`'s Phase 10
    Task 3 code (a separate task, reviewed under its own "Loop 2" round
    numbering, not this file's rounds 1-29 above) derives `attention_
    items.source_entity_version` for `email_thread` rows directly from
    this column (`email_threads` has no real optimistic-concurrency
    `version` column of its own), so the unconditional bump silently
    un-dismissed a dismissed `email_thread` attention item on the next
    `regenerate_attention` call after an ordinary backfill re-run or
    incremental/backfill overlap touched the thread with zero new
    content -- found by that task's own Loop 2 round 6 review, fixed
    here since the bug lives in this file's own `gmail_adapter.py`, not
    `attention.py`. Closed with `test_backfill_rerun_over_an_overlapping_
    window_does_not_bump_thread_updated_at`, asserting `updated_at` is
    byte-for-byte unchanged across an identical re-sync of the same
    single message.
31. A naive (no `tzinfo`) `since` is treated as UTC, not local system
    time. `datetime.timestamp()` on a naive value is Python's own
    local-time interpretation -- Phase 10 Task 8's own Loop 2 round 8
    review found `SyncRequest.since` has no `tzinfo` requirement, so a
    direct API caller bypassing `GmailPanel`'s own always-UTC
    `toISOString()` construction could reach `backfill` with a naive
    value and get a wrong day-range window whenever the server's local
    `TZ` isn't UTC -- a bug round 17 had explicitly closed as
    unreachable before Task 8 wired a real HTTP caller to this
    parameter. Closed with `test_backfill_treats_a_naive_since_as_utc_
    not_local_time`.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from json import dumps as json_dumps
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from identity_fixtures import create_identity
from sqlalchemy import text

from ecc.config import get_settings
from ecc.database import engine
from ecc.domains.engineering.connectors import ConnectorAccountContext
from ecc.domains.engineering.crypto import encrypt_credential
from ecc.domains.personal.gmail_adapter import (
    GmailAdapter,
    _GmailHistoryCursor,
    _pack_credential,
)

settings = get_settings()
pytestmark = pytest.mark.skipif(
    not settings.database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)

_OWNER_EMAIL = "owner@example.test"


def _json_response(body: Any, *, status_code: int = 200) -> httpx.Response:
    return httpx.Response(status_code=status_code, json=body)


def _message_body(
    *,
    message_id: str,
    thread_id: str,
    from_addr: str,
    to_addrs: list[str],
    internal_date_ms: int,
    subject: str | None = "A subject",
    label_ids: list[str] | None = None,
    history_id: int = 1000,
) -> dict[str, Any]:
    headers = [{"name": "From", "value": from_addr}, {"name": "To", "value": ", ".join(to_addrs)}]
    if subject is not None:
        headers.append({"name": "Subject", "value": subject})
    return {
        "id": message_id,
        "threadId": thread_id,
        "labelIds": label_ids if label_ids is not None else ["INBOX"],
        "internalDate": str(internal_date_ms),
        "historyId": history_id,
        "payload": {"headers": headers},
    }


@pytest.fixture
def seeded_gmail_account() -> Iterator[tuple[ConnectorAccountContext, UUID]]:
    """A `workspaces`/`users`/`connector_accounts`(`provider='gmail'`)/
    `personal_domains`/`domain_consents` row set -- required for any
    `backfill`/`incremental_sync` call, both of which genuinely write to
    `email_threads`/`email_messages`/`pkos_nodes`/`pkos_evidence`/
    `entity_aliases` (none of this is mocked) and read `connector_
    accounts.owner_id`/`domain_consents` directly. Migration `0063`'s
    `_NEW_OWNER_FROM_CREATED_BY` trigger backfills `connector_accounts.
    owner_id` from `created_by` when not explicitly set below, matching
    `gmail_oauth.py`'s own real INSERT (which sets both to the same
    value) -- not relied on as a coincidence, confirmed by `test_owner_
    id_is_populated_from_created_by` below.
    """
    workspace_id = uuid4()
    owner_id = uuid4()
    account_id = uuid4()
    domain_id = uuid4()
    now = datetime.now(UTC)
    credential = _pack_credential("access-1", "refresh-1", now + timedelta(hours=1))
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, 'Gmail Sync Unit Test', 'UTC', :now)"
            ),
            {"id": workspace_id, "now": now},
        )
        create_identity(
            connection, workspace_id=workspace_id, user_id=owner_id, email=_OWNER_EMAIL, now=now
        )
        connection.execute(
            text(
                """
                INSERT INTO connector_accounts (
                    id, workspace_id, provider, external_account_id, display_name,
                    granted_scopes, encrypted_credentials, status, version,
                    created_by, updated_by, created_at, updated_at
                ) VALUES (
                    :id, :workspace_id, 'gmail', :external_account_id, 'Gmail sync test',
                    ARRAY['https://www.googleapis.com/auth/gmail.metadata'], :encrypted,
                    'active', 1, :actor_id, :actor_id, :now, :now
                )
                """
            ),
            {
                "id": account_id,
                "workspace_id": workspace_id,
                "external_account_id": _OWNER_EMAIL,
                "encrypted": encrypt_credential(credential),
                "actor_id": owner_id,
                "now": now,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO personal_domains (
                    id, workspace_id, owner_id, domain_key, classification, enabled,
                    enabled_at, created_by, updated_by, created_at, updated_at, version
                ) VALUES (
                    :id, :workspace_id, :owner_id, 'email', 'high_stakes', true,
                    :now, :owner_id, :owner_id, :now, :now, 1
                )
                """
            ),
            {"id": domain_id, "workspace_id": workspace_id, "owner_id": owner_id, "now": now},
        )
        connection.execute(
            text(
                "INSERT INTO domain_consents (id, workspace_id, owner_id, domain_key, "
                "granted_at, created_at) "
                "VALUES (:id, :workspace_id, :owner_id, 'email', :now, :now)"
            ),
            {"id": uuid4(), "workspace_id": workspace_id, "owner_id": owner_id, "now": now},
        )
    try:
        yield (
            ConnectorAccountContext(
                workspace_id=workspace_id,
                connector_account_id=account_id,
                external_account_id=_OWNER_EMAIL,
                credential=credential,
            ),
            owner_id,
        )
    finally:
        with engine.begin() as connection:
            for table in (
                "entity_aliases",
                "pkos_evidence",
                "pkos_nodes",
                "email_messages",
                "email_threads",
                "domain_consents",
                "personal_domains",
                "connector_accounts",
                "users",
            ):
                connection.execute(
                    text(f"DELETE FROM {table} WHERE workspace_id = :workspace_id"),  # noqa: S608
                    {"workspace_id": workspace_id},
                )
            connection.execute(
                text("DELETE FROM workspaces WHERE id = :workspace_id"),
                {"workspace_id": workspace_id},
            )
            connection.execute(
                text("DELETE FROM accounts WHERE email = :email"), {"email": _OWNER_EMAIL}
            )


def _revoke_consent(workspace_id: UUID, owner_id: UUID) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE domain_consents SET revoked_at = :now "
                "WHERE workspace_id = :workspace_id AND owner_id = :owner_id "
                "AND domain_key = 'email'"
            ),
            {"workspace_id": workspace_id, "owner_id": owner_id, "now": datetime.now(UTC)},
        )


def _threads_and_messages(workspace_id: UUID) -> list[dict[str, Any]]:
    with engine.begin() as connection:
        rows = (
            connection.execute(
                text(
                    "SELECT m.external_message_id, m.sender, m.recipients, m.direction, "
                    "t.external_thread_id, t.subject, t.last_message_at "
                    "FROM email_messages m JOIN email_threads t ON t.id = m.thread_id "
                    "WHERE m.workspace_id = :workspace_id ORDER BY m.sent_at"
                ),
                {"workspace_id": workspace_id},
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


def _resolved_person(workspace_id: UUID, email: str) -> dict[str, Any] | None:
    with engine.begin() as connection:
        row = (
            connection.execute(
                text(
                    "SELECT n.id, n.canonical_name FROM entity_aliases a "
                    "JOIN pkos_nodes n ON n.workspace_id = a.workspace_id AND n.id = a.entity_id "
                    "WHERE a.workspace_id = :workspace_id AND a.alias_type = 'email' "
                    "AND a.normalized_value = :normalized_value"
                ),
                {"workspace_id": workspace_id, "normalized_value": email.strip().casefold()},
            )
            .mappings()
            .one_or_none()
        )
        return dict(row) if row is not None else None


# --- ResourceType widening / non-message no-op ------------------------------


def test_backfill_still_no_ops_for_a_non_message_resource_type(
    seeded_gmail_account: tuple[ConnectorAccountContext, UUID],
) -> None:
    context, _owner_id = seeded_gmail_account
    adapter = GmailAdapter()
    outcome = adapter.backfill(context, "thread")
    assert outcome == adapter.backfill(context, "thread")
    assert outcome.status == "succeeded"
    assert outcome.items_processed == 0
    assert outcome.next_cursor is None


def test_incremental_sync_still_no_ops_for_a_non_message_resource_type(
    seeded_gmail_account: tuple[ConnectorAccountContext, UUID],
) -> None:
    context, _owner_id = seeded_gmail_account
    adapter = GmailAdapter()
    outcome = adapter.incremental_sync(context, "thread", "some-cursor")
    assert outcome.status == "succeeded"
    assert outcome.items_processed == 0
    assert outcome.next_cursor == "some-cursor"


# --- backfill: happy path, window, entity resolution ------------------------


def test_backfill_writes_messages_and_resolves_participants(
    seeded_gmail_account: tuple[ConnectorAccountContext, UUID],
) -> None:
    context, owner_id = seeded_gmail_account
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    bodies = {
        "msg-1": _message_body(
            message_id="msg-1",
            thread_id="thread-1",
            from_addr="Alice Sender <alice@example.test>",
            to_addrs=["owner@example.test"],
            internal_date_ms=now_ms,
            history_id=101,
        ),
        "msg-2": _message_body(
            message_id="msg-2",
            thread_id="thread-1",
            from_addr="Alice Sender <alice@example.test>",
            to_addrs=["owner@example.test", "Bob Recipient <bob@example.test>"],
            internal_date_ms=now_ms + 1000,
            history_id=102,
        ),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/gmail/v1/users/me/messages":
            return _json_response({"messages": [{"id": "msg-1"}, {"id": "msg-2"}]})
        if request.url.path.startswith("/gmail/v1/users/me/messages/"):
            message_id = request.url.path.rsplit("/", 1)[-1]
            return _json_response(bodies[message_id])
        raise AssertionError(f"unexpected request to {request.url}")

    adapter = GmailAdapter(transport=httpx.MockTransport(handler))
    outcome = adapter.backfill(context, "message", since=datetime.now(UTC) - timedelta(days=1))

    assert outcome.status == "succeeded"
    assert outcome.items_processed == 2
    assert outcome.next_cursor == "102"

    rows = _threads_and_messages(context.workspace_id)
    assert len(rows) == 2
    assert {row["external_message_id"] for row in rows} == {"msg-1", "msg-2"}
    assert all(row["external_thread_id"] == "thread-1" for row in rows)
    assert rows[0]["sender"] == "alice@example.test"
    assert set(rows[1]["recipients"]) == {"owner@example.test", "bob@example.test"}

    alice = _resolved_person(context.workspace_id, "alice@example.test")
    bob = _resolved_person(context.workspace_id, "bob@example.test")
    owner = _resolved_person(context.workspace_id, "owner@example.test")
    assert alice is not None and alice["canonical_name"] == "Alice Sender"
    assert bob is not None and bob["canonical_name"] == "Bob Recipient"
    assert owner is not None
    # The second message's `alice@example.test` reuses the entity resolved
    # by the first, rather than creating a duplicate `pkos_nodes` row.
    with engine.begin() as connection:
        count = connection.execute(
            text(
                "SELECT count(*) FROM entity_aliases WHERE workspace_id = :workspace_id "
                "AND normalized_value = 'alice@example.test'"
            ),
            {"workspace_id": context.workspace_id},
        ).scalar_one()
    assert count == 1


def test_backfill_thread_upsert_keeps_latest_last_message_at_and_first_subject(
    seeded_gmail_account: tuple[ConnectorAccountContext, UUID],
) -> None:
    """`_upsert_thread`'s `ON CONFLICT DO UPDATE` clause is exercised by
    every multi-message-per-thread test in this file (e.g. `test_backfill_
    writes_messages_and_resolves_participants`), but none of them actually
    assert on the *values* that clause's own `GREATEST(...)`/`COALESCE(...)`
    SQL produces -- every existing such test happens to process its
    messages in ascending chronological order, so a naive unconditional
    `EXCLUDED.last_message_at`/`EXCLUDED.subject` overwrite (no `GREATEST`/
    `COALESCE` at all) would produce byte-identical results and pass
    unnoticed. Mutation-confirmed: replacing both clauses with a plain
    `EXCLUDED.*` overwrite left the combined suite's 161 then-existing
    tests -- across this file, `test_gmail_connector_postgres.py`, and
    `test_engineering_connectors_postgres.py` -- passing (this file alone
    had 49 of those 161 at the time).

    Gmail's own `messages.list` returns messages newest-first by default,
    so `_sync_messages`'/`_sync_history`'s own per-page loop processes a
    thread's newest message before its older ones -- the exact ordering
    this test reproduces (`msg-newer` before `msg-older` in the mocked
    `messages.list` response), the opposite of the existing tests' own
    ordering. Without `GREATEST`, `email_threads.last_message_at` would
    regress to the *older* message's timestamp once it's processed second,
    corrupting the exact column `attention_items`/thread-list ordering
    (a later task's own scope) would rely on to mean "this thread's most
    recent activity." Without `COALESCE`, `subject` would be silently
    overwritten by whichever message happens to be processed last, instead
    of staying pinned to the thread's own original subject line.
    """
    context, _owner_id = seeded_gmail_account
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    older_ms = now_ms - 86_400_000
    bodies = {
        "msg-newer": _message_body(
            message_id="msg-newer",
            thread_id="thread-order",
            from_addr="alice@example.test",
            to_addrs=["owner@example.test"],
            internal_date_ms=now_ms,
            subject="Newest Subject",
            history_id=201,
        ),
        "msg-older": _message_body(
            message_id="msg-older",
            thread_id="thread-order",
            from_addr="alice@example.test",
            to_addrs=["owner@example.test"],
            internal_date_ms=older_ms,
            subject="Older Subject",
            history_id=200,
        ),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/gmail/v1/users/me/messages":
            # Newest-first, matching Gmail's own real `messages.list` order.
            return _json_response({"messages": [{"id": "msg-newer"}, {"id": "msg-older"}]})
        if request.url.path.startswith("/gmail/v1/users/me/messages/"):
            message_id = request.url.path.rsplit("/", 1)[-1]
            return _json_response(bodies[message_id])
        raise AssertionError(f"unexpected request to {request.url}")

    adapter = GmailAdapter(transport=httpx.MockTransport(handler))
    outcome = adapter.backfill(context, "message", since=datetime.now(UTC) - timedelta(days=2))

    assert outcome.status == "succeeded"
    assert outcome.items_processed == 2

    rows = _threads_and_messages(context.workspace_id)
    assert len(rows) == 2
    assert all(row["external_thread_id"] == "thread-order" for row in rows)
    # Both rows share one `email_threads` row -- both must agree.
    assert {row["subject"] for row in rows} == {"Newest Subject"}
    expected_last_message_at = datetime.fromtimestamp(now_ms / 1000, tz=UTC)
    for row in rows:
        assert row["last_message_at"] == expected_last_message_at


def test_backfill_uses_since_as_the_gmail_query_lower_bound(
    seeded_gmail_account: tuple[ConnectorAccountContext, UUID],
) -> None:
    context, _owner_id = seeded_gmail_account
    since = datetime(2026, 1, 1, tzinfo=UTC)
    captured_query: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/gmail/v1/users/me/messages":
            captured_query["q"] = request.url.params.get("q", "")
            return _json_response({"messages": []})
        if request.url.path == "/gmail/v1/users/me/profile":
            return _json_response({"historyId": 1})
        raise AssertionError(f"unexpected request to {request.url}")

    adapter = GmailAdapter(transport=httpx.MockTransport(handler))
    outcome = adapter.backfill(context, "message", since=since)
    assert outcome.status == "succeeded"
    assert captured_query["q"] == f"after:{int(since.timestamp())}"


def test_backfill_treats_a_naive_since_as_utc_not_local_time(
    seeded_gmail_account: tuple[ConnectorAccountContext, UUID],
) -> None:
    # Round 8 review (PR #130): `SyncRequest.since` has no `tzinfo`
    # requirement, so a direct API caller (bypassing `GmailPanel`'s own
    # always-UTC `toISOString()` construction) can reach `backfill` with a
    # naive `datetime`. `datetime.timestamp()` on a naive value is
    # local-system-time by Python's own definition -- round 17 closed this
    # exact code shape as unreachable; Task 8 makes it reachable.
    context, _owner_id = seeded_gmail_account
    naive_since = datetime(2026, 1, 1)  # noqa: DTZ001 -- deliberately naive
    aware_since = datetime(2026, 1, 1, tzinfo=UTC)
    captured_query: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/gmail/v1/users/me/messages":
            captured_query["q"] = request.url.params.get("q", "")
            return _json_response({"messages": []})
        if request.url.path == "/gmail/v1/users/me/profile":
            return _json_response({"historyId": 1})
        raise AssertionError(f"unexpected request to {request.url}")

    adapter = GmailAdapter(transport=httpx.MockTransport(handler))
    outcome = adapter.backfill(context, "message", since=naive_since)
    assert outcome.status == "succeeded"
    assert captured_query["q"] == f"after:{int(aware_since.timestamp())}"


def test_backfill_defaults_to_the_30_day_window_when_since_is_none(
    seeded_gmail_account: tuple[ConnectorAccountContext, UUID],
) -> None:
    context, _owner_id = seeded_gmail_account
    captured_query: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/gmail/v1/users/me/messages":
            captured_query["q"] = request.url.params.get("q", "")
            return _json_response({"messages": []})
        if request.url.path == "/gmail/v1/users/me/profile":
            return _json_response({"historyId": 1})
        raise AssertionError(f"unexpected request to {request.url}")

    adapter = GmailAdapter(transport=httpx.MockTransport(handler))
    before = int((datetime.now(UTC) - timedelta(days=30)).timestamp())
    outcome = adapter.backfill(context, "message", since=None)
    after = int((datetime.now(UTC) - timedelta(days=30)).timestamp())
    assert outcome.status == "succeeded"
    observed = int(captured_query["q"].removeprefix("after:"))
    # Small window: the "30 days ago" reference point is computed twice
    # (once by this test, once inside `backfill`), a few seconds apart.
    assert before <= observed <= after


def test_backfill_empty_window_seeds_next_cursor_from_profile(
    seeded_gmail_account: tuple[ConnectorAccountContext, UUID],
) -> None:
    """Zero messages in the requested window means no `historyId` was ever
    observed from a message resource -- `backfill` falls back to `users.
    getProfile` (the only other Gmail endpoint reporting the mailbox's
    current `historyId`) so `incremental_sync` still has a real cursor to
    resume from, rather than permanently deferring back to `backfill`.
    """
    context, _owner_id = seeded_gmail_account

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/gmail/v1/users/me/messages":
            return _json_response({"messages": []})
        if request.url.path == "/gmail/v1/users/me/profile":
            return _json_response({"emailAddress": _OWNER_EMAIL, "historyId": 555})
        raise AssertionError(f"unexpected request to {request.url}")

    adapter = GmailAdapter(transport=httpx.MockTransport(handler))
    outcome = adapter.backfill(context, "message", since=datetime.now(UTC) - timedelta(days=1))
    assert outcome.status == "succeeded"
    assert outcome.items_processed == 0
    assert outcome.next_cursor == "555"


# --- entity resolution: one participant's failure must not crash the call ----


def test_participant_resolution_failure_does_not_crash_the_sync_call(
    seeded_gmail_account: tuple[ConnectorAccountContext, UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 9 review: `_insert_message_if_new` commits the `email_
    messages`/`email_threads` write in its own transaction, closed before
    `_process_message`'s participant-resolution loop even starts (`_resolve_
    or_create_person` deliberately opens a separate transaction per
    participant -- see that function's own docstring). Previously, an
    uncaught exception from *any* participant's `_resolve_or_create_person`
    call -- a transient one (a deadlock, a dropped connection, the app's own
    `statement_timeout`; unlike every other guard in this file, this needs
    no malformed input at all) as much as an unexpected one -- propagated
    straight out of `_process_message`, aborting the *entire* sync call:
    every message still queued behind this one in the same call went
    unprocessed, not merely this one message's own remaining participants.

    This test proves the narrower, message-scoped half: a mid-loop failure
    resolving one participant of one message must not prevent a *second*,
    independent message (with entirely different participants) processed
    later in the *same* call from being written and resolved normally --
    `msg-2`'s handler raises `AssertionError` if it is ever skipped, so a
    regression to the pre-fix uncaught-crash behavior fails this test with
    that assertion rather than merely an unmet post-condition.
    `test_participant_resolution_failure_still_resolves_other_participants_
    of_the_same_message` (below) covers the second half: the *other*
    participants of the *same* message as the failing one are still
    resolved, not dropped alongside it.
    """
    import ecc.domains.personal.gmail_adapter as gmail_adapter_module

    context, _owner_id = seeded_gmail_account
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    first_body = _message_body(
        message_id="msg-1",
        thread_id="thread-1",
        from_addr="Bob Flaky <bob@example.test>",
        to_addrs=["owner@example.test"],
        internal_date_ms=now_ms,
    )
    second_body = _message_body(
        message_id="msg-2",
        thread_id="thread-2",
        from_addr="Carol Fine <carol@example.test>",
        to_addrs=["owner@example.test"],
        internal_date_ms=now_ms + 1000,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/gmail/v1/users/me/messages":
            return _json_response({"messages": [{"id": "msg-1"}, {"id": "msg-2"}]})
        if request.url.path == "/gmail/v1/users/me/messages/msg-1":
            return _json_response(first_body)
        if request.url.path == "/gmail/v1/users/me/messages/msg-2":
            return _json_response(second_body)
        raise AssertionError(f"unexpected request to {request.url}")

    real_resolve = gmail_adapter_module._resolve_or_create_person

    def flaky_resolve(*, email: str, **kwargs: Any) -> UUID:
        if email == "bob@example.test":
            raise RuntimeError("simulated transient DB failure resolving bob")
        return real_resolve(email=email, **kwargs)  # type: ignore[no-any-return]

    monkeypatch.setattr(gmail_adapter_module, "_resolve_or_create_person", flaky_resolve)

    adapter = GmailAdapter(transport=httpx.MockTransport(handler))
    # Must not raise -- the pre-fix behavior let bob's RuntimeError escape
    # uncaught here, which pytest would report as an unhandled exception
    # from this call rather than a normal return.
    outcome = adapter.backfill(context, "message", since=datetime.now(UTC) - timedelta(days=1))
    assert outcome.status == "succeeded"
    # Both messages were reached -- msg-2 was not stranded behind msg-1's
    # participant-resolution failure the way an uncaught exception would
    # have stranded it.
    assert outcome.items_processed == 2

    rows = _threads_and_messages(context.workspace_id)
    assert {row["external_message_id"] for row in rows} == {"msg-1", "msg-2"}
    # carol (msg-2's sender) is unaffected by bob's (msg-1's sender) failure.
    assert _resolved_person(context.workspace_id, "carol@example.test") is not None


def test_participant_resolution_failure_still_resolves_other_participants_of_the_same_message(
    seeded_gmail_account: tuple[ConnectorAccountContext, UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other half of the round-9 fix: within *one* message that has
    multiple participants, a resolution failure for one of them (here, the
    `To` header's `bob@example.test`) must not prevent the *other*
    participants of that same message (the sender, `alice@example.test`,
    and the second recipient, `carol@example.test`) from being resolved --
    the fix isolates the failure to just the one participant that raised,
    not the whole message's own participant set.
    """
    import ecc.domains.personal.gmail_adapter as gmail_adapter_module

    context, _owner_id = seeded_gmail_account
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    body = _message_body(
        message_id="msg-1",
        thread_id="thread-1",
        from_addr="Alice Sender <alice@example.test>",
        to_addrs=["Bob Flaky <bob@example.test>", "Carol Fine <carol@example.test>"],
        internal_date_ms=now_ms,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/gmail/v1/users/me/messages":
            return _json_response({"messages": [{"id": "msg-1"}]})
        if request.url.path == "/gmail/v1/users/me/messages/msg-1":
            return _json_response(body)
        raise AssertionError(f"unexpected request to {request.url}")

    real_resolve = gmail_adapter_module._resolve_or_create_person

    def flaky_resolve(*, email: str, **kwargs: Any) -> UUID:
        if email == "bob@example.test":
            raise RuntimeError("simulated transient DB failure resolving bob")
        return real_resolve(email=email, **kwargs)  # type: ignore[no-any-return]

    monkeypatch.setattr(gmail_adapter_module, "_resolve_or_create_person", flaky_resolve)

    adapter = GmailAdapter(transport=httpx.MockTransport(handler))
    outcome = adapter.backfill(context, "message", since=datetime.now(UTC) - timedelta(days=1))
    assert outcome.status == "succeeded"
    assert outcome.items_processed == 1

    rows = _threads_and_messages(context.workspace_id)
    assert len(rows) == 1
    assert set(rows[0]["recipients"]) == {"bob@example.test", "carol@example.test"}

    # alice (sender) and carol (the non-failing recipient) are resolved
    # despite bob's failure; bob himself is not resolved for this message.
    assert _resolved_person(context.workspace_id, "alice@example.test") is not None
    assert _resolved_person(context.workspace_id, "carol@example.test") is not None
    assert _resolved_person(context.workspace_id, "bob@example.test") is None

    # The person is not permanently lost, though -- a *later* message
    # naming bob still resolves him normally, once the transient failure
    # has cleared (proving the failure is isolated to this one message's
    # own evidence of bob, not a permanent block on ever resolving him).
    monkeypatch.setattr(gmail_adapter_module, "_resolve_or_create_person", real_resolve)
    second_body = _message_body(
        message_id="msg-2",
        thread_id="thread-2",
        from_addr="bob@example.test",
        to_addrs=["owner@example.test"],
        internal_date_ms=now_ms + 1000,
    )

    def handler2(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/gmail/v1/users/me/messages":
            return _json_response({"messages": [{"id": "msg-2"}]})
        if request.url.path == "/gmail/v1/users/me/messages/msg-2":
            return _json_response(second_body)
        raise AssertionError(f"unexpected request to {request.url}")

    adapter2 = GmailAdapter(transport=httpx.MockTransport(handler2))
    outcome2 = adapter2.backfill(context, "message", since=datetime.now(UTC) - timedelta(days=1))
    assert outcome2.status == "succeeded"
    assert _resolved_person(context.workspace_id, "bob@example.test") is not None


# --- consent re-verification -------------------------------------------------


def test_backfill_raises_when_consent_was_never_active(
    seeded_gmail_account: tuple[ConnectorAccountContext, UUID],
) -> None:
    context, owner_id = seeded_gmail_account
    _revoke_consent(context.workspace_id, owner_id)

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"no Gmail call should happen: {request.url}")

    adapter = GmailAdapter(transport=httpx.MockTransport(handler))
    with pytest.raises(RuntimeError, match="consent is not active"):
        adapter.backfill(context, "message", since=datetime.now(UTC) - timedelta(days=1))


def test_backfill_halts_when_consent_is_revoked_mid_window(
    seeded_gmail_account: tuple[ConnectorAccountContext, UUID],
) -> None:
    """Plan Task 2: "a revoked-mid-window consent halts the call." Revokes
    consent as a side effect of fetching the *first* message's metadata --
    the second message must never be fetched or written.

    `next_cursor is None` -- round 11 review: this partial branch is the
    second of the three sites (in call order: `list_response is None`,
    then this mid-loop consent-revocation check, then `get_response is
    None`) that previously handed out an unsafe `next_cursor` computed
    from `msg-1`'s own `historyId`, which a still-unprocessed `msg-2`
    could easily have a *lower* `historyId` than (see
    `test_backfill_partial_next_cursor_does_not_skip_a_lower_
    history_id_message`'s own docstring for the full mechanism); this
    test's own scenario doesn't control `msg-2`'s `historyId` at all
    (it's never fetched), so `next_cursor is None` is the only value this
    assertion can safely require, but it's exactly the value the round 11
    fix guarantees regardless of what that unseen `historyId` might be.
    """
    context, owner_id = seeded_gmail_account
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    first_body = _message_body(
        message_id="msg-1",
        thread_id="thread-1",
        from_addr="alice@example.test",
        to_addrs=["owner@example.test"],
        internal_date_ms=now_ms,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/gmail/v1/users/me/messages":
            return _json_response({"messages": [{"id": "msg-1"}, {"id": "msg-2"}]})
        if request.url.path == "/gmail/v1/users/me/messages/msg-1":
            return _json_response(first_body)
        if request.url.path == "/gmail/v1/users/me/messages/msg-2":
            raise AssertionError("msg-2 must not be fetched after consent is revoked")
        raise AssertionError(f"unexpected request to {request.url}")

    adapter = GmailAdapter(transport=httpx.MockTransport(handler))
    original_get = adapter._request_with_rate_limit_retry

    def instrumented(method: str, path: str, **kwargs: Any) -> httpx.Response | None:
        response = original_get(method, path, **kwargs)
        if path == "/gmail/v1/users/me/messages/msg-1":
            _revoke_consent(context.workspace_id, owner_id)
        return response

    adapter._request_with_rate_limit_retry = instrumented  # type: ignore[method-assign]

    outcome = adapter.backfill(context, "message", since=datetime.now(UTC) - timedelta(days=1))
    assert outcome.status == "partial"
    assert outcome.error_summary is not None and "revoked" in outcome.error_summary
    assert outcome.items_processed == 1
    assert outcome.next_cursor is None

    rows = _threads_and_messages(context.workspace_id)
    assert len(rows) == 1
    assert rows[0]["external_message_id"] == "msg-1"


# --- rate limiting ------------------------------------------------------------


def test_backfill_degrades_to_partial_when_rate_limited_beyond_the_bound(
    seeded_gmail_account: tuple[ConnectorAccountContext, UUID],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/gmail/v1/users/me/messages":
            return httpx.Response(
                429, json={"error": {"errors": [{"reason": "rateLimitExceeded"}]}}
            )
        raise AssertionError(f"unexpected request to {request.url}")

    context, _owner_id = seeded_gmail_account
    adapter = GmailAdapter(transport=httpx.MockTransport(handler), sleep=lambda _seconds: None)
    outcome = adapter.backfill(context, "message", since=datetime.now(UTC) - timedelta(days=1))
    assert outcome.status == "partial"
    assert outcome.error_summary is not None and "rate limit" in outcome.error_summary
    assert outcome.items_processed == 0


def test_backfill_succeeds_after_one_rate_limited_retry(
    seeded_gmail_account: tuple[ConnectorAccountContext, UUID],
) -> None:
    context, _owner_id = seeded_gmail_account
    calls = {"messages_list": 0}
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    body = _message_body(
        message_id="msg-1",
        thread_id="thread-1",
        from_addr="alice@example.test",
        to_addrs=["owner@example.test"],
        internal_date_ms=now_ms,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/gmail/v1/users/me/messages":
            calls["messages_list"] += 1
            if calls["messages_list"] == 1:
                return httpx.Response(
                    429, headers={"Retry-After": "0"}, json={"error": {"errors": []}}
                )
            return _json_response({"messages": [{"id": "msg-1"}]})
        if request.url.path == "/gmail/v1/users/me/messages/msg-1":
            return _json_response(body)
        raise AssertionError(f"unexpected request to {request.url}")

    adapter = GmailAdapter(transport=httpx.MockTransport(handler), sleep=lambda _seconds: None)
    outcome = adapter.backfill(context, "message", since=datetime.now(UTC) - timedelta(days=1))
    assert outcome.status == "succeeded"
    assert outcome.items_processed == 1


def test_a_genuine_403_is_not_treated_as_rate_limiting(
    seeded_gmail_account: tuple[ConnectorAccountContext, UUID],
) -> None:
    """A `403` whose `reason` isn't one of the rate-limit family (here,
    `insufficientPermissions`) must raise, not silently degrade to
    `partial` -- retrying can never fix it, and swallowing it would mask a
    real, non-transient failure as if it will resolve itself later.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/gmail/v1/users/me/messages":
            return httpx.Response(
                403, json={"error": {"errors": [{"reason": "insufficientPermissions"}]}}
            )
        raise AssertionError(f"unexpected request to {request.url}")

    context, _owner_id = seeded_gmail_account
    adapter = GmailAdapter(transport=httpx.MockTransport(handler))
    with pytest.raises(RuntimeError, match="status 403"):
        adapter.backfill(context, "message", since=datetime.now(UTC) - timedelta(days=1))


def test_backfill_partial_next_cursor_does_not_skip_a_lower_history_id_message(
    seeded_gmail_account: tuple[ConnectorAccountContext, UUID],
) -> None:
    """Round 11 review: a `partial` result from a rate-limited `messages.
    get` call previously handed `incremental_sync` a `next_cursor` computed
    as `max()` over every `historyId` already processed *this call* -- safe
    only if `messages.list`'s own ordering guaranteed ascending `historyId`,
    which it does not (Gmail's default sort is newest-received-first, and
    `historyId` does not track receipt order closely enough to guarantee
    monotonicity). Here, `msg-newer` (`historyId=2000`) is listed and
    processed first; `msg-older` (`historyId=500`, deliberately *lower* --
    an older message Gmail's own newest-first ordering can and does place
    behind a newer, higher-`historyId` one) is listed second and is
    rate-limited on its own `messages.get` call, past the bounded retry.
    Before the fix, `next_cursor` came back `"2000"` -- a value a real
    `history.list(startHistoryId=2000)` call would never surface
    `msg-older` (historyId 500) through again, silently and permanently
    losing it from the synced entity graph despite the sync call itself
    reporting real, if partial, progress. Fixed by returning `next_cursor=
    None` from every `partial` branch in `_sync_messages` that isn't the
    already-correct `budget_exhausted` one, so a subsequent
    `incremental_sync` call falls back to a fresh `backfill` instead of
    trusting an unsafe cursor.
    """
    context, _owner_id = seeded_gmail_account
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    newer_body = _message_body(
        message_id="msg-newer",
        thread_id="thread-newer",
        from_addr="alice@example.test",
        to_addrs=[_OWNER_EMAIL],
        internal_date_ms=now_ms,
        history_id=2000,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/gmail/v1/users/me/messages":
            # Newest-first, matching Gmail's own real `messages.list`
            # order -- `msg-older` (the lower-`historyId` message) is
            # listed *after* `msg-newer`, so it's still unprocessed when
            # its own `messages.get` call gets rate-limited below.
            return _json_response({"messages": [{"id": "msg-newer"}, {"id": "msg-older"}]})
        if request.url.path == "/gmail/v1/users/me/messages/msg-newer":
            return _json_response(newer_body)
        if request.url.path == "/gmail/v1/users/me/messages/msg-older":
            return httpx.Response(
                429, json={"error": {"errors": [{"reason": "rateLimitExceeded"}]}}
            )
        raise AssertionError(f"unexpected request to {request.url}")

    adapter = GmailAdapter(transport=httpx.MockTransport(handler), sleep=lambda _seconds: None)
    outcome = adapter.backfill(context, "message", since=datetime.now(UTC) - timedelta(days=2))

    assert outcome.status == "partial"
    assert outcome.items_processed == 1
    assert outcome.next_cursor is None

    rows = _threads_and_messages(context.workspace_id)
    assert {row["external_message_id"] for row in rows} == {"msg-newer"}


def test_backfill_partial_next_cursor_is_none_when_list_call_rate_limited_across_pages(
    seeded_gmail_account: tuple[ConnectorAccountContext, UUID],
) -> None:
    """Same finding as `test_backfill_partial_next_cursor_does_not_skip_a_
    lower_history_id_message` above, at the sibling `list_response is None`
    partial-return site instead of `get_response is None`: page 1's own
    message is fully processed (seeding a non-zero `highest_history_id`)
    before page 2's own `messages.list` call -- which could easily be
    listing further, still-unprocessed, potentially lower-`historyId`
    messages -- gets rate-limited past the bounded retry.
    """
    context, _owner_id = seeded_gmail_account
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    page1_body = _message_body(
        message_id="page1-msg",
        thread_id="thread-page1",
        from_addr="alice@example.test",
        to_addrs=[_OWNER_EMAIL],
        internal_date_ms=now_ms,
        history_id=2000,
    )
    calls = {"list": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/gmail/v1/users/me/messages":
            calls["list"] += 1
            if calls["list"] == 1:
                return _json_response(
                    {"messages": [{"id": "page1-msg"}], "nextPageToken": "page-2"}
                )
            return httpx.Response(
                429, json={"error": {"errors": [{"reason": "rateLimitExceeded"}]}}
            )
        if request.url.path == "/gmail/v1/users/me/messages/page1-msg":
            return _json_response(page1_body)
        raise AssertionError(f"unexpected request to {request.url}")

    adapter = GmailAdapter(transport=httpx.MockTransport(handler), sleep=lambda _seconds: None)
    outcome = adapter.backfill(context, "message", since=datetime.now(UTC) - timedelta(days=2))

    assert outcome.status == "partial"
    assert outcome.items_processed == 1
    assert outcome.next_cursor is None

    rows = _threads_and_messages(context.workspace_id)
    assert {row["external_message_id"] for row in rows} == {"page1-msg"}


# --- idempotent re-sync --------------------------------------------------------


def test_backfill_rerun_over_an_overlapping_window_does_not_duplicate_rows(
    seeded_gmail_account: tuple[ConnectorAccountContext, UUID],
) -> None:
    context, _owner_id = seeded_gmail_account
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    body = _message_body(
        message_id="msg-1",
        thread_id="thread-1",
        from_addr="alice@example.test",
        to_addrs=["owner@example.test"],
        internal_date_ms=now_ms,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/gmail/v1/users/me/messages":
            return _json_response({"messages": [{"id": "msg-1"}]})
        if request.url.path == "/gmail/v1/users/me/messages/msg-1":
            return _json_response(body)
        raise AssertionError(f"unexpected request to {request.url}")

    adapter = GmailAdapter(transport=httpx.MockTransport(handler))
    since = datetime.now(UTC) - timedelta(days=1)
    first = adapter.backfill(context, "message", since=since)
    second = adapter.backfill(context, "message", since=since)
    assert first.status == "succeeded"
    assert second.status == "succeeded"
    assert second.items_processed == 1

    rows = _threads_and_messages(context.workspace_id)
    assert len(rows) == 1
    with engine.begin() as connection:
        alias_count = connection.execute(
            text(
                "SELECT count(*) FROM entity_aliases WHERE workspace_id = :workspace_id "
                "AND normalized_value = 'alice@example.test'"
            ),
            {"workspace_id": context.workspace_id},
        ).scalar_one()
    assert alias_count == 1


def test_backfill_rerun_over_an_overlapping_window_does_not_bump_thread_updated_at(
    seeded_gmail_account: tuple[ConnectorAccountContext, UUID],
) -> None:
    """This round's own review finding: `_upsert_thread`'s `ON CONFLICT DO
    UPDATE` previously set `updated_at = :now` unconditionally, even on the
    exact no-op re-sync this file's sibling test above (`..._does_not_
    duplicate_rows`) already exercises -- that test only asserts row
    *counts*, never `email_threads.updated_at` itself, so it passed both
    before and after this fix and never caught the gap.

    `updated_at` matters beyond bookkeeping: `attention.py`'s own
    `email_thread_rows` derives `attention_items.source_entity_version`
    directly from it (`email_threads` has no real optimistic-concurrency
    `version` column of its own, per migration 0069). A `regenerate_
    attention` call after a no-op `updated_at` bump saw a "changed"
    version and un-dismissed any `email_thread` item the user had already
    dismissed for that thread -- even though nothing about the thread's
    real content moved. This test asserts the fix directly at the
    `GmailAdapter.backfill` level: an identical re-sync of the same single
    message must leave `email_threads.updated_at` unchanged.
    """
    context, _owner_id = seeded_gmail_account
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    body = _message_body(
        message_id="msg-1",
        thread_id="thread-1",
        from_addr="alice@example.test",
        to_addrs=["owner@example.test"],
        internal_date_ms=now_ms,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/gmail/v1/users/me/messages":
            return _json_response({"messages": [{"id": "msg-1"}]})
        if request.url.path == "/gmail/v1/users/me/messages/msg-1":
            return _json_response(body)
        raise AssertionError(f"unexpected request to {request.url}")

    adapter = GmailAdapter(transport=httpx.MockTransport(handler))
    since = datetime.now(UTC) - timedelta(days=1)
    first = adapter.backfill(context, "message", since=since)
    assert first.status == "succeeded"

    def _thread_updated_at() -> datetime:
        with engine.begin() as connection:
            return connection.execute(
                text(
                    "SELECT updated_at FROM email_threads "
                    "WHERE workspace_id = :workspace_id AND external_thread_id = 'thread-1'"
                ),
                {"workspace_id": context.workspace_id},
            ).scalar_one()

    updated_at_after_first = _thread_updated_at()

    second = adapter.backfill(context, "message", since=since)
    assert second.status == "succeeded"
    assert second.items_processed == 1

    assert _thread_updated_at() == updated_at_after_first


# --- incremental_sync ----------------------------------------------------------


def test_incremental_sync_with_none_cursor_defers_to_backfill(
    seeded_gmail_account: tuple[ConnectorAccountContext, UUID],
) -> None:
    context, _owner_id = seeded_gmail_account
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/gmail/v1/users/me/messages":
            return _json_response({"messages": []})
        if request.url.path == "/gmail/v1/users/me/profile":
            return _json_response({"historyId": 42})
        raise AssertionError(f"unexpected request to {request.url}")

    adapter = GmailAdapter(transport=httpx.MockTransport(handler))
    outcome = adapter.incremental_sync(context, "message", None)
    assert outcome.status == "succeeded"
    assert "/gmail/v1/users/me/messages" in calls
    assert "/gmail/v1/users/me/history" not in calls


def test_incremental_sync_resumes_from_history_cursor(
    seeded_gmail_account: tuple[ConnectorAccountContext, UUID],
) -> None:
    context, _owner_id = seeded_gmail_account
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    body = _message_body(
        message_id="msg-9",
        thread_id="thread-9",
        from_addr="carol@example.test",
        to_addrs=["owner@example.test"],
        internal_date_ms=now_ms,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/gmail/v1/users/me/history":
            assert request.url.params.get("startHistoryId") == "100"
            return _json_response(
                {
                    "history": [{"messagesAdded": [{"message": {"id": "msg-9"}}]}],
                    "historyId": 150,
                }
            )
        if request.url.path == "/gmail/v1/users/me/messages/msg-9":
            return _json_response(body)
        raise AssertionError(f"unexpected request to {request.url}")

    adapter = GmailAdapter(transport=httpx.MockTransport(handler))
    outcome = adapter.incremental_sync(context, "message", "100")
    assert outcome.status == "succeeded"
    assert outcome.items_processed == 1
    assert outcome.next_cursor == "150"

    rows = _threads_and_messages(context.workspace_id)
    assert len(rows) == 1
    assert rows[0]["external_message_id"] == "msg-9"


def test_incremental_sync_clears_a_stuck_record_even_when_an_intervening_page_has_no_entries(
    seeded_gmail_account: tuple[ConnectorAccountContext, UUID],
) -> None:
    """Code review finding on the `_GmailHistoryCursor` refactor: the input
    cursor `"100:101:2"` claims a prior call got stuck 2 messages into
    record 101. Page 1 of `history.list` (filtered server-side to
    `historyTypes=messageAdded`) comes back with an empty `history` array
    -- e.g. every real change on that page was a `labelsAdded`/
    `labelsRemoved` entry Gmail's own filter dropped -- but a non-empty
    `nextPageToken`, so the per-record loop that would normally clear the
    stuck point via `with_resumed_through` never runs even once. Page 2
    then gets rate-limited past the bounded retry. `next_cursor` must
    still collapse to the plain `"100"` here, matching what the
    pre-`_GmailHistoryCursor` code did unconditionally at this exact
    branch -- not round-trip the stale `"100:101:2"` verbatim, which
    would wrongly re-trim 2 messages off whatever record 101 turns out to
    be on the next, successful call.
    """
    context, _owner_id = seeded_gmail_account
    calls = {"history": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/gmail/v1/users/me/history":
            calls["history"] += 1
            if calls["history"] == 1:
                assert request.url.params.get("startHistoryId") == "100"
                return _json_response({"history": [], "nextPageToken": "page-2"})
            return httpx.Response(
                429, json={"error": {"errors": [{"reason": "rateLimitExceeded"}]}}
            )
        raise AssertionError(f"unexpected request to {request.url}")

    adapter = GmailAdapter(transport=httpx.MockTransport(handler), sleep=lambda _seconds: None)
    outcome = adapter.incremental_sync(context, "message", "100:101:2")

    assert outcome.status == "partial"
    assert outcome.items_processed == 0
    assert outcome.next_cursor == "100"


def test_incremental_sync_falls_back_to_backfill_when_history_id_expired(
    seeded_gmail_account: tuple[ConnectorAccountContext, UUID],
) -> None:
    """Gmail retains `history` for a rolling window, not indefinitely -- a
    `startHistoryId` older than that window 404s, and the only correct
    recovery is a fresh `backfill` (disclosed design decision, see
    `_sync_history`'s own `history_response.status_code == 404` branch
    comment -- not a docstring; neither `_sync_history` nor `_sync_messages`
    has ever had one. Round 13 review fixed this identical stale
    cross-reference in `gmail_adapter.py`'s own module docstring (present
    since the original Task 2 commit) but missed this file's own copy of
    it, left stale until round 14 review)."""
    context, _owner_id = seeded_gmail_account
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/gmail/v1/users/me/history":
            return httpx.Response(404, json={"error": {"errors": [{"reason": "notFound"}]}})
        if request.url.path == "/gmail/v1/users/me/messages":
            return _json_response({"messages": []})
        if request.url.path == "/gmail/v1/users/me/profile":
            return _json_response({"historyId": 1})
        raise AssertionError(f"unexpected request to {request.url}")

    adapter = GmailAdapter(transport=httpx.MockTransport(handler))
    outcome = adapter.incremental_sync(context, "message", "999999")
    assert outcome.status == "succeeded"
    assert "/gmail/v1/users/me/messages" in calls


def test_incremental_sync_raises_when_consent_was_never_active(
    seeded_gmail_account: tuple[ConnectorAccountContext, UUID],
) -> None:
    context, owner_id = seeded_gmail_account
    _revoke_consent(context.workspace_id, owner_id)

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"no Gmail call should happen: {request.url}")

    adapter = GmailAdapter(transport=httpx.MockTransport(handler))
    with pytest.raises(RuntimeError, match="consent is not active"):
        adapter.incremental_sync(context, "message", "100")


# --- owner_id trigger precondition -------------------------------------------


def test_owner_id_is_populated_from_created_by(
    seeded_gmail_account: tuple[ConnectorAccountContext, UUID],
) -> None:
    """`seeded_gmail_account`'s own `connector_accounts` INSERT above does
    not explicitly set `owner_id` -- this asserts migration `0063`'s
    trigger actually backfills it from `created_by` in this sandbox's real
    Postgres, rather than every other test in this file silently depending
    on that being true without ever checking it.
    """
    context, owner_id = seeded_gmail_account
    with engine.begin() as connection:
        row = connection.execute(
            text("SELECT owner_id FROM connector_accounts WHERE id = :id"),
            {"id": context.connector_account_id},
        ).one()
    assert row[0] == owner_id


# --- malformed message shape --------------------------------------------------


def test_nul_byte_in_from_address_is_skipped_not_a_crash(
    seeded_gmail_account: tuple[ConnectorAccountContext, UUID],
) -> None:
    """A JSON-escaped `\\u0000` in Gmail's own response body decodes to a
    real NUL character in the parsed `From` address -- Postgres `text`/
    `varchar` columns can never store `0x00` (a distinct constraint from
    `_MAX_EMAIL_ADDRESS_LENGTH`'s column-width check above), so this
    previously reached the `email_messages` INSERT uncaught (`psycopg.
    DataError`), crashing the whole sync call the same way an over-length
    address did -- found by the same review pass. Must degrade to a
    skipped message, not a crash.
    """
    context, _owner_id = seeded_gmail_account
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    body = _message_body(
        message_id="msg-nul",
        thread_id="thread-nul",
        from_addr="weird\x00addr@example.test",
        to_addrs=[_OWNER_EMAIL],
        internal_date_ms=now_ms,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/gmail/v1/users/me/messages":
            return _json_response({"messages": [{"id": "msg-nul"}]})
        if request.url.path.startswith("/gmail/v1/users/me/messages/"):
            return _json_response(body)
        if request.url.path == "/gmail/v1/users/me/profile":
            return _json_response({"historyId": 1})
        raise AssertionError(f"unexpected request to {request.url}")

    adapter = GmailAdapter(transport=httpx.MockTransport(handler))
    outcome = adapter.backfill(context, "message", since=datetime.now(UTC) - timedelta(days=1))
    assert outcome.status == "succeeded"
    assert outcome.items_processed == 1
    assert _threads_and_messages(context.workspace_id) == []


def test_nul_byte_in_from_display_name_is_skipped_not_a_crash(
    seeded_gmail_account: tuple[ConnectorAccountContext, UUID],
) -> None:
    """Same NUL-byte hazard as the address case above, but in the display-
    name half of the header -- `parseaddr` keeps it separate from the
    address, and it flows to `pkos_nodes.canonical_name` (also `text`)
    via `_resolve_or_create_person`, not `email_messages.sender`. A valid
    address alongside a NUL-carrying display name must still be treated
    as unparseable as a whole (matching `_parse_address`'s own "can't
    make sense of it" contract), not silently truncate/strip just the
    display name and resolve a person from the rest.
    """
    context, _owner_id = seeded_gmail_account
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    body = _message_body(
        message_id="msg-nul-name",
        thread_id="thread-nul-name",
        from_addr="Weird\x00Name <alice@example.test>",
        to_addrs=[_OWNER_EMAIL],
        internal_date_ms=now_ms,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/gmail/v1/users/me/messages":
            return _json_response({"messages": [{"id": "msg-nul-name"}]})
        if request.url.path.startswith("/gmail/v1/users/me/messages/"):
            return _json_response(body)
        if request.url.path == "/gmail/v1/users/me/profile":
            return _json_response({"historyId": 1})
        raise AssertionError(f"unexpected request to {request.url}")

    adapter = GmailAdapter(transport=httpx.MockTransport(handler))
    outcome = adapter.backfill(context, "message", since=datetime.now(UTC) - timedelta(days=1))
    assert outcome.status == "succeeded"
    assert outcome.items_processed == 1
    assert _threads_and_messages(context.workspace_id) == []
    assert _resolved_person(context.workspace_id, "alice@example.test") is None


def test_nul_byte_in_subject_is_dropped_but_message_still_syncs(
    seeded_gmail_account: tuple[ConnectorAccountContext, UUID],
) -> None:
    """`email_threads.subject` is nullable, so a NUL-carrying `Subject`
    header only drops that one field rather than the whole message --
    unlike a NUL in the sender/recipient address, nothing else about the
    message depends on it.
    """
    context, _owner_id = seeded_gmail_account
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    body = _message_body(
        message_id="msg-nul-subject",
        thread_id="thread-nul-subject",
        from_addr="alice@example.test",
        to_addrs=[_OWNER_EMAIL],
        internal_date_ms=now_ms,
        subject="Weird\x00Subject",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/gmail/v1/users/me/messages":
            return _json_response({"messages": [{"id": "msg-nul-subject"}]})
        if request.url.path.startswith("/gmail/v1/users/me/messages/"):
            return _json_response(body)
        raise AssertionError(f"unexpected request to {request.url}")

    adapter = GmailAdapter(transport=httpx.MockTransport(handler))
    outcome = adapter.backfill(context, "message", since=datetime.now(UTC) - timedelta(days=1))
    assert outcome.status == "succeeded"
    assert outcome.items_processed == 1

    rows = _threads_and_messages(context.workspace_id)
    assert len(rows) == 1
    assert rows[0]["subject"] is None


def test_oversized_from_address_is_skipped_not_a_crash(
    seeded_gmail_account: tuple[ConnectorAccountContext, UUID],
) -> None:
    """`email_messages.sender`/`.recipients` are `VARCHAR(320)` (migration
    `0069`); `email.utils.parseaddr` has no length limit of its own, so a
    `From` header whose address portion exceeds that column width
    previously reached Postgres uncaught (`StringDataRightTruncation`),
    aborting the entire sync call instead of skipping just this one
    malformed message -- found by review. A real Gmail sender fully
    controls their own `From` header value; this must degrade the same
    way any other malformed message already does (skipped, contributing
    to `items_processed` but writing nothing -- see `_process_message`'s
    own docstring), not crash the call.
    """
    context, _owner_id = seeded_gmail_account
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    long_addr = "x" * 400 + "@example.test"
    body = _message_body(
        message_id="msg-oversized",
        thread_id="thread-oversized",
        from_addr=f"Attacker <{long_addr}>",
        to_addrs=[_OWNER_EMAIL],
        internal_date_ms=now_ms,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/gmail/v1/users/me/messages":
            return _json_response({"messages": [{"id": "msg-oversized"}]})
        if request.url.path.startswith("/gmail/v1/users/me/messages/"):
            return _json_response(body)
        if request.url.path == "/gmail/v1/users/me/profile":
            return _json_response({"historyId": 1})
        raise AssertionError(f"unexpected request to {request.url}")

    adapter = GmailAdapter(transport=httpx.MockTransport(handler))
    outcome = adapter.backfill(context, "message", since=datetime.now(UTC) - timedelta(days=1))
    assert outcome.status == "succeeded"
    assert outcome.items_processed == 1
    assert _threads_and_messages(context.workspace_id) == []


def test_oversized_recipient_address_is_dropped_but_message_still_syncs(
    seeded_gmail_account: tuple[ConnectorAccountContext, UUID],
) -> None:
    """Same bound as the `From` case above, but on one `To` address among
    several -- only the oversized recipient is dropped (matching how any
    other individually-unparseable recipient is already handled), the
    message itself still syncs with its remaining, well-formed recipients.
    """
    context, _owner_id = seeded_gmail_account
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    long_addr = "y" * 400 + "@example.test"
    body = _message_body(
        message_id="msg-partial-recipients",
        thread_id="thread-partial-recipients",
        from_addr="alice@example.test",
        to_addrs=[_OWNER_EMAIL, f"Oversized <{long_addr}>"],
        internal_date_ms=now_ms,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/gmail/v1/users/me/messages":
            return _json_response({"messages": [{"id": "msg-partial-recipients"}]})
        if request.url.path.startswith("/gmail/v1/users/me/messages/"):
            return _json_response(body)
        raise AssertionError(f"unexpected request to {request.url}")

    adapter = GmailAdapter(transport=httpx.MockTransport(handler))
    outcome = adapter.backfill(context, "message", since=datetime.now(UTC) - timedelta(days=1))
    assert outcome.status == "succeeded"
    assert outcome.items_processed == 1

    rows = _threads_and_messages(context.workspace_id)
    assert len(rows) == 1
    assert rows[0]["recipients"] == [_OWNER_EMAIL]


def _surrogate_json_response(body: dict[str, Any], *, marker_replacement: str) -> httpx.Response:
    """Builds an httpx `Response` whose raw body bytes carry a literal
    `\\ud800`-style JSON escape (an unpaired UTF-16 surrogate) at every
    `"__SURROGATE__"` marker in `body` -- round 2 review's
    `_contains_unpaired_surrogate` finding. This can't be built via
    `httpx.Response(json=...)`/`_json_response` above: constructing an
    actual Python `str` containing a real surrogate character first (as
    `test_nul_byte_in_from_address_is_skipped_not_a_crash` does for
    `\\x00`) and handing it to `json=` fails *at response-construction
    time* (httpx's own `encode_json` calls `.encode("utf-8")` on the
    dumped JSON text, which raises the very `UnicodeEncodeError` this
    test means to reproduce, one layer too early -- before the response
    even exists to send). The real Gmail wire response never contains a
    raw surrogate *byte*; it contains the literal six-character ASCII
    escape sequence `\\ud800`, which `json.dumps` on an ordinary marker
    string followed by a text substitution reproduces exactly, and which
    `json.loads` (inside `response.json()`, same as this adapter's own
    call) then decodes into a real unpaired-surrogate `str` -- the
    identical shape `json.loads` would produce from a genuine Gmail
    response.
    """
    return httpx.Response(
        status_code=200,
        content=json_dumps(body).replace("__SURROGATE__", marker_replacement).encode("utf-8"),
        headers={"content-type": "application/json"},
    )


def test_unpaired_surrogate_in_from_display_name_is_skipped_not_a_crash(
    seeded_gmail_account: tuple[ConnectorAccountContext, UUID],
) -> None:
    """A lone (unpaired) UTF-16 surrogate escape in Gmail's own response
    JSON (e.g. from a malformed RFC 2047 encoded-word header Gmail's own
    decoder resolved leniently) decodes cleanly via `json.loads` into a
    Python `str` that *looks* like any other string but cannot be
    `.encode("utf-8")`-d -- required for psycopg to put it on the wire.
    Previously this reached the `pkos_nodes.canonical_name` INSERT
    uncaught as a raw `UnicodeEncodeError` (not even a DB-side
    `DataError`), aborting the entire sync call for one malformed message
    -- the same failure class `_contains_nul` already closed for an
    embedded NUL byte, but distinct and previously unguarded. Must
    degrade to a skipped message, not a crash.
    """
    context, _owner_id = seeded_gmail_account
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    body = _message_body(
        message_id="msg-surrogate",
        thread_id="thread-surrogate",
        from_addr="Weird__SURROGATE__Name <alice@example.test>",
        to_addrs=[_OWNER_EMAIL],
        internal_date_ms=now_ms,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/gmail/v1/users/me/messages":
            return _json_response({"messages": [{"id": "msg-surrogate"}]})
        if request.url.path.startswith("/gmail/v1/users/me/messages/"):
            return _surrogate_json_response(body, marker_replacement=r"\ud800")
        if request.url.path == "/gmail/v1/users/me/profile":
            return _json_response({"historyId": 1})
        raise AssertionError(f"unexpected request to {request.url}")

    adapter = GmailAdapter(transport=httpx.MockTransport(handler))
    outcome = adapter.backfill(context, "message", since=datetime.now(UTC) - timedelta(days=1))
    assert outcome.status == "succeeded"
    assert outcome.items_processed == 1
    assert _threads_and_messages(context.workspace_id) == []
    assert _resolved_person(context.workspace_id, "alice@example.test") is None


def test_unpaired_surrogate_in_message_id_is_skipped_not_a_crash(
    seeded_gmail_account: tuple[ConnectorAccountContext, UUID],
) -> None:
    """Same hazard as the display-name case above, but in Gmail's own `id`
    field rather than attacker-influenced header content -- `message_id`
    seeds `source_ref = f"gmail:{message_id}"`, which `_resolve_or_create_
    person` `.encode()`s for its `sha256`; an unpaired surrogate there
    raised the identical uncaught `UnicodeEncodeError` one call deeper,
    from a response shape this adapter's own docstring already treats
    every other field of as untrusted-until-validated. `external_thread_
    id`/`external_message_id` are also `VARCHAR(255)` -- `_is_valid_
    external_id` closes both the surrogate and the (previously entirely
    unguarded) length/NUL gap for this pair of fields in one guard.
    """
    context, _owner_id = seeded_gmail_account
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    body = _message_body(
        message_id="msg-__SURROGATE__-id",
        thread_id="thread-surrogate-id",
        from_addr="alice@example.test",
        to_addrs=[_OWNER_EMAIL],
        internal_date_ms=now_ms,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/gmail/v1/users/me/messages":
            return _json_response({"messages": [{"id": "msg-surrogate-id"}]})
        if request.url.path.startswith("/gmail/v1/users/me/messages/"):
            return _surrogate_json_response(body, marker_replacement=r"\ud800")
        if request.url.path == "/gmail/v1/users/me/profile":
            return _json_response({"historyId": 1})
        raise AssertionError(f"unexpected request to {request.url}")

    adapter = GmailAdapter(transport=httpx.MockTransport(handler))
    outcome = adapter.backfill(context, "message", since=datetime.now(UTC) - timedelta(days=1))
    assert outcome.status == "succeeded"
    assert outcome.items_processed == 1
    assert _threads_and_messages(context.workspace_id) == []
    assert _resolved_person(context.workspace_id, "alice@example.test") is None


def test_unpaired_surrogate_in_subject_is_dropped_but_message_still_syncs(
    seeded_gmail_account: tuple[ConnectorAccountContext, UUID],
) -> None:
    """Same hazard as `test_unpaired_surrogate_in_from_display_name_is_
    skipped_not_a_crash` above, but in `Subject` -- and, like `test_nul_
    byte_in_subject_is_dropped_but_message_still_syncs`, `email_threads.
    subject` is nullable, so this drops only that one field rather than
    the whole message (unlike the `From`/`id` cases, nothing else about
    the message depends on `Subject`). Must not crash and must not leave
    the unencodable value on the stored row.
    """
    context, _owner_id = seeded_gmail_account
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    body = _message_body(
        message_id="msg-surrogate-subject",
        thread_id="thread-surrogate-subject",
        from_addr="alice@example.test",
        to_addrs=[_OWNER_EMAIL],
        internal_date_ms=now_ms,
        subject="Weird__SURROGATE__Subject",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/gmail/v1/users/me/messages":
            return _json_response({"messages": [{"id": "msg-surrogate-subject"}]})
        if request.url.path.startswith("/gmail/v1/users/me/messages/"):
            return _surrogate_json_response(body, marker_replacement=r"\ud800")
        raise AssertionError(f"unexpected request to {request.url}")

    adapter = GmailAdapter(transport=httpx.MockTransport(handler))
    outcome = adapter.backfill(context, "message", since=datetime.now(UTC) - timedelta(days=1))
    assert outcome.status == "succeeded"
    assert outcome.items_processed == 1

    rows = _threads_and_messages(context.workspace_id)
    assert len(rows) == 1
    assert rows[0]["subject"] is None


def test_unpaired_surrogate_in_thread_id_is_skipped_not_a_crash(
    seeded_gmail_account: tuple[ConnectorAccountContext, UUID],
) -> None:
    """Same hazard as `test_unpaired_surrogate_in_message_id_is_skipped_
    not_a_crash` above, but in Gmail's own `threadId` field rather than
    `id` -- `_is_valid_external_id` (see its own comment) guards both
    `email_messages.external_message_id` and `email_threads.external_
    thread_id` identically; this covers the second of that pair, which
    `test_oversized_message_id_is_skipped_not_a_crash` below covers only
    for length, not for an unpaired surrogate.
    """
    context, _owner_id = seeded_gmail_account
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    body = _message_body(
        message_id="msg-surrogate-thread",
        thread_id="thread-__SURROGATE__-id",
        from_addr="alice@example.test",
        to_addrs=[_OWNER_EMAIL],
        internal_date_ms=now_ms,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/gmail/v1/users/me/messages":
            return _json_response({"messages": [{"id": "msg-surrogate-thread"}]})
        if request.url.path.startswith("/gmail/v1/users/me/messages/"):
            return _surrogate_json_response(body, marker_replacement=r"\ud800")
        if request.url.path == "/gmail/v1/users/me/profile":
            return _json_response({"historyId": 1})
        raise AssertionError(f"unexpected request to {request.url}")

    adapter = GmailAdapter(transport=httpx.MockTransport(handler))
    outcome = adapter.backfill(context, "message", since=datetime.now(UTC) - timedelta(days=1))
    assert outcome.status == "succeeded"
    assert outcome.items_processed == 1
    assert _threads_and_messages(context.workspace_id) == []
    assert _resolved_person(context.workspace_id, "alice@example.test") is None


def test_oversized_message_id_is_skipped_not_a_crash(
    seeded_gmail_account: tuple[ConnectorAccountContext, UUID],
) -> None:
    """`email_messages.external_message_id` is `VARCHAR(255)` (migration
    `0069`) -- previously nothing checked `body["id"]`'s length before
    the INSERT, unlike `sender`/`recipients`' own `_MAX_EMAIL_ADDRESS_
    LENGTH` bound. `_is_valid_external_id` closes it the same way.
    """
    context, _owner_id = seeded_gmail_account
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    long_id = "m" * 300
    body = _message_body(
        message_id=long_id,
        thread_id="thread-oversized-id",
        from_addr="alice@example.test",
        to_addrs=[_OWNER_EMAIL],
        internal_date_ms=now_ms,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/gmail/v1/users/me/messages":
            return _json_response({"messages": [{"id": long_id}]})
        if request.url.path.startswith("/gmail/v1/users/me/messages/"):
            return _json_response(body)
        if request.url.path == "/gmail/v1/users/me/profile":
            return _json_response({"historyId": 1})
        raise AssertionError(f"unexpected request to {request.url}")

    adapter = GmailAdapter(transport=httpx.MockTransport(handler))
    outcome = adapter.backfill(context, "message", since=datetime.now(UTC) - timedelta(days=1))
    assert outcome.status == "succeeded"
    assert outcome.items_processed == 1
    assert _threads_and_messages(context.workspace_id) == []


def test_to_header_display_name_with_comma_does_not_corrupt_recipients(
    seeded_gmail_account: tuple[ConnectorAccountContext, UUID],
) -> None:
    """`To: "Doe, John" <john@example.test>, jane@example.test` is a
    single, valid recipient (`john@example.test`) plus a second
    (`jane@example.test`) -- a naive `to_header.split(",")` (the previous
    implementation) breaks the quoted display name's own comma into two
    bogus fragments (`'"Doe'`, `' John" <john@example.test>'`); `parseaddr`
    on each fragment separately does not merely mis-format the name, it
    silently *loses the real address* (`' John" <john@example.test>'`
    parses to address `'John'`, not `'john@example.test'`) while
    fabricating a second bogus "recipient" (address `'Doe'`) that isn't
    an email address at all. Both bogus fragments pass length/NUL
    validation and previously reached `email_messages.recipients` and a
    real `pkos_nodes` person with a fake "email" -- silent entity-graph
    corruption from a header any real Gmail sender fully controls, found
    by round 2 review. `email.utils.getaddresses` (comma-list-aware)
    parses this correctly instead: two recipients, the quoted comma kept
    inside the display name, `john@example.test`'s address intact.
    """
    context, _owner_id = seeded_gmail_account
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    body = _message_body(
        message_id="msg-quoted-comma",
        thread_id="thread-quoted-comma",
        from_addr="alice@example.test",
        to_addrs=["ignored -- overridden by raw_to below"],
        internal_date_ms=now_ms,
    )
    body["payload"]["headers"] = [
        {"name": "From", "value": "alice@example.test"},
        {
            "name": "To",
            "value": '"Doe, John" <john@example.test>, jane@example.test',
        },
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/gmail/v1/users/me/messages":
            return _json_response({"messages": [{"id": "msg-quoted-comma"}]})
        if request.url.path.startswith("/gmail/v1/users/me/messages/"):
            return _json_response(body)
        raise AssertionError(f"unexpected request to {request.url}")

    adapter = GmailAdapter(transport=httpx.MockTransport(handler))
    outcome = adapter.backfill(context, "message", since=datetime.now(UTC) - timedelta(days=1))
    assert outcome.status == "succeeded"
    assert outcome.items_processed == 1

    rows = _threads_and_messages(context.workspace_id)
    assert len(rows) == 1
    assert sorted(rows[0]["recipients"]) == ["jane@example.test", "john@example.test"]

    john = _resolved_person(context.workspace_id, "john@example.test")
    assert john is not None
    assert john["canonical_name"] == "Doe, John"

    # The bogus fragment-addresses a naive comma-split previously produced
    # must never have been resolved as if they were real participants.
    assert _resolved_person(context.workspace_id, "doe") is None
    assert _resolved_person(context.workspace_id, "john") is None


def test_to_header_trailing_comma_does_not_drop_the_whole_message(
    seeded_gmail_account: tuple[ConnectorAccountContext, UUID],
) -> None:
    """Round 3 review: `email.utils.getaddresses` (round 2's own fix,
    above) defaults to `strict=True`, which validates a header's parsed
    address count against `1 + (top-level comma count)` and, on a
    mismatch, discards the *entire* result as a single `("", "")`
    sentinel rather than only the offending part -- CPython's own defense
    against a *different* spoofing shape (`'alice@x.test<bob@y.test>'`,
    no comma, one field ambiguously parsing into two addresses). A `To`
    header ending in a bare trailing comma (`"alice@x.test, bob@x.test,"`
    -- real mail systems produce this, and Gmail forwards a sender's raw
    header verbatim) is consumed without leaving a padding empty tuple
    behind, so the actual parsed count comes in one short of the formula's
    expectation and every real recipient gets discarded by that same
    all-or-nothing sentinel -- `recipients` ends up `[]`, and
    `_process_message`'s own `if not recipients: return None` then drops
    the *entire* message (sender, subject, thread -- not just the
    trailing-comma artifact) with no error and no `error_summary`. An
    attacker who wants a message to never appear in the synced entity
    graph needs only append one comma to their own `To` header.
    `_getaddresses_resilient` (see its own docstring) retries once with
    the bare trailing comma stripped, recovering both real recipients
    here without weakening `getaddresses`' actual protection (see the
    companion test below).
    """
    context, _owner_id = seeded_gmail_account
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    body = _message_body(
        message_id="msg-trailing-comma",
        thread_id="thread-trailing-comma",
        from_addr="alice@example.test",
        to_addrs=["ignored -- overridden by raw_to below"],
        internal_date_ms=now_ms,
    )
    body["payload"]["headers"] = [
        {"name": "From", "value": "alice@example.test"},
        {"name": "To", "value": "bob@example.test, carol@example.test,"},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/gmail/v1/users/me/messages":
            return _json_response({"messages": [{"id": "msg-trailing-comma"}]})
        if request.url.path.startswith("/gmail/v1/users/me/messages/"):
            return _json_response(body)
        raise AssertionError(f"unexpected request to {request.url}")

    adapter = GmailAdapter(transport=httpx.MockTransport(handler))
    outcome = adapter.backfill(context, "message", since=datetime.now(UTC) - timedelta(days=1))
    assert outcome.status == "succeeded"
    assert outcome.items_processed == 1

    rows = _threads_and_messages(context.workspace_id)
    assert len(rows) == 1
    assert sorted(rows[0]["recipients"]) == ["bob@example.test", "carol@example.test"]
    assert _resolved_person(context.workspace_id, "bob@example.test") is not None
    assert _resolved_person(context.workspace_id, "carol@example.test") is not None


def test_to_header_ambiguous_no_comma_address_is_still_safely_dropped(
    seeded_gmail_account: tuple[ConnectorAccountContext, UUID],
) -> None:
    """Companion to `test_to_header_trailing_comma_does_not_drop_the_whole_
    message` above -- `_getaddresses_resilient`'s trailing-comma retry
    must not weaken `getaddresses(strict=True)`'s real protection. A `To`
    header with no comma at all that itself parses ambiguously into two
    addresses (`'alice@example.test<bob@example.test>'` -- the shape
    `strict=True` exists to reject, e.g. CPython's own fixed spoofing
    case) has no trailing comma for the retry to strip, so the retry is a
    no-op and `getaddresses`' own `("", "")` sentinel stands: this message
    still has zero valid recipients and is still safely dropped entirely,
    exactly as before this round's fix -- not a regression that starts
    trusting an ambiguous header as two real recipients.
    """
    context, _owner_id = seeded_gmail_account
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    body = _message_body(
        message_id="msg-ambiguous-to",
        thread_id="thread-ambiguous-to",
        from_addr="alice@example.test",
        to_addrs=["ignored -- overridden by raw_to below"],
        internal_date_ms=now_ms,
    )
    body["payload"]["headers"] = [
        {"name": "From", "value": "alice@example.test"},
        {"name": "To", "value": "bob@example.test<carol@example.test>"},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/gmail/v1/users/me/messages":
            return _json_response({"messages": [{"id": "msg-ambiguous-to"}]})
        if request.url.path.startswith("/gmail/v1/users/me/messages/"):
            return _json_response(body)
        if request.url.path == "/gmail/v1/users/me/profile":
            return _json_response({"historyId": 1})
        raise AssertionError(f"unexpected request to {request.url}")

    adapter = GmailAdapter(transport=httpx.MockTransport(handler))
    outcome = adapter.backfill(context, "message", since=datetime.now(UTC) - timedelta(days=1))
    assert outcome.status == "succeeded"
    assert outcome.items_processed == 1
    assert _threads_and_messages(context.workspace_id) == []
    assert _resolved_person(context.workspace_id, "bob@example.test") is None
    assert _resolved_person(context.workspace_id, "carol@example.test") is None


def test_to_header_ambiguous_address_with_trailing_comma_is_still_safely_dropped(
    seeded_gmail_account: tuple[ConnectorAccountContext, UUID],
) -> None:
    """Round 4 review: a second companion to `test_to_header_trailing_
    comma_does_not_drop_the_whole_message` above, this one proving the
    *opposite* direction of `test_to_header_ambiguous_no_comma_address_
    is_still_safely_dropped`'s own claim doesn't silently break. That test
    proves the ambiguous, no-comma shape
    (`'bob@example.test<carol@example.test>'`) is safely dropped because
    `getaddresses(strict=True)`'s aggregate `1 + comma_count` address-count
    check doesn't match (0 commas expected 1 address, actual parse yields
    2) -- but append exactly one trailing comma to that same ambiguous
    shape and the arithmetic flips: 1 comma expects 2 addresses, the
    malformed field alone still parses into exactly 2 (the same two
    addresses as before), and `2 == 2` satisfies the check.
    `getaddresses` -- and, since this collision is resolved on the very
    first, un-retried call, `_getaddresses_resilient` too -- would accept
    `bob@example.test`/`carol@example.test` as two genuine recipients,
    each resolved into its own `pkos_nodes` person entity, from what is
    still exactly the same ambiguous, spoofing-shaped header the no-comma
    companion test proves must be rejected. `_to_header_has_grammar_
    defect` (see its own docstring) closes this via an independent,
    grammar-based cross-check `getaddresses`' own count heuristic cannot
    provide from within itself: the message is still dropped entirely,
    same as the no-comma case, not two fabricated recipients.
    """
    context, _owner_id = seeded_gmail_account
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    body = _message_body(
        message_id="msg-ambiguous-to-trailing-comma",
        thread_id="thread-ambiguous-to-trailing-comma",
        from_addr="alice@example.test",
        to_addrs=["ignored -- overridden by raw_to below"],
        internal_date_ms=now_ms,
    )
    body["payload"]["headers"] = [
        {"name": "From", "value": "alice@example.test"},
        {"name": "To", "value": "bob@example.test<carol@example.test>,"},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/gmail/v1/users/me/messages":
            return _json_response({"messages": [{"id": "msg-ambiguous-to-trailing-comma"}]})
        if request.url.path.startswith("/gmail/v1/users/me/messages/"):
            return _json_response(body)
        if request.url.path == "/gmail/v1/users/me/profile":
            return _json_response({"historyId": 1})
        raise AssertionError(f"unexpected request to {request.url}")

    adapter = GmailAdapter(transport=httpx.MockTransport(handler))
    outcome = adapter.backfill(context, "message", since=datetime.now(UTC) - timedelta(days=1))
    assert outcome.status == "succeeded"
    assert outcome.items_processed == 1
    assert _threads_and_messages(context.workspace_id) == []
    assert _resolved_person(context.workspace_id, "bob@example.test") is None
    assert _resolved_person(context.workspace_id, "carol@example.test") is None


def test_to_header_interior_double_comma_recovers_recipients_not_dropped_by_grammar_check(
    seeded_gmail_account: tuple[ConnectorAccountContext, UUID],
) -> None:
    """Round 5 review: `_to_header_has_grammar_defect` (round 4's own fix,
    above) gated the entire recipient loop on `bool(HeaderRegistry(...)
    .defects)` -- any defect at all, not specifically the
    `InvalidHeaderDefect` the round 4 collision case actually raises. A
    `To` header with a benign interior double comma or a comma-plus-
    whitespace empty entry (`"bob@example.test,,carol@example.test"` --
    real mail-merge tooling and forwarded headers produce this; Gmail
    forwards a sender's raw header verbatim) parses via `getaddresses`
    into the two real addresses plus a spurious `("", "")` entry
    `_validate_address` already drops on its own -- the exact same benign
    shape `_getaddresses_resilient`'s own trailing-comma recovery (see
    that test above) exists to handle -- but `HeaderRegistry` reports this
    shape as `ObsoleteHeaderDefect('empty element in address-list')`, a
    genuinely harmless, backward-compatibility-only defect class, never
    `InvalidHeaderDefect` (independently confirmed in a standalone
    interpreter alongside every other legitimate multi-recipient shape
    this module already accepts). `bool(.defects)` could not tell that
    apart from the genuine `InvalidHeaderDefect` the round 4 collision
    raises, so it gated the recipient loop off entirely and silently
    dropped the *whole message* -- sender, subject, thread, all of it,
    via `_process_message`'s own `if not recipients: return None` -- over
    a header that was never actually ambiguous: the identical "one comma
    away from an otherwise well-formed message vanishing with no error"
    impact `_getaddresses_resilient`'s own round 3 fix closed for a
    *different* comma placement, reintroduced here by round 4's own fix
    for a different one. Narrowing the check to `InvalidHeaderDefect`
    specifically (this round's fix) recovers both real recipients here
    without reopening the round 4 collision (see the companion test
    above, still passing unchanged).
    """
    context, _owner_id = seeded_gmail_account
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    body = _message_body(
        message_id="msg-interior-double-comma",
        thread_id="thread-interior-double-comma",
        from_addr="alice@example.test",
        to_addrs=["ignored -- overridden by raw_to below"],
        internal_date_ms=now_ms,
    )
    body["payload"]["headers"] = [
        {"name": "From", "value": "alice@example.test"},
        {"name": "To", "value": "bob@example.test,,carol@example.test"},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/gmail/v1/users/me/messages":
            return _json_response({"messages": [{"id": "msg-interior-double-comma"}]})
        if request.url.path.startswith("/gmail/v1/users/me/messages/"):
            return _json_response(body)
        raise AssertionError(f"unexpected request to {request.url}")

    adapter = GmailAdapter(transport=httpx.MockTransport(handler))
    outcome = adapter.backfill(context, "message", since=datetime.now(UTC) - timedelta(days=1))
    assert outcome.status == "succeeded"
    assert outcome.items_processed == 1

    rows = _threads_and_messages(context.workspace_id)
    assert len(rows) == 1
    assert sorted(rows[0]["recipients"]) == ["bob@example.test", "carol@example.test"]
    assert _resolved_person(context.workspace_id, "bob@example.test") is not None
    assert _resolved_person(context.workspace_id, "carol@example.test") is not None


def test_to_header_recipient_count_is_capped_to_bound_entity_resolution_work(
    seeded_gmail_account: tuple[ConnectorAccountContext, UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`email_messages.recipients` is an unbounded `ARRAY(VARCHAR(320))`
    (migration `0069`) -- nothing previously bounded how many addresses a
    single `To` header could carry, and each *distinct* one makes
    `_resolve_or_create_person` open its own independent `SessionFactory`
    transaction: a real, synchronous, per-participant round trip to
    Postgres, not merely an in-memory parse. Gmail forwards a sender's raw
    `To` header verbatim and RFC 5322 places no upper bound on how many
    addresses it may list. `_MAX_MESSAGES_PER_CALL` already bounds total
    messages fetched in one `backfill`/`incremental_sync` call, but
    nothing bounded work *within* a single message -- a single crafted
    message with an extreme recipient count could still make one call do
    an effectively unbounded amount of synchronous DB work (and open that
    many short-lived sessions) before that one message ever finishes,
    regardless of how small `_MAX_MESSAGES_PER_CALL` itself is -- found by
    review, a resource-exhaustion angle distinct from every prior
    malformed-input crash/corruption finding in this file.
    `_MAX_RECIPIENTS_PER_MESSAGE` caps it, keeping only the first N valid
    recipients (in header order) rather than rejecting the whole message
    or doing unbounded work. Monkeypatches the constant down to 3 (rather
    than sending 500+ real addresses through real Postgres transactions)
    to exercise the cap itself, matching this file's own established
    `_MAX_MESSAGES_PER_CALL` monkeypatch precedent above.
    """
    import ecc.domains.personal.gmail_adapter as gmail_adapter_module

    monkeypatch.setattr(gmail_adapter_module, "_MAX_RECIPIENTS_PER_MESSAGE", 3)

    context, _owner_id = seeded_gmail_account
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    to_addrs = [f"recipient{i}@example.test" for i in range(5)]
    body = _message_body(
        message_id="msg-recipient-flood",
        thread_id="thread-recipient-flood",
        from_addr="alice@example.test",
        to_addrs=to_addrs,
        internal_date_ms=now_ms,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/gmail/v1/users/me/messages":
            return _json_response({"messages": [{"id": "msg-recipient-flood"}]})
        if request.url.path.startswith("/gmail/v1/users/me/messages/"):
            return _json_response(body)
        raise AssertionError(f"unexpected request to {request.url}")

    adapter = GmailAdapter(transport=httpx.MockTransport(handler))
    outcome = adapter.backfill(context, "message", since=datetime.now(UTC) - timedelta(days=1))
    assert outcome.status == "succeeded"
    assert outcome.items_processed == 1

    rows = _threads_and_messages(context.workspace_id)
    assert len(rows) == 1
    # Only the first 3 (header order) recipients are kept, not all 5.
    assert rows[0]["recipients"] == to_addrs[:3]

    # The capped-out recipients must never reach entity resolution either
    # -- proving the cap actually bounds the expensive per-participant DB
    # work, not merely the stored array's own length.
    for kept in to_addrs[:3]:
        assert _resolved_person(context.workspace_id, kept) is not None
    for dropped in to_addrs[3:]:
        assert _resolved_person(context.workspace_id, dropped) is None


def _comment_nesting_bomb(address: str) -> str:
    """RFC 5322 permits arbitrarily nested `(...)` comments inside an
    address; `email.utils.parseaddr`/`getaddresses` and `email.
    headerregistry.HeaderRegistry` all share the same underlying
    `_AddressList`/`_parseaddr` grammar, which recurses one Python stack
    frame per nesting level with no depth cap of its own. 497 levels
    (independently confirmed against the installed Python's default
    recursion limit in a standalone interpreter -- the minimum that
    reliably crashes, with headroom) produces ~1KB total -- comfortably
    within Gmail API response body limits, and irrelevant to RFC 5322's
    998-byte *unfolded line* guidance either way, since this module reads
    an already-decoded JSON string field, never raw SMTP line-folded
    bytes.
    """
    depth = 497
    return "(" * depth + address + ")" * depth


def test_from_header_comment_nesting_bomb_is_skipped_not_a_crash(
    seeded_gmail_account: tuple[ConnectorAccountContext, UUID],
) -> None:
    """Round 6 review: a `From` header built entirely of deeply nested
    RFC 5322 `(...)` comments around an otherwise ordinary address
    previously reached `email.utils.parseaddr` (`_parse_address`)
    uncaught as a `RecursionError` -- not any exception type this module
    already anticipated (`ValueError`/`TypeError`/`UnicodeEncodeError`/
    Postgres `DataError`) -- aborting the entire sync call for a single
    malformed message, and for `incremental_sync` permanently wedging that
    connector account's sync since the call never reaches a `return` that
    advances `next_cursor` past it: the identical "single crafted message
    crashes the whole call" failure class `_MAX_EMAIL_ADDRESS_LENGTH`/
    `_contains_nul`/`_contains_unpaired_surrogate` already closed for their
    own failure classes. Gmail forwards a sender's raw `From` header
    verbatim and a sender fully controls their own outgoing headers. Must
    degrade to a skipped message, not a crash.
    """
    context, _owner_id = seeded_gmail_account
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    body = _message_body(
        message_id="msg-comment-bomb-from",
        thread_id="thread-comment-bomb-from",
        from_addr=_comment_nesting_bomb("a@example.test"),
        to_addrs=[_OWNER_EMAIL],
        internal_date_ms=now_ms,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/gmail/v1/users/me/messages":
            return _json_response({"messages": [{"id": "msg-comment-bomb-from"}]})
        if request.url.path.startswith("/gmail/v1/users/me/messages/"):
            return _json_response(body)
        if request.url.path == "/gmail/v1/users/me/profile":
            return _json_response({"historyId": 1})
        raise AssertionError(f"unexpected request to {request.url}")

    adapter = GmailAdapter(transport=httpx.MockTransport(handler))
    outcome = adapter.backfill(context, "message", since=datetime.now(UTC) - timedelta(days=1))
    assert outcome.status == "succeeded"
    assert outcome.items_processed == 1
    assert _threads_and_messages(context.workspace_id) == []


def test_to_header_comment_nesting_bomb_is_safely_dropped_not_a_crash(
    seeded_gmail_account: tuple[ConnectorAccountContext, UUID],
) -> None:
    """Same hazard as the `From`-header case above, reached via two
    separate call sites on the `To`-header parsing chain instead of one:
    `_to_header_has_grammar_defect` (`email.headerregistry.HeaderRegistry`,
    which runs *first* in `_process_message` and independently shares the
    same recursive grammar -- so this module's very first parse of a
    comment-nesting `To` header would have crashed here even before
    `_getaddresses_resilient` ever ran) and `_getaddresses_resilient`
    (`email.utils.getaddresses`) itself. Both now catch `RecursionError`
    and treat the header as unparseable rather than raising, the same
    "zero valid recipients -> whole message safely dropped, not a crash"
    contract `test_to_header_ambiguous_no_comma_address_is_still_safely_
    dropped` above already establishes for a different unparseable-`To`-
    header shape -- not a regression to a naive per-recipient drop this
    file has never actually implemented for a whole-header parse failure.
    """
    context, _owner_id = seeded_gmail_account
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    body = _message_body(
        message_id="msg-comment-bomb-to",
        thread_id="thread-comment-bomb-to",
        from_addr="alice@example.test",
        to_addrs=["ignored -- overridden by raw_to below"],
        internal_date_ms=now_ms,
    )
    body["payload"]["headers"] = [
        {"name": "From", "value": "alice@example.test"},
        {"name": "To", "value": _comment_nesting_bomb("bob@example.test")},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/gmail/v1/users/me/messages":
            return _json_response({"messages": [{"id": "msg-comment-bomb-to"}]})
        if request.url.path.startswith("/gmail/v1/users/me/messages/"):
            return _json_response(body)
        if request.url.path == "/gmail/v1/users/me/profile":
            return _json_response({"historyId": 1})
        raise AssertionError(f"unexpected request to {request.url}")

    adapter = GmailAdapter(transport=httpx.MockTransport(handler))
    outcome = adapter.backfill(context, "message", since=datetime.now(UTC) - timedelta(days=1))
    assert outcome.status == "succeeded"
    assert outcome.items_processed == 1
    assert _threads_and_messages(context.workspace_id) == []
    assert _resolved_person(context.workspace_id, "bob@example.test") is None


def test_to_header_unpaired_surrogate_does_not_crash_the_grammar_defect_check(
    seeded_gmail_account: tuple[ConnectorAccountContext, UUID],
) -> None:
    """Round 6 review, architecture/quality lens: `_to_header_has_grammar_
    defect` (round 4's own fix) parses the raw `To` header through `email.
    headerregistry.HeaderRegistry` *before* `_getaddresses_resilient`/
    `_validate_address` ever run (see `_process_message`'s own call site) --
    but that parse itself raises an uncaught `UnicodeEncodeError` for a
    header carrying an unpaired UTF-16 surrogate escape, the identical
    encode failure `_contains_unpaired_surrogate` exists to catch
    elsewhere in this file, reached here one call earlier and via a
    different stdlib entry point. The guard's own docstring has described
    this exact hazard and a narrow `except (UnicodeEncodeError, ...)`
    since round 4, and it is not in doubt that the guard is correct (a
    standalone interpreter reproduces the raw crash directly against
    `HeaderRegistry`) -- but until this test, no test in this file actually
    drove a surrogate-carrying `To` header through `_to_header_has_grammar_
    defect` itself: every existing surrogate test (`test_unpaired_
    surrogate_in_from_display_name_is_skipped_not_a_crash` and its
    siblings) puts the surrogate in `From`/`Subject`/`id`/`threadId`
    instead, none of which ever reach this function at all. Mutation-
    confirmed: narrowing the `except` clause down to `RecursionError` only
    (removing `UnicodeEncodeError`/`ValueError`/`TypeError`/`LookupError`/
    `IndexError`, i.e. reverting to before round 4's own fix) left every
    other test in this file passing -- this was the only test that failed,
    with the same raw, uncaught `UnicodeEncodeError` the guard exists to
    prevent. Uses the same `_surrogate_json_response` marker-substitution
    helper the `From`/`Subject`/`id` surrogate tests already establish, this
    time on the `To` header's own address portion; the lone surrogate
    recipient has no other valid recipient alongside it, so the message
    ends up with zero valid recipients and is safely dropped entirely --
    the same "can't make sense of it -> treat as zero recipients" contract
    `_validate_address`'s own docstring establishes, not a crash.
    """
    context, _owner_id = seeded_gmail_account
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    body = _message_body(
        message_id="msg-to-surrogate",
        thread_id="thread-to-surrogate",
        from_addr="alice@example.test",
        to_addrs=["ignored -- overridden by raw_to below"],
        internal_date_ms=now_ms,
    )
    body["payload"]["headers"] = [
        {"name": "From", "value": "alice@example.test"},
        {"name": "To", "value": "bob__SURROGATE__@example.test"},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/gmail/v1/users/me/messages":
            return _json_response({"messages": [{"id": "msg-to-surrogate"}]})
        if request.url.path.startswith("/gmail/v1/users/me/messages/"):
            return _surrogate_json_response(body, marker_replacement=r"\ud800")
        if request.url.path == "/gmail/v1/users/me/profile":
            return _json_response({"historyId": 1})
        raise AssertionError(f"unexpected request to {request.url}")

    adapter = GmailAdapter(transport=httpx.MockTransport(handler))
    outcome = adapter.backfill(context, "message", since=datetime.now(UTC) - timedelta(days=1))
    assert outcome.status == "succeeded"
    assert outcome.items_processed == 1
    assert _threads_and_messages(context.workspace_id) == []


def test_to_header_group_syntax_collision_does_not_crash_the_grammar_defect_check(
    seeded_gmail_account: tuple[ConnectorAccountContext, UUID],
) -> None:
    """Round 7 review: RFC 5322's `group` syntax (`"name: member@x.test;"`)
    is grammar `HeaderRegistry` otherwise parses cleanly, but a `To` header
    made of two such groups -- or a group immediately followed by a bare
    address -- with no comma between them (invalid grammar `HeaderRegistry`'s
    own legacy fallback still attempts to recover from) makes that recovery
    path try to read `.local_part` off a `Group` object, which doesn't have
    one, raising an uncaught `AttributeError: 'Group' object has no
    attribute 'local_part'` from inside `HeaderRegistry.__call__` itself --
    independently confirmed in a standalone interpreter, reproducible with
    as little as `"g:a@x.test;b@x.test"`. None of the six exception types
    `_to_header_has_grammar_defect`'s `except` clause already caught
    (`UnicodeEncodeError`/`ValueError`/`TypeError`/`LookupError`/
    `IndexError`/`RecursionError`) are raised for this input -- the
    identical "single crafted message reaches an uncaught crash, aborting
    the entire sync call, and for `incremental_sync` wedging that account
    permanently since the cursor never advances past it" failure class
    every other guard in this file exists to close, reached here via a
    stdlib construction-time `AttributeError` instead. Gmail forwards a
    sender's raw `To` header verbatim and a sender fully controls their own
    outgoing headers, including this syntax. `email.utils.getaddresses`
    (`_getaddresses_resilient`'s own underlying call) handles this same
    input without raising, returning its `("", "")` sentinel -- so with
    `AttributeError` added to the caught tuple, this message ends up with
    zero valid recipients and is safely dropped entirely, the same "can't
    make sense of it -> treat as zero recipients" contract every other
    unparseable-`To`-header test in this file already establishes, not a
    crash.
    """
    context, _owner_id = seeded_gmail_account
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    body = _message_body(
        message_id="msg-group-syntax-to",
        thread_id="thread-group-syntax-to",
        from_addr="alice@example.test",
        to_addrs=["ignored -- overridden by raw_to below"],
        internal_date_ms=now_ms,
    )
    body["payload"]["headers"] = [
        {"name": "From", "value": "alice@example.test"},
        {"name": "To", "value": "g:bob@example.test;carol@example.test"},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/gmail/v1/users/me/messages":
            return _json_response({"messages": [{"id": "msg-group-syntax-to"}]})
        if request.url.path.startswith("/gmail/v1/users/me/messages/"):
            return _json_response(body)
        if request.url.path == "/gmail/v1/users/me/profile":
            return _json_response({"historyId": 1})
        raise AssertionError(f"unexpected request to {request.url}")

    adapter = GmailAdapter(transport=httpx.MockTransport(handler))
    outcome = adapter.backfill(context, "message", since=datetime.now(UTC) - timedelta(days=1))
    assert outcome.status == "succeeded"
    assert outcome.items_processed == 1
    assert _threads_and_messages(context.workspace_id) == []
    assert _resolved_person(context.workspace_id, "bob@example.test") is None
    assert _resolved_person(context.workspace_id, "carol@example.test") is None


def test_to_header_exceeding_length_limit_is_dropped_not_a_cpu_stall(
    seeded_gmail_account: tuple[ConnectorAccountContext, UUID],
) -> None:
    """Round 7 review: `_to_header_has_grammar_defect`'s `HeaderRegistry`
    cross-check is a from-scratch recursive-descent parser that reslices
    its remaining input on every token consumed -- independently profiled
    (`cProfile` against `HeaderRegistry()("To", ...)` directly) to cost
    roughly *quadratic* CPU time in the number of comma-separated fields in
    the header, not merely the `RecursionError` stack-depth hazard round 6
    already closed for deeply *nested* comment syntax: a 2,000-address `To`
    header costs ~0.1s of pure CPU in a single call, an 8,000-address one
    ~2.4s, a 16,000-address one ~10s -- no crash, no exception, nothing
    downstream (including `_MAX_RECIPIENTS_PER_MESSAGE`'s own cap, applied
    only *after* this call already returned) bounds it. `_MAX_TO_HEADER_
    LENGTH` rejects a header whose raw length already exceeds what any
    genuine `_MAX_RECIPIENTS_PER_MESSAGE`-sized message could plausibly
    need, via one cheap `len(...)` comparison, before either expensive
    parser ever runs on it. This builds a `To` header of ~3,000 short,
    individually well-formed addresses (comfortably over `_MAX_TO_HEADER_
    LENGTH`'s 50,000-character bound, and, not incidentally, also over
    `_MAX_RECIPIENTS_PER_MESSAGE` itself) -- with the guard, this message is
    safely dropped (zero recipients, same "can't make sense of it" contract
    every other unparseable-`To`-header test in this file establishes)
    rather than the call spending unbounded CPU time parsing it. This test
    completing at all inside pytest's own default timeout is itself part of
    what it demonstrates.
    """
    context, _owner_id = seeded_gmail_account
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    huge_to_addrs = [f"recipient{i}@example.test" for i in range(3000)]
    body = _message_body(
        message_id="msg-oversized-to",
        thread_id="thread-oversized-to",
        from_addr="alice@example.test",
        to_addrs=huge_to_addrs,
        internal_date_ms=now_ms,
    )
    raw_to_header = ", ".join(huge_to_addrs)
    assert len(raw_to_header) > 50_000

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/gmail/v1/users/me/messages":
            return _json_response({"messages": [{"id": "msg-oversized-to"}]})
        if request.url.path.startswith("/gmail/v1/users/me/messages/"):
            return _json_response(body)
        if request.url.path == "/gmail/v1/users/me/profile":
            return _json_response({"historyId": 1})
        raise AssertionError(f"unexpected request to {request.url}")

    adapter = GmailAdapter(transport=httpx.MockTransport(handler))
    outcome = adapter.backfill(context, "message", since=datetime.now(UTC) - timedelta(days=1))
    assert outcome.status == "succeeded"
    assert outcome.items_processed == 1
    assert _threads_and_messages(context.workspace_id) == []
    assert _resolved_person(context.workspace_id, "recipient0@example.test") is None


# --- per-call message budget hit mid-page -------------------------------------


def test_backfill_reports_partial_not_succeeded_when_budget_exhausted_mid_page(
    seeded_gmail_account: tuple[ConnectorAccountContext, UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 1 review: `_MAX_MESSAGES_PER_CALL` bounds total `messages.get`
    calls across every page fetched in one call -- but the per-page `for`
    loop that enforces it previously fell straight through to the
    `nextPageToken` check once the budget ran out, rather than returning
    early. When the bound-hitting page also happened to be Gmail's own
    *last* page (no `nextPageToken`), that meant this method reported
    `status="succeeded"` -- and handed `incremental_sync` a `next_cursor`
    implying nothing older remained -- despite some of that very page's
    own `messages` never having been fetched or written at all: silent
    data loss reported as success. This monkeypatches `_MAX_MESSAGES_PER_
    CALL` down to 3 against a single five-message page (no `nextPageToken`
    at all) to exercise exactly that shape without needing 200+ messages
    in a test fixture. Mutation-confirmed: reverting `gmail_adapter.py`'s
    `budget_exhausted` fix reproduces `status="succeeded"` with only 3 of
    5 messages ever written -- silently incomplete, not merely capped.
    """
    import ecc.domains.personal.gmail_adapter as gmail_adapter_module

    monkeypatch.setattr(gmail_adapter_module, "_MAX_MESSAGES_PER_CALL", 3)

    context, _owner_id = seeded_gmail_account
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    message_ids = [f"msg-{i}" for i in range(5)]
    bodies = {
        message_id: _message_body(
            message_id=message_id,
            thread_id=f"thread-{message_id}",
            from_addr="alice@example.test",
            to_addrs=[_OWNER_EMAIL],
            internal_date_ms=now_ms + i,
        )
        for i, message_id in enumerate(message_ids)
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/gmail/v1/users/me/messages":
            # A single page, no `nextPageToken` -- this is Gmail's own
            # last (and only) page for this query, with more refs than
            # the (monkeypatched) budget allows.
            return _json_response({"messages": [{"id": m} for m in message_ids]})
        if request.url.path.startswith("/gmail/v1/users/me/messages/"):
            message_id = request.url.path.rsplit("/", 1)[-1]
            return _json_response(bodies[message_id])
        raise AssertionError(f"unexpected request to {request.url}")

    adapter = GmailAdapter(transport=httpx.MockTransport(handler))
    outcome = adapter.backfill(context, "message", since=datetime.now(UTC) - timedelta(days=1))

    assert outcome.status == "partial"
    assert outcome.items_processed == 3
    assert outcome.next_cursor is None
    assert outcome.error_summary is not None and "per-call bound" in outcome.error_summary

    rows = _threads_and_messages(context.workspace_id)
    assert len(rows) == 3


def test_incremental_sync_reports_partial_not_succeeded_when_budget_exhausted_mid_page(
    seeded_gmail_account: tuple[ConnectorAccountContext, UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same bug, same fix, in `_sync_history`'s identical loop shape --
    see `test_backfill_reports_partial_not_succeeded_when_budget_exhausted_
    mid_page`'s own docstring for the full mechanism.
    """
    import ecc.domains.personal.gmail_adapter as gmail_adapter_module

    monkeypatch.setattr(gmail_adapter_module, "_MAX_MESSAGES_PER_CALL", 2)

    context, _owner_id = seeded_gmail_account
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    message_ids = [f"hist-{i}" for i in range(4)]
    bodies = {
        message_id: _message_body(
            message_id=message_id,
            thread_id=f"thread-{message_id}",
            from_addr="carol@example.test",
            to_addrs=[_OWNER_EMAIL],
            internal_date_ms=now_ms + i,
        )
        for i, message_id in enumerate(message_ids)
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/gmail/v1/users/me/history":
            return _json_response(
                {
                    "history": [{"messagesAdded": [{"message": {"id": m}}]} for m in message_ids],
                    "historyId": 999,
                }
            )
        if request.url.path.startswith("/gmail/v1/users/me/messages/"):
            message_id = request.url.path.rsplit("/", 1)[-1]
            return _json_response(bodies[message_id])
        raise AssertionError(f"unexpected request to {request.url}")

    adapter = GmailAdapter(transport=httpx.MockTransport(handler))
    outcome = adapter.incremental_sync(context, "message", "100")

    assert outcome.status == "partial"
    assert outcome.items_processed == 2
    assert outcome.next_cursor == "100"
    assert outcome.error_summary is not None and "per-call bound" in outcome.error_summary

    rows = _threads_and_messages(context.workspace_id)
    assert len(rows) == 2


# --- pagination across multiple messages.list/history.list pages -------------


def test_backfill_paginates_across_multiple_messages_list_pages(
    seeded_gmail_account: tuple[ConnectorAccountContext, UUID],
) -> None:
    """Every other `backfill` test in this file exercises a single
    `messages.list` page (`nextPageToken` absent) -- this is the only test
    proving the `while`/`pageToken` loop that walks a real multi-page
    `messages.list` response actually advances page-to-page rather than
    only ever having been exercised with one page.
    """
    context, _owner_id = seeded_gmail_account
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    body_1 = _message_body(
        message_id="page1-msg",
        thread_id="thread-page1",
        from_addr="alice@example.test",
        to_addrs=[_OWNER_EMAIL],
        internal_date_ms=now_ms,
        history_id=201,
    )
    body_2 = _message_body(
        message_id="page2-msg",
        thread_id="thread-page2",
        from_addr="bob@example.test",
        to_addrs=[_OWNER_EMAIL],
        internal_date_ms=now_ms + 1000,
        history_id=202,
    )
    seen_page_tokens: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/gmail/v1/users/me/messages":
            page_token = request.url.params.get("pageToken")
            seen_page_tokens.append(page_token)
            if page_token is None:
                return _json_response(
                    {"messages": [{"id": "page1-msg"}], "nextPageToken": "page-2"}
                )
            assert page_token == "page-2"
            return _json_response({"messages": [{"id": "page2-msg"}]})
        if request.url.path == "/gmail/v1/users/me/messages/page1-msg":
            return _json_response(body_1)
        if request.url.path == "/gmail/v1/users/me/messages/page2-msg":
            return _json_response(body_2)
        raise AssertionError(f"unexpected request to {request.url}")

    adapter = GmailAdapter(transport=httpx.MockTransport(handler))
    outcome = adapter.backfill(context, "message", since=datetime.now(UTC) - timedelta(days=1))

    assert outcome.status == "succeeded"
    assert outcome.items_processed == 2
    assert outcome.next_cursor == "202"
    assert seen_page_tokens == [None, "page-2"]

    rows = _threads_and_messages(context.workspace_id)
    assert {row["external_message_id"] for row in rows} == {"page1-msg", "page2-msg"}


def test_incremental_sync_paginates_across_multiple_history_list_pages(
    seeded_gmail_account: tuple[ConnectorAccountContext, UUID],
) -> None:
    """Same coverage gap as the `backfill` test above, for `_sync_history`'s
    own `history.list` pagination loop.
    """
    context, _owner_id = seeded_gmail_account
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    body = _message_body(
        message_id="hist-page2-msg",
        thread_id="thread-hist-page2",
        from_addr="dave@example.test",
        to_addrs=[_OWNER_EMAIL],
        internal_date_ms=now_ms,
    )
    seen_page_tokens: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/gmail/v1/users/me/history":
            page_token = request.url.params.get("pageToken")
            seen_page_tokens.append(page_token)
            if page_token is None:
                return _json_response(
                    {"history": [], "historyId": 300, "nextPageToken": "hist-page-2"}
                )
            assert page_token == "hist-page-2"
            return _json_response(
                {
                    "history": [{"messagesAdded": [{"message": {"id": "hist-page2-msg"}}]}],
                    "historyId": 310,
                }
            )
        if request.url.path == "/gmail/v1/users/me/messages/hist-page2-msg":
            return _json_response(body)
        raise AssertionError(f"unexpected request to {request.url}")

    adapter = GmailAdapter(transport=httpx.MockTransport(handler))
    outcome = adapter.incremental_sync(context, "message", "100")

    assert outcome.status == "succeeded"
    assert outcome.items_processed == 1
    assert outcome.next_cursor == "310"
    assert seen_page_tokens == [None, "hist-page-2"]

    rows = _threads_and_messages(context.workspace_id)
    assert rows[0]["external_message_id"] == "hist-page2-msg"


def test_incremental_sync_makes_forward_progress_across_repeated_budget_exhausted_calls(
    seeded_gmail_account: tuple[ConnectorAccountContext, UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 12 review: `_sync_history`'s own budget-exhausted `partial`
    returns previously reported `next_cursor=start_history_id`
    unconditionally -- the cursor the call *started* from, never advanced,
    regardless of how many `history[]` records this call had already fully
    processed before the budget ran out. Since `history.list(startHistoryId=
    X)` deterministically returns the identical records in the identical
    order for the identical `X` (a stronger ordering guarantee than
    `messages.list`'s own, which is exactly why `_sync_messages`' sibling
    fix -- round 11 -- had to fall back to `next_cursor=None` instead), a
    subsequent `incremental_sync` call resuming from that same, never-
    advanced cursor re-walked `history.list` from page 1 and re-hit the
    identical budget boundary on the identical records every single time:
    every message past position `_MAX_MESSAGES_PER_CALL` (in this cursor's
    own ascending-`historyId` ordering) was silently, permanently
    unreachable, no matter how many times `incremental_sync` was retried
    with the cursor it kept handing back -- a livelock, not merely delayed
    progress. Reproduced directly against real Postgres (not merely
    reasoned about) before this fix: four history records behind one
    `historyId=100` cursor, budget monkeypatched to 2 -- five consecutive
    `incremental_sync("100")` calls each processed only the same first two
    records (`hist-0`/`hist-1`) and reported `next_cursor="100"` every
    time; `hist-2`/`hist-3` were never written by any of them. Fixed by
    tracking the historyId of the last *fully processed* history record
    (`resumable_history_id`) and resuming from it instead of `start_
    history_id` on every partial-return site in this method -- this test
    proves two repeated calls, each hitting the same monkeypatched budget,
    together make genuine forward progress and eventually cover every
    message, unlike the identical two calls before this fix (which would
    have processed `hist-0`/`hist-1` twice over and never reached `hist-2`/
    `hist-3` at all).
    """
    import ecc.domains.personal.gmail_adapter as gmail_adapter_module

    monkeypatch.setattr(gmail_adapter_module, "_MAX_MESSAGES_PER_CALL", 2)

    context, _owner_id = seeded_gmail_account
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    message_ids = [f"hist-{i}" for i in range(4)]
    bodies = {
        message_id: _message_body(
            message_id=message_id,
            thread_id=f"thread-{message_id}",
            from_addr="carol@example.test",
            to_addrs=[_OWNER_EMAIL],
            internal_date_ms=now_ms + i,
        )
        for i, message_id in enumerate(message_ids)
    }
    # One record per message, strictly ascending `id` -- the ordering
    # `history.list` itself guarantees and this fix's own correctness
    # depends on (see `resumable_history_id`'s own comment in
    # `gmail_adapter.py`).
    history_records = [
        {"id": 101 + i, "messagesAdded": [{"message": {"id": m}}]}
        for i, m in enumerate(message_ids)
    ]
    seen_start_history_ids: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/gmail/v1/users/me/history":
            start = int(request.url.params["startHistoryId"])
            seen_start_history_ids.append(start)
            # Real Gmail semantics: only records with `id` strictly greater
            # than `startHistoryId` are ever returned -- a mock that
            # (incorrectly) always returned every record regardless of
            # `startHistoryId` would not actually exercise whether
            # `next_cursor` makes genuine forward progress across repeated
            # calls, which is exactly what this test needs to prove.
            visible = [r for r in history_records if r["id"] > start]
            return _json_response({"history": visible, "historyId": 999})
        if request.url.path.startswith("/gmail/v1/users/me/messages/"):
            message_id = request.url.path.rsplit("/", 1)[-1]
            return _json_response(bodies[message_id])
        raise AssertionError(f"unexpected request to {request.url}")

    adapter = GmailAdapter(transport=httpx.MockTransport(handler))

    outcome_1 = adapter.incremental_sync(context, "message", "100")
    assert outcome_1.status == "partial"
    assert outcome_1.items_processed == 2
    # Not "100" (the old, buggy, unconditional-pin behavior) -- advanced to
    # `hist-1`'s own record id, the last one fully processed before the
    # budget ran out.
    assert outcome_1.next_cursor == "102"

    outcome_2 = adapter.incremental_sync(context, "message", outcome_1.next_cursor)
    assert outcome_2.status == "succeeded"
    assert outcome_2.items_processed == 2
    assert outcome_2.next_cursor == "999"

    # The two calls' own `startHistoryId` values prove the second call
    # actually resumed past the first's progress rather than re-listing
    # from scratch.
    assert seen_start_history_ids == [100, 102]

    rows = _threads_and_messages(context.workspace_id)
    assert {row["external_message_id"] for row in rows} == set(message_ids)


def test_incremental_sync_makes_forward_progress_through_a_single_oversized_history_record(
    seeded_gmail_account: tuple[ConnectorAccountContext, UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 13 review: round 12's own `resumable_history_id` fix (see the
    test immediately above) resumes at `history[]` *record* granularity --
    it closes the livelock for *many small records* spanning more than one
    budget's worth of messages, but does nothing for a *single* record
    whose own `messagesAdded` list by itself exceeds `_MAX_MESSAGES_PER_
    CALL`: `history.list(startHistoryId=X)` has no way to return that one
    record partially, only ever whole again for the identical `X`, so
    before this fix every retry re-walked it from its own first message
    and re-hit the identical budget boundary every single time --
    `resumable_history_id` never advances past it (it never *finishes*),
    so `next_cursor` never changes either. Reproduced directly against
    real Postgres before this fix: one history record with five
    `messagesAdded` entries, budget monkeypatched to 2 -- six consecutive
    `incremental_sync("100")` calls each processed only the same first two
    messages (`big-0`/`big-1`) and reported `next_cursor="100"` every
    time; `big-2`/`big-3`/`big-4` were never written by any of them.

    Fixed by `_parse_history_cursor`/`_build_history_cursor`: a compound
    `next_cursor` (`"{list_start_history_id}:{record_id}:{skip_count}"`)
    carries "and skip the first N messages of this specific record" across
    calls, on top of the existing `resumable_history_id` mechanism -- this
    test proves three successive calls, each hitting the same
    monkeypatched budget, together cover every message in the oversized
    record without ever re-fetching one already written by an earlier
    call (`fetch_counts` below asserts each message is fetched exactly
    once total, not merely that all five eventually land in Postgres).
    """
    import ecc.domains.personal.gmail_adapter as gmail_adapter_module

    monkeypatch.setattr(gmail_adapter_module, "_MAX_MESSAGES_PER_CALL", 2)

    context, _owner_id = seeded_gmail_account
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    message_ids = [f"big-{i}" for i in range(5)]
    bodies = {
        message_id: _message_body(
            message_id=message_id,
            thread_id=f"thread-{message_id}",
            from_addr="carol@example.test",
            to_addrs=[_OWNER_EMAIL],
            internal_date_ms=now_ms + i,
        )
        for i, message_id in enumerate(message_ids)
    }
    # ONE record, with all five messages -- more than the (monkeypatched)
    # budget of 2, unlike the many-small-records shape the test above
    # covers.
    history_records = [
        {"id": 101, "messagesAdded": [{"message": {"id": m}} for m in message_ids]},
    ]
    seen_start_history_ids: list[int] = []
    fetch_counts: dict[str, int] = {message_id: 0 for message_id in message_ids}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/gmail/v1/users/me/history":
            start = int(request.url.params["startHistoryId"])
            seen_start_history_ids.append(start)
            visible = [r for r in history_records if r["id"] > start]
            return _json_response({"history": visible, "historyId": 999})
        if request.url.path.startswith("/gmail/v1/users/me/messages/"):
            message_id = request.url.path.rsplit("/", 1)[-1]
            fetch_counts[message_id] += 1
            return _json_response(bodies[message_id])
        raise AssertionError(f"unexpected request to {request.url}")

    adapter = GmailAdapter(transport=httpx.MockTransport(handler))

    outcome_1 = adapter.incremental_sync(context, "message", "100")
    assert outcome_1.status == "partial"
    assert outcome_1.items_processed == 2
    # Not "100" (the pre-fix, never-advancing behavior) -- a compound
    # cursor recording "record 101, 2 of its own messages already done."
    assert outcome_1.next_cursor == "100:101:2"

    outcome_2 = adapter.incremental_sync(context, "message", outcome_1.next_cursor)
    assert outcome_2.status == "partial"
    assert outcome_2.items_processed == 2
    assert outcome_2.next_cursor == "100:101:4"

    outcome_3 = adapter.incremental_sync(context, "message", outcome_2.next_cursor)
    assert outcome_3.status == "succeeded"
    assert outcome_3.items_processed == 1
    assert outcome_3.next_cursor == "999"

    # Every `history.list` call resumes from the same `startHistoryId=100`
    # (the record itself, `id=101`, is only ever returned by asking for
    # everything *after* 100) -- proving the compound cursor's own
    # `list_start_history_id` component round-trips correctly across all
    # three calls, not just its skip-count component.
    assert seen_start_history_ids == [100, 100, 100]
    # Each message is fetched exactly once, total, across all three calls
    # -- the skip mechanism actually avoids re-fetching, not merely
    # re-fetching-but-idempotently-no-op-ing on the DB write.
    assert fetch_counts == {message_id: 1 for message_id in message_ids}

    rows = _threads_and_messages(context.workspace_id)
    assert {row["external_message_id"] for row in rows} == set(message_ids)


# --- next_cursor's own `0`-vs-`None` boundary --------------------------------
#
# Round 16 review: `_sync_messages`'s/`_sync_history`'s "fully caught up"
# success returns each computed `next_cursor` via a bare truthiness check
# (`if highest_history_id else None` / `if latest_history_id else list_
# start_history_id`) rather than `is not None`, unlike every other read of
# either variable in the same two methods. `_coerce_int` -- what both
# variables are ultimately assigned from, whether from a message's own
# `historyId` field, the `users.getProfile` fallback, or `history.list`'s
# own page-level `historyId` field -- can return `0`, and `0` is falsy in
# Python. Before this fix, a `historyId` of exactly `0` was silently
# treated identically to "no historyId was ever observed at all": `_sync_
# messages` would discard a real cursor value (`next_cursor=None` on an
# otherwise genuinely `"succeeded"`, fully-caught-up sync) and `_sync_
# history` would fall back to the *stale* starting cursor it was given
# instead of the fresh value Gmail actually returned. Real Gmail `historyId`
# values are not documented as ever being exactly `0` in practice, so this
# is latent rather than a live production bug -- the same "confirmed
# harmless in practice today, but a real, mutation-testable contract
# violation" framing round 14 review used for `_parse_history_cursor`'s own
# `record_id < 0` gap -- but nothing in this adapter validates that
# assumption on the read side, and every sibling check on these same two
# variables elsewhere in the same methods (e.g. `_sync_messages`'s own `if
# highest_history_id is None:` a few lines above its own fix site) already
# gets this right; this was the one remaining truthiness-shortcut. Fixed by
# switching both sites to `is not None`.


def test_backfill_reports_next_cursor_zero_not_none_when_history_id_is_zero(
    seeded_gmail_account: tuple[ConnectorAccountContext, UUID],
) -> None:
    """`_sync_messages`'s own "fully caught up" success branch -- a single
    message whose own `historyId` is exactly `0` (never realistic for a
    real Gmail account, but nothing on the read side rules it out) must
    still be handed back as `next_cursor="0"`, not discarded as if no
    `historyId` had ever been observed. Mutation-confirmed: reverting the
    `is not None` fix back to a bare truthiness check makes this fail with
    `next_cursor is None` instead of `"0"`.
    """
    context, _owner_id = seeded_gmail_account
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    body = _message_body(
        message_id="zero-history-msg",
        thread_id="thread-zero-history",
        from_addr="zero@example.test",
        to_addrs=[_OWNER_EMAIL],
        internal_date_ms=now_ms,
        history_id=0,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/gmail/v1/users/me/messages":
            return _json_response({"messages": [{"id": "zero-history-msg"}]})
        if request.url.path == "/gmail/v1/users/me/messages/zero-history-msg":
            return _json_response(body)
        raise AssertionError(f"unexpected request to {request.url}")

    adapter = GmailAdapter(transport=httpx.MockTransport(handler))
    outcome = adapter.backfill(context, "message", since=datetime.now(UTC) - timedelta(days=1))
    assert outcome.status == "succeeded"
    assert outcome.items_processed == 1
    assert outcome.next_cursor == "0"


def test_incremental_sync_reports_next_cursor_zero_not_the_stale_start_when_history_id_is_zero(
    seeded_gmail_account: tuple[ConnectorAccountContext, UUID],
) -> None:
    """`_sync_history`'s own "fully caught up" success branch -- Gmail's
    own page-level `historyId` field reported as exactly `0` must still be
    handed back as `next_cursor="0"`, not silently replaced with the stale
    `list_start_history_id` this call started from. Mutation-confirmed:
    reverting the `is not None` fix back to a bare truthiness check makes
    this fail with `next_cursor == "100"` (the stale starting cursor)
    instead of `"0"`.
    """
    context, _owner_id = seeded_gmail_account

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/gmail/v1/users/me/history":
            return _json_response({"history": [], "historyId": 0})
        raise AssertionError(f"unexpected request to {request.url}")

    adapter = GmailAdapter(transport=httpx.MockTransport(handler))
    outcome = adapter.incremental_sync(context, "message", "100")
    assert outcome.status == "succeeded"
    assert outcome.items_processed == 0
    assert outcome.next_cursor == "0"


# --- `_GmailHistoryCursor.from_str`: malformed compound state degrades safely -
#
# Round 14 review: `_parse_history_cursor`'s own docstring -- and its
# module-level comment above the function -- had never had a single direct
# unit test of its own, unlike this file's established "call the guard
# function directly, no HTTP mocking needed" pattern already used below for
# `_process_message`. The full-`_sync_history` integration test above
# (`test_incremental_sync_makes_forward_progress_through_a_single_oversized_
# history_record`) only ever exercises the well-formed compound cursor
# `_GmailHistoryCursor` itself produces -- never a malformed one, since
# nothing in that test's own mocked Gmail responses can produce one. These
# tests close that gap and, in doing so, found the docstring's own claim
# ("a non-integer/negative record id or skip count" degrades to the plain-
# cursor fallback) was only half true: `skip_count < 0` was checked, but the
# symmetric `record_id < 0` case was not -- `_GmailHistoryCursor.from_str(
# "100:-5:3")` returned a live `(list_start_history_id="100", stuck_record_
# id=-5, stuck_offset=3)` as if it were a well-formed compound cursor, not
# the documented degrade-to-plain fallback. Harmless in practice today (a
# real Gmail `history[]` record `id`, parsed via `_coerce_int` the same way
# this method's own `record_id` is, is never negative, so a negative
# `pending_stuck_record_id` could only ever originate from corrupted
# persisted state and would then simply never match any real record --
# equivalent in effect, not in the `list_start_history_id` value handed to
# `history.list`, to the documented fallback), but a real, mutation-testable
# gap between this method's own documented contract and what it actually
# did -- found by round 14 review. Fixed by adding the missing `record_id <
# 0` check, symmetric with the pre-existing `skip_count < 0` one. Later
# collapsed from two free functions (`_parse_history_cursor`/`_build_
# history_cursor`) into this one owned type -- see `_GmailHistoryCursor`'s
# own module-level comment for why.


def _cursor_fields(cursor: _GmailHistoryCursor) -> tuple[str, int | None, int]:
    return cursor.list_start_history_id, cursor.stuck_record_id, cursor.stuck_offset


def test_history_cursor_from_str_returns_the_plain_no_skip_case_for_a_bare_historyid() -> None:
    assert _cursor_fields(_GmailHistoryCursor.from_str("12345")) == ("12345", None, 0)


def test_history_cursor_from_str_parses_a_well_formed_compound_cursor() -> None:
    assert _cursor_fields(_GmailHistoryCursor.from_str("100:101:2")) == ("100", 101, 2)


def test_history_cursor_from_str_degrades_wrong_field_count_to_a_plain_cursor() -> None:
    assert _cursor_fields(_GmailHistoryCursor.from_str("100:101")) == ("100:101", None, 0)
    assert _cursor_fields(_GmailHistoryCursor.from_str("100:101:2:3")) == ("100:101:2:3", None, 0)


def test_history_cursor_from_str_degrades_a_non_integer_record_id_to_a_plain_cursor() -> None:
    assert _cursor_fields(_GmailHistoryCursor.from_str("100:abc:2")) == ("100:abc:2", None, 0)


def test_history_cursor_from_str_degrades_a_non_integer_skip_count_to_a_plain_cursor() -> None:
    assert _cursor_fields(_GmailHistoryCursor.from_str("100:101:xyz")) == ("100:101:xyz", None, 0)


def test_history_cursor_from_str_degrades_a_negative_skip_count_to_a_plain_cursor() -> None:
    assert _cursor_fields(_GmailHistoryCursor.from_str("100:101:-2")) == ("100:101:-2", None, 0)


def test_history_cursor_from_str_degrades_a_negative_record_id_to_a_plain_cursor() -> None:
    """Round 14 review: previously returned `("100", -5, 3)` -- a live
    (if never Gmail-reachable) `record_id`/`skip_count` pair, not the
    documented plain-cursor fallback -- see this section's own module-level
    comment for the full mechanism and why this is safe-in-practice but a
    real contract violation. Mutation-confirmed: reverting the fix (dropping
    `record_id < 0` from `_GmailHistoryCursor.from_str`'s own guard) makes
    this test fail with `("100", -5, 3)` instead of the expected degraded
    tuple.
    """
    assert _cursor_fields(_GmailHistoryCursor.from_str("100:-5:3")) == ("100:-5:3", None, 0)


def test_history_cursor_round_trips_through_str_for_every_parseable_shape() -> None:
    """`str(_GmailHistoryCursor.from_str(s)) == s` for every shape `_sync_
    history` itself ever persists and replays -- a bare historyId, a well-
    formed compound cursor with a positive skip offset, and a degraded
    (malformed) string echoed back unchanged. The one shape that does NOT
    round-trip -- a well-formed compound cursor whose own skip offset is
    exactly `0` -- is never produced by `with_progress` (see `__str__`'s own
    docstring) and is handled at its one call site (`_sync_history`'s
    `history_response is None` branch) by echoing `start_history_id`
    verbatim instead of going through `str(cursor)`, not by this type.
    """
    for cursor_str in ("12345", "100:101:2", "100:101", "100:abc:2", "100:101:xyz"):
        assert str(_GmailHistoryCursor.from_str(cursor_str)) == cursor_str


def test_history_cursor_with_resumed_through_clears_any_stuck_point() -> None:
    cursor = _GmailHistoryCursor.from_str("100:101:2").with_resumed_through(101)
    assert _cursor_fields(cursor) == ("100", None, 0)
    assert str(cursor) == "101"


def test_history_cursor_with_progress_keeps_the_prior_resumable_history_id() -> None:
    cursor = _GmailHistoryCursor.from_str("100").with_resumed_through(101).with_progress(102, 3)
    assert cursor.resumable_history_id == 101
    assert _cursor_fields(cursor) == ("100", 102, 3)
    assert str(cursor) == "101:102:3"


# --- _process_message guard clauses: skip gracefully, never crash ------------
#
# `_process_message`'s own docstring: "A malformed/incomplete response
# shape is skipped ... rather than raising." Every branch below was
# previously only exercised indirectly (or not at all) through the
# happy-path tests above -- these call `_process_message` directly (no
# HTTP mocking needed, it takes an already-parsed response body) to prove
# each guard clause actually returns `None`/skips rather than raising,
# per this file's own review-round-1 coverage sweep.


def test_process_message_skips_when_thread_id_missing(
    seeded_gmail_account: tuple[ConnectorAccountContext, UUID],
) -> None:
    context, owner_id = seeded_gmail_account
    adapter = GmailAdapter()
    body = _message_body(
        message_id="msg-no-thread",
        thread_id="irrelevant",
        from_addr="alice@example.test",
        to_addrs=[_OWNER_EMAIL],
        internal_date_ms=int(datetime.now(UTC).timestamp() * 1000),
    )
    del body["threadId"]

    result = adapter._process_message(
        body,
        workspace_id=context.workspace_id,
        owner_id=owner_id,
        connector_account_id=context.connector_account_id,
        now=datetime.now(UTC),
    )
    assert result is None
    assert _threads_and_messages(context.workspace_id) == []


def test_process_message_skips_when_from_header_missing(
    seeded_gmail_account: tuple[ConnectorAccountContext, UUID],
) -> None:
    context, owner_id = seeded_gmail_account
    adapter = GmailAdapter()
    body = _message_body(
        message_id="msg-no-from",
        thread_id="thread-no-from",
        from_addr="irrelevant@example.test",
        to_addrs=[_OWNER_EMAIL],
        internal_date_ms=int(datetime.now(UTC).timestamp() * 1000),
    )
    body["payload"]["headers"] = [h for h in body["payload"]["headers"] if h["name"] != "From"]

    result = adapter._process_message(
        body,
        workspace_id=context.workspace_id,
        owner_id=owner_id,
        connector_account_id=context.connector_account_id,
        now=datetime.now(UTC),
    )
    assert result is None
    assert _threads_and_messages(context.workspace_id) == []


def test_process_message_skips_when_no_recipients(
    seeded_gmail_account: tuple[ConnectorAccountContext, UUID],
) -> None:
    context, owner_id = seeded_gmail_account
    adapter = GmailAdapter()
    body = _message_body(
        message_id="msg-no-to",
        thread_id="thread-no-to",
        from_addr="alice@example.test",
        to_addrs=["irrelevant@example.test"],
        internal_date_ms=int(datetime.now(UTC).timestamp() * 1000),
    )
    body["payload"]["headers"] = [h for h in body["payload"]["headers"] if h["name"] != "To"]

    result = adapter._process_message(
        body,
        workspace_id=context.workspace_id,
        owner_id=owner_id,
        connector_account_id=context.connector_account_id,
        now=datetime.now(UTC),
    )
    assert result is None
    assert _threads_and_messages(context.workspace_id) == []


def test_process_message_skips_when_internal_date_is_non_numeric(
    seeded_gmail_account: tuple[ConnectorAccountContext, UUID],
) -> None:
    context, owner_id = seeded_gmail_account
    adapter = GmailAdapter()
    body = _message_body(
        message_id="msg-bad-date",
        thread_id="thread-bad-date",
        from_addr="alice@example.test",
        to_addrs=[_OWNER_EMAIL],
        internal_date_ms=0,
    )
    body["internalDate"] = "not-a-number"

    result = adapter._process_message(
        body,
        workspace_id=context.workspace_id,
        owner_id=owner_id,
        connector_account_id=context.connector_account_id,
        now=datetime.now(UTC),
    )
    assert result is None
    assert _threads_and_messages(context.workspace_id) == []


def test_process_message_skips_when_message_id_missing(
    seeded_gmail_account: tuple[ConnectorAccountContext, UUID],
) -> None:
    context, owner_id = seeded_gmail_account
    adapter = GmailAdapter()
    body = _message_body(
        message_id="irrelevant",
        thread_id="thread-no-id",
        from_addr="alice@example.test",
        to_addrs=[_OWNER_EMAIL],
        internal_date_ms=int(datetime.now(UTC).timestamp() * 1000),
    )
    del body["id"]

    result = adapter._process_message(
        body,
        workspace_id=context.workspace_id,
        owner_id=owner_id,
        connector_account_id=context.connector_account_id,
        now=datetime.now(UTC),
    )
    assert result is None
    assert _threads_and_messages(context.workspace_id) == []
