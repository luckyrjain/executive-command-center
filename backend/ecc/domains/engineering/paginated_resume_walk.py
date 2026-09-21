"""Shared page-walk/resume-cursor loop -- extracted from `github_adapter.
py`'s and `gitlab_adapter.py`'s own `_sync_repositories`, which were
byte-identical aside from the request shape, the field name carrying an
item's own timestamp, and the upsert call -- verified during an
architecture review of the connector/backfill area (2026-09-18). The
duplication was not cosmetic: the page-vs-budget counter-conflation bug
`93ceaf3` fixed ("make backfill genuinely resumable across GitHub/GitLab/
Jira") had to be found once in GitHub's copy, then independently
re-derived and re-fixed in GitLab's. This module gives that class of bug
one home instead of two -- and, as of the Jira migration, one home
instead of three.

**The cursor is opaque.** GitHub and GitLab paginate by page number over a
`Link` header; Jira paginates by a provider-issued `nextPageToken` in the
response body. Rather than baking either in, the walker treats
`cursor: str | None` as an opaque value: `fetch_page(cursor=...)` requests
"the page this cursor names" (`None` meaning the first page), and
`parse_page(response, cursor=...)` returns a `Page` -- the items plus the
next cursor (`None` meaning no further pages). Each adapter owns its own
pagination mechanism inside those two callables (GitHub/GitLab turn a
`Link` header into `str(page + 1)`; Jira returns `body["nextPageToken"]`);
the walker owns only what was actually duplicated: the per-call page
budget, the watermark stop, and the `SyncOutcome` it reports.

**Why callables, not subclasses/config.** Only genuinely-differing
per-provider concerns are callables: building the request (`fetch_page`),
reading the response's items and next cursor (`parse_page`), reading an
item's own timestamp (`extract_timestamp`), and persisting an item
(`upsert`). Two are optional and default off: `timestamp_key` (compare
timestamps by a derived key instead of as raw strings -- Jira's
`fields.updated` carries the site's own UTC offset, so raw string
comparison is unsound across a DST transition; see `jira_adapter.
_parse_jira_timestamp`) and `retry_without_cursor` (Jira's resume token
can go stale between calls and is rejected with a `400`; see below).

**Contract a caller must satisfy.**
- Items arrive newest-first by `extract_timestamp`'s own ordering -- the
  watermark stop exits at the first item at or before `since_cursor`, so
  oldest-first data would silently under-sync everything after it.
- `parse_page`'s `next_cursor` is the sole pagination-continuation signal
  -- and only for a non-empty page: an empty `items` list ends the walk as
  `succeeded` even if `next_cursor` is set. A parser that fails to report a
  next page a provider actually has makes the walk stop after that page and
  report `succeeded`, silently -- there is no error to alert on.
- `resume_cursor` is whatever a previous `partial` outcome reported as
  `backfill_resume_cursor`, round-tripped through storage unchanged.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from .connectors import SyncOutcome


@dataclass(frozen=True, slots=True)
class Page:
    """One fetched page: its items, and the cursor naming the next page
    (`None` when this is the last one).
    """

    items: Sequence[Mapping[str, Any]]
    next_cursor: str | None


class FetchPage(Protocol):
    """Named parameters, unlike `rate_limit_retry.py`'s own zero- or
    single-argument callables -- a positional `Callable[[str | None, int],
    ...]` called positionally could silently swap arguments against an
    implementation declaring them in the other order, with no type error.
    The real protection is the *call site* below using keyword arguments
    (`fetch_page(cursor=..., page_size=...)`): Python binds keyword
    arguments by name regardless of an implementation's own parameter
    order. This Protocol's `*` documents that contract (a caller must
    accept `cursor`/`page_size` as keywords); it only rejects a
    positional-only (`def f(cursor, page_size, /)`) implementation, which
    would fail loudly at the keyword call site anyway, `*` or not.
    """

    def __call__(self, *, cursor: str | None, page_size: int) -> httpx.Response | None: ...


class ParsePage(Protocol):
    def __call__(self, response: httpx.Response, *, cursor: str | None) -> Page: ...


# Page-number pagination over an RFC 8288 `Link` header -- shared by GitHub
# and GitLab, whose cursors are just page numbers (`"1"`, `"2"`, ...). It
# lives here rather than in either adapter so the one place that reads
# `response.links` (`httpx`'s own parser, not a hand-rolled comma-split -- a
# prior review found that split could mis-parse a `Link` header whose URL
# itself contains a comma) is shared. A provider paginating differently
# (Jira: a `nextPageToken` in the body) supplies its own `fetch_page`/
# `parse_page` instead.
LINK_HEADER_FIRST_CURSOR = "1"


def link_header_page_number(cursor: str | None) -> int:
    return int(cursor) if cursor else 1


def parse_link_header_page(response: httpx.Response, *, cursor: str | None) -> Page:
    """Flat JSON-array response body; a `Link` header with a `next` rel
    means another page exists. Anything else -- including a missing `Link`
    header on a provider that paginates some other way -- reports no next
    page, and the walk silently stops there as `succeeded`.
    """
    next_cursor = str(link_header_page_number(cursor) + 1) if "next" in response.links else None
    return Page(items=response.json(), next_cursor=next_cursor)


def walk_paginated_resource(
    *,
    resource_type: str,
    provider_label: str,
    resource_label: str,
    since_cursor: str | None,
    resume_cursor: str | None,
    start_cursor: str | None,
    apply_watermark_stop: bool,
    fetch_page: FetchPage,
    parse_page: ParsePage,
    extract_timestamp: Callable[[Mapping[str, Any]], str | None],
    upsert: Callable[[Mapping[str, Any]], None],
    timestamp_key: Callable[[str], Any] | None = None,
    retry_without_cursor: Callable[[httpx.Response], bool] | None = None,
    max_pages_per_call: int = 10,
    page_size: int = 100,
) -> SyncOutcome:
    """`apply_watermark_stop=False` (a `backfill()` call only) means this
    walk never stops early on `since_cursor` -- it walks purely by page,
    resuming from `resume_cursor` and stopping only on natural exhaustion
    or `max_pages_per_call`. `since_cursor` is always `None` on that path
    (`backfill()` never threads a watermark in), so `next_cursor` is only
    ever reported back to the caller on the very first backfill call
    (`resume_cursor is None`) -- every page beyond page 1 can, by
    construction (results are strictly newest-first), only contain items
    at or older than what page 1 of *that same walk's first call* already
    established as the true newest, so a resumed call has nothing new to
    teach the watermark and must not overwrite it with a lower value
    computed from a page deep in the walk. An *incremental* call
    (`apply_watermark_stop=True`) that ends `partial` (page cap or rate
    limit) reports `since_cursor` back unchanged, keeping the old watermark:
    it only saw the newest slice of the changed range, and advancing the
    watermark past the unfetched older pages would drop them for good.

    `resume_cursor` (the persisted state, used to tell a first backfill
    call from a resumed one) and `start_cursor` (the cursor to actually
    fetch first) are separate on purpose: a provider whose "first page" has
    an explicit name (GitHub/GitLab: page `"1"`) passes it as
    `start_cursor` while `resume_cursor` stays `None`; one whose first page
    is simply "no cursor" (Jira) passes `start_cursor=resume_cursor`. They
    must agree: when `resume_cursor` is not `None`, `start_cursor` must be
    it (or the walk reports resumed-call semantics for a first-page fetch).

    `fetch_page(cursor=..., page_size=...)` returning `None` means
    rate-limited beyond `bounded_single_retry`'s own bound -- reported as
    `partial`, resumable from the same cursor, never raised.

    `retry_without_cursor(response)`, when given, is consulted for a
    non-200 response on a request that carried a cursor: if it returns
    `True` and no such retry has happened yet this call, the walk
    restarts from the first page (`cursor=None`) instead of raising. That
    attempt still counts against `max_pages_per_call` -- otherwise a
    repeatedly-stale cursor could double a workspace's per-call request
    budget -- and only one such retry is allowed per call. `fetch_page`
    is therefore called with `cursor=None` mid-walk and must treat that as
    "first page" -- the same as at the start of a fresh walk.

    `timestamp_key(ts)` derives the value timestamps are compared by
    (default: the raw string). Returning `None` means "not comparable":
    such an item never triggers the watermark stop and never advances the
    newest-seen timestamp -- the safe direction to fail in (a possible
    extra sync, never a silently dropped update). Keys must be mutually
    comparable (`<=`, `>`); an exception raised by `timestamp_key`, or an
    incomparable pair of keys, propagates -- it is not swallowed as
    "not comparable".
    """
    key = timestamp_key or (lambda ts: ts)

    # A `partial` outcome may only advance the watermark for a *first
    # backfill* call (see the docstring above: page 1 of that walk already
    # established the true newest). An *incremental* walk cut short by the
    # page cap or a rate limit has only seen the newest slice of what
    # changed since `since_cursor` -- advancing the watermark to that
    # slice's newest timestamp would make the next call stop at the first
    # item and never fetch the older, still-unsynced pages. It reports
    # `since_cursor` back unchanged instead: the watermark stays put (the
    # next call re-walks the range; upserts are idempotent), while the
    # non-`None` value still makes `connector_sync` upsert the cursor row,
    # which is what `metrics.py`'s coverage freshness reads. A resumed
    # backfill call reports `None`, as always.
    def partial_next_cursor(newest: str | None) -> str | None:
        if resume_cursor is None and not apply_watermark_stop:
            return newest
        return since_cursor if apply_watermark_stop else None

    items_processed = 0
    newest_updated_at = since_cursor
    since_key = key(since_cursor) if since_cursor is not None else None
    newest_key = since_key
    # `pages_fetched_this_call` is the per-call budget; the cursor is free
    # to start anywhere. `93ceaf3` found conflating the two (a loop
    # condition of `page <= max_pages_per_call` directly) happened to work
    # only because every call started at page 1; resuming at page 11 made
    # that same condition false immediately, ending the call with zero
    # requests made.
    cursor = start_cursor
    pages_fetched_this_call = 0
    stopped_early = False
    retried_without_cursor = False

    while pages_fetched_this_call < max_pages_per_call:
        pages_fetched_this_call += 1
        response = fetch_page(cursor=cursor, page_size=page_size)
        if response is None:
            return SyncOutcome(
                resource_type=resource_type,
                items_processed=items_processed,
                status="partial",
                next_cursor=partial_next_cursor(newest_updated_at),
                error_summary=(
                    f"{provider_label} rate limit exceeded; sync paused, will resume next call"
                ),
                # Rate-limited before fetching anything on a first call:
                # there is no progress to resume, so report none -- a
                # persisted `start_cursor` would make the next call look
                # like a *resumed* one (`resume_cursor is not None`), which
                # never reports the watermark a first backfill call must.
                backfill_resume_cursor=(
                    None if resume_cursor is None and cursor == start_cursor else cursor
                ),
            )
        if response.status_code != 200:
            if (
                retry_without_cursor is not None
                and cursor is not None
                and not retried_without_cursor
                and retry_without_cursor(response)
            ):
                retried_without_cursor = True
                cursor = None
                continue
            raise RuntimeError(
                f"{provider_label} {resource_label} list failed with status {response.status_code}"
            )

        page = parse_page(response, cursor=cursor)
        if not page.items:
            break

        for item in page.items:
            updated_at = extract_timestamp(item)
            updated_key = key(updated_at) if updated_at is not None else None
            if (
                apply_watermark_stop
                and since_key is not None
                and updated_key is not None
                and updated_key <= since_key
            ):
                stopped_early = True
                break
            upsert(item)
            items_processed += 1
            if updated_key is not None and (newest_key is None or updated_key > newest_key):
                newest_updated_at = updated_at
                newest_key = updated_key

        if stopped_early or page.next_cursor is None:
            break
        cursor = page.next_cursor
    else:
        # The loop ran `max_pages_per_call` iterations without ever
        # `break`-ing -- i.e. the last page fetched still had further
        # pages available (or the last budget slot went to a stale-cursor
        # retry, leaving `cursor=None`: the next call then starts over).
        # `partial`, not `succeeded` -- silently capping a large account's
        # backfill with no signal that more remained was the other half of
        # `93ceaf3`'s fix. On a first backfill call `next_cursor` is the
        # newest timestamp observed so far, so a subsequent *incremental*
        # sync call resumes from it.
        # `backfill_resume_cursor=cursor`: `cursor` was already advanced to
        # the last page's `next_cursor`, so it names the next page to fetch.
        return SyncOutcome(
            resource_type=resource_type,
            items_processed=items_processed,
            status="partial",
            next_cursor=partial_next_cursor(newest_updated_at),
            error_summary=(
                f"{provider_label} {resource_label} sync hit the {max_pages_per_call}-page "
                "per-call bound with more pages remaining; sync paused, will resume next call"
            ),
            backfill_resume_cursor=cursor,
        )

    return SyncOutcome(
        resource_type=resource_type,
        items_processed=items_processed,
        status="succeeded",
        next_cursor=newest_updated_at if resume_cursor is None else None,
        backfill_resume_cursor=None,
    )
