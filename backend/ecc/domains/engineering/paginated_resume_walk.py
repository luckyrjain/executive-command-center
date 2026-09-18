"""Shared page-walk/resume-cursor loop -- extracted from `github_adapter.
py`'s and `gitlab_adapter.py`'s own `_sync_repositories`, which were
byte-identical aside from the request shape, the field name carrying an
item's own timestamp, and the upsert call -- verified during an
architecture review of the connector/backfill area (2026-09-18). The
duplication was not cosmetic: the page-vs-budget counter-conflation bug
`93ceaf3` fixed ("make backfill genuinely resumable across GitHub/GitLab/
Jira") had to be found once in GitHub's copy, then independently
re-derived and re-fixed in GitLab's. This module gives that class of bug
one home instead of two.

**Why `response.json()` and the `response.links` "next" check are owned
here, not taken as callables.** Both providers return a flat JSON array
(no envelope key) and both paginate via the RFC 8288 `Link` header --
`httpx`'s own parser, not a hand-rolled comma-split (a prior review found
that split could mis-parse a `Link` header whose URL itself contains a
comma). That's a proven identical mechanism between the two providers,
not a coincidental similarity -- GitLab's own retired `_sync_repositories`
docstring said as much explicitly. Only genuinely-differing per-provider
concerns are callables: building the request (`fetch_page`), reading an
item's own timestamp (`extract_timestamp`), and persisting an item
(`upsert`).

**Jira's `_sync_work_items` is not a drop-in third caller today -- an
earlier draft of this docstring understated how much would need to
change.** With only two callers, this module is an extraction of two
designs that happen to be identical in every dimension that matters, not
yet a proven seam (two genuinely different designs sharing one
interface). Adapting Jira here would hit three real mismatches, not one:
its response is `{"issues": [...]}`, not a flat array, so this module's
own `items = response.json()` would iterate the wrong thing; its
next-page signal is a `nextPageToken` field in the response body, not a
`Link` header, so `"next" not in response.links` is always `True` against
a Jira response and the walk would silently stop after page 1 every call,
reporting `succeeded` -- no error, nothing to alert on; and its resume
value is an opaque provider-issued token, not a page number, so this
module's own `int(resume_cursor)` (below) would raise on a real token
immediately, not degrade gracefully. Migrating Jira onto this module
means widening its signature (at minimum, an `extract_items(response)`
callable and treating `resume_cursor` as opaque rather than `int()`-ing
it), not just adding its stale-token-retry quirk as a fourth callable.

**Contract a caller must satisfy** (see `walk_paginated_resource`'s own
docstring below for the per-call behavior this implies):
- `fetch_page` returns a response whose `.json()` is a flat JSON array
  (no envelope key).
- Further pages are discoverable via the RFC 8288 `Link` response header
  (a `next` rel) -- there is no other pagination-continuation mechanism.
- Items arrive newest-first by `extract_timestamp`'s own ordering.
- `resume_cursor` is a page number (`str` of an `int`), not an opaque
  token -- unlike `ConnectorAdapter`'s own more general opaque-cursor
  contract.

Violating any of these degrades differently: a non-flat-array response
raises inside `extract_timestamp` with a confusing stack trace (loud);
missing the `Link` header stops the walk after page 1 silently, reporting
`succeeded` (quiet and dangerous); oldest-first data makes the
watermark-stop check exit after the very first stale-looking item,
silently under-syncing everything after it (quiet).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Protocol

import httpx

from .connectors import SyncOutcome


class FetchPage(Protocol):
    """Named parameters, unlike `rate_limit_retry.py`'s own zero- or
    single-argument callables -- `page`/`page_size` are both plain `int`,
    so a positional
    `Callable[[int, int], ...]` called positionally (`fetch_page(page,
    page_size)`) would silently swap which int means what at runtime
    against an implementation declaring them in the other order, with no
    type error. The real fix is the *call site* below using keyword
    arguments (`fetch_page(page=page, page_size=page_size)`) -- Python
    binds keyword arguments by name regardless of an implementation's own
    parameter order, so a correctly-named implementation can never be
    transposed this way again. This Protocol's `*` documents that
    contract explicitly (a caller must accept `page`/`page_size` as
    keywords) rather than encode the protection itself -- it only rejects
    a positional-only (`def f(page, page_size, /)`) implementation, which
    would fail loudly at the keyword call site anyway, `*` or not.
    """

    def __call__(self, *, page: int, page_size: int) -> httpx.Response | None: ...


def walk_paginated_resource(
    *,
    resource_type: str,
    provider_label: str,
    resource_label: str,
    since_cursor: str | None,
    resume_cursor: str | None,
    apply_watermark_stop: bool,
    fetch_page: FetchPage,
    extract_timestamp: Callable[[Mapping[str, Any]], str | None],
    upsert: Callable[[Mapping[str, Any]], None],
    max_pages_per_call: int = 10,
    page_size: int = 100,
) -> SyncOutcome:
    """`apply_watermark_stop=False` (a `backfill()` call only) means this
    walk never stops early on `since_cursor` -- it walks purely by page,
    resuming from `resume_cursor` (the page to continue from) and
    stopping only on natural exhaustion or `max_pages_per_call`.
    `since_cursor` is always `None` on that path (`backfill()` never
    threads a watermark in), so `next_cursor` is only ever reported back
    to the caller on the very first backfill call (`resume_cursor is
    None`) -- every page beyond page 1 can, by construction (results are
    strictly newest-first), only contain items at or older than what page
    1 of *that same walk's first call* already established as the true
    newest, so a resumed call has nothing new to teach the watermark and
    must not overwrite it with a lower value computed from a page deep in
    the walk.

    `fetch_page(page=..., page_size=...)` returning `None` means
    rate-limited beyond `bounded_single_retry`'s own bound -- reported as
    `partial`, resumable from the same page, never raised.

    Requires of `fetch_page`: a successful response's `.json()` is a flat
    JSON array (no envelope key), items arrive newest-first by
    `extract_timestamp`'s own ordering, and further pages are signaled
    only via an RFC 8288 `Link` response header (a `next` rel) -- there is
    no other pagination-continuation mechanism this function recognizes.
    `resume_cursor` is always a page number (`str` of an `int`), not an
    opaque token.
    """
    items_processed = 0
    newest_updated_at = since_cursor
    # `page` is the real, absolute page number requested from the
    # provider (carries across calls via `resume_cursor`) -- deliberately
    # a *different* variable from `pages_fetched_this_call` below. `93ceaf3`
    # found conflating the two (a loop condition of `page <=
    # max_pages_per_call` directly) happened to work only because every
    # call started at page 1; resuming at page 11 made that same
    # condition false immediately, ending the call with zero requests
    # made.
    page = int(resume_cursor) if resume_cursor else 1
    pages_fetched_this_call = 0
    stopped_early = False

    while pages_fetched_this_call < max_pages_per_call:
        pages_fetched_this_call += 1
        response = fetch_page(page=page, page_size=page_size)
        if response is None:
            return SyncOutcome(
                resource_type=resource_type,
                items_processed=items_processed,
                status="partial",
                next_cursor=newest_updated_at if resume_cursor is None else None,
                error_summary=(
                    f"{provider_label} rate limit exceeded; sync paused, will resume next call"
                ),
                backfill_resume_cursor=str(page),
            )
        if response.status_code != 200:
            raise RuntimeError(
                f"{provider_label} {resource_label} list failed with status {response.status_code}"
            )

        items = response.json()
        if not items:
            break

        for item in items:
            updated_at = extract_timestamp(item)
            if (
                apply_watermark_stop
                and since_cursor is not None
                and updated_at is not None
                and updated_at <= since_cursor
            ):
                stopped_early = True
                break
            upsert(item)
            items_processed += 1
            if newest_updated_at is None or (updated_at and updated_at > newest_updated_at):
                newest_updated_at = updated_at

        if stopped_early or "next" not in response.links:
            break
        page += 1
    else:
        # The loop ran `max_pages_per_call` iterations without ever
        # `break`-ing -- i.e. the last page fetched still had further
        # pages available. `partial`, not `succeeded` -- silently capping
        # a large account's backfill with no signal that more remained
        # was the other half of `93ceaf3`'s fix. `next_cursor` is still
        # the newest timestamp observed so far, so a subsequent
        # *incremental* sync call resumes from it.
        # `backfill_resume_cursor=str(page)`: the loop's own `page += 1`
        # already advanced past the last page actually fetched, so `page`
        # here already names the next page to resume from.
        return SyncOutcome(
            resource_type=resource_type,
            items_processed=items_processed,
            status="partial",
            next_cursor=newest_updated_at if resume_cursor is None else None,
            error_summary=(
                f"{provider_label} {resource_label} sync hit the {max_pages_per_call}-page "
                "per-call bound with more pages remaining; sync paused, will resume next call"
            ),
            backfill_resume_cursor=str(page),
        )

    return SyncOutcome(
        resource_type=resource_type,
        items_processed=items_processed,
        status="succeeded",
        next_cursor=newest_updated_at if resume_cursor is None else None,
        backfill_resume_cursor=None,
    )
