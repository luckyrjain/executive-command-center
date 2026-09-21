"""`paginated_resume_walk.walk_paginated_resource` -- the page-walk/resume-
cursor/watermark loop shared by the GitHub, GitLab and Jira adapters
(architecture review, 2026-09-18; see that module's own docstring for the
full duplication history). Pure unit tests against fake `fetch_page`/
`parse_page`/`extract_timestamp`/`upsert` callables -- no HTTP, no database,
no Postgres -- since the whole point of the extraction is that these
invariants no longer need either to exercise directly.

Covers the invariants `93ceaf3` ("make backfill genuinely resumable across
GitHub/GitLab/Jira") found broken and fixed once per provider: resuming a
backfill from a cursor other than the first, the cursor-vs-budget split,
the per-call page bound reporting `partial` (not a silent `succeeded`) with
more pages remaining, and `next_cursor` only ever being reported back on
the very first backfill call -- plus the opaque-cursor, `timestamp_key` and
`retry_without_cursor` hooks Jira needs.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

import httpx
import pytest

from ecc.domains.engineering import paginated_resume_walk as walk
from ecc.domains.engineering.connectors import SyncOutcome


def _link_response(
    items: list[dict[str, object]], *, next_page: int | None = None, status: int = 200
) -> httpx.Response:
    headers = {}
    if next_page is not None:
        headers["Link"] = f'<https://example.test/items?page={next_page}>; rel="next"'
    return httpx.Response(status, json=items, headers=headers)


def _item(id_: int, updated_at: str | None) -> dict[str, object]:
    return {"id": id_, "updated_at": updated_at}


def _ts(item: Mapping[str, Any]) -> str | None:
    value = item["updated_at"]
    return value if isinstance(value, str) or value is None else str(value)


def _run_link(
    pages: Mapping[int, httpx.Response] | Callable[[int], httpx.Response | None],
    *,
    since_cursor: str | None = None,
    resume_cursor: str | None = None,
    apply_watermark_stop: bool = True,
    upserted: list[Mapping[str, Any]] | None = None,
    requested: list[int] | None = None,
    **kwargs: Any,
) -> SyncOutcome:
    """GitHub/GitLab-shaped walk: page-number cursors over a `Link` header."""

    def fetch_page(*, cursor: str | None, page_size: int) -> httpx.Response | None:
        number = walk.link_header_page_number(cursor)
        if requested is not None:
            requested.append(number)
        return pages(number) if callable(pages) else pages[number]

    return walk.walk_paginated_resource(
        resource_type="repository",
        provider_label="TestProvider",
        resource_label="repository",
        since_cursor=since_cursor,
        resume_cursor=resume_cursor,
        start_cursor=resume_cursor or walk.LINK_HEADER_FIRST_CURSOR,
        apply_watermark_stop=apply_watermark_stop,
        fetch_page=fetch_page,
        parse_page=walk.parse_link_header_page,
        extract_timestamp=_ts,
        upsert=(upserted.append if upserted is not None else lambda item: None),
        **kwargs,
    )


def test_single_page_reports_succeeded_and_the_newest_timestamp() -> None:
    upserted: list[Mapping[str, Any]] = []
    outcome = _run_link(
        {1: _link_response([_item(1, "2026-01-01T00:00:00Z"), _item(2, "2026-01-02T00:00:00Z")])},
        upserted=upserted,
    )

    assert outcome.status == "succeeded"
    assert outcome.items_processed == 2
    assert [item["id"] for item in upserted] == [1, 2]
    assert outcome.next_cursor == "2026-01-02T00:00:00Z"
    assert outcome.backfill_resume_cursor is None


def test_pagination_follows_the_link_header_across_multiple_pages() -> None:
    upserted: list[Mapping[str, Any]] = []
    outcome = _run_link(
        {
            1: _link_response([_item(1, "2026-01-01T00:00:00Z")], next_page=2),
            2: _link_response([_item(2, "2026-01-02T00:00:00Z")]),
        },
        upserted=upserted,
    )

    assert outcome.status == "succeeded"
    assert [item["id"] for item in upserted] == [1, 2]
    assert outcome.next_cursor == "2026-01-02T00:00:00Z"


def test_watermark_stop_only_upserts_items_newer_than_since_cursor() -> None:
    upserted: list[Mapping[str, Any]] = []
    outcome = _run_link(
        {
            1: _link_response(
                [_item(2, "2026-01-05T00:00:00Z"), _item(1, "2026-01-01T00:00:00Z")], next_page=2
            )
        },
        since_cursor="2026-01-01T00:00:00Z",
        upserted=upserted,
    )

    assert outcome.status == "succeeded"
    assert [item["id"] for item in upserted] == [2]
    assert outcome.next_cursor == "2026-01-05T00:00:00Z"


def test_backfill_resumes_from_the_given_cursor_not_the_first_page() -> None:
    """The exact bug `93ceaf3` fixed: resuming a backfill call must request
    the resume page, not restart at page 1, and the page-vs-budget counter
    must be independent of the loop's own bound.
    """
    requested: list[int] = []
    outcome = _run_link(
        {11: _link_response([_item(1, "2026-01-01T00:00:00Z")])},
        resume_cursor="11",
        apply_watermark_stop=False,
        requested=requested,
        max_pages_per_call=10,
    )

    assert requested == [11]
    assert outcome.status == "succeeded"
    # A resumed backfill call never reports next_cursor -- only the very
    # first backfill call (resume_cursor is None) is allowed to, since a
    # page deep into the walk can't teach the watermark anything newer.
    assert outcome.next_cursor is None


def test_page_cap_reports_partial_with_the_resume_cursor_preserved() -> None:
    calls: list[int] = []

    def pages(number: int) -> httpx.Response:
        calls.append(number)
        return _link_response([_item(number, "2026-01-01T00:00:00Z")], next_page=number + 1)

    outcome = _run_link(pages, apply_watermark_stop=False, max_pages_per_call=3)

    assert calls == [1, 2, 3]
    assert outcome.status == "partial"
    assert outcome.items_processed == 3
    # cursor advanced past the last page actually fetched (3 -> 4).
    assert outcome.backfill_resume_cursor == "4"
    assert "3-page per-call bound" in (outcome.error_summary or "")
    # This is the *first* backfill call (resume_cursor=None) -- unlike a
    # resumed call, it must still report next_cursor.
    assert outcome.next_cursor == "2026-01-01T00:00:00Z"


def test_backfill_ignores_a_leftover_since_cursor() -> None:
    """`apply_watermark_stop=False` must stay purely page-driven even if a
    caller passes a non-`None` `since_cursor`.
    """
    upserted: list[Mapping[str, Any]] = []
    outcome = _run_link(
        {1: _link_response([_item(1, "2020-01-01T00:00:00Z")])},
        since_cursor="2026-01-01T00:00:00Z",  # would stop everything if honored
        apply_watermark_stop=False,
        upserted=upserted,
    )

    assert outcome.status == "succeeded"
    assert [item["id"] for item in upserted] == [1]


def test_empty_first_page_succeeds_with_no_items() -> None:
    # `next_page=2` makes this a real regression guard for the empty-page
    # break: without it the walk would try page 2 (a KeyError here).
    outcome = _run_link({1: _link_response([], next_page=2)})

    assert outcome.status == "succeeded"
    assert outcome.items_processed == 0
    assert outcome.next_cursor is None


def test_item_with_no_timestamp_is_still_upserted_and_does_not_crash() -> None:
    upserted: list[Mapping[str, Any]] = []
    outcome = _run_link(
        {1: _link_response([_item(1, None), _item(2, "2026-01-01T00:00:00Z")])},
        since_cursor="2020-01-01T00:00:00Z",
        upserted=upserted,
    )

    assert outcome.status == "succeeded"
    assert [item["id"] for item in upserted] == [1, 2]
    assert outcome.next_cursor == "2026-01-01T00:00:00Z"


def test_rate_limit_reports_partial_and_preserves_the_current_cursor() -> None:
    outcome = _run_link(lambda number: None, resume_cursor="5", apply_watermark_stop=False)

    assert outcome.status == "partial"
    assert outcome.backfill_resume_cursor == "5"
    assert outcome.next_cursor is None
    assert "rate limit" in (outcome.error_summary or "").lower()


def test_rate_limit_before_any_progress_on_a_first_call_reports_no_resume_cursor() -> None:
    """Rate-limited at the very first page of a first backfill call there is
    nothing to resume. Persisting `start_cursor` ("1") as the resume cursor
    would make the next call look *resumed* -- and a resumed call never
    reports the watermark a first backfill call must.
    """
    outcome = _run_link(lambda number: None, apply_watermark_stop=False)

    assert outcome.status == "partial"
    assert outcome.backfill_resume_cursor is None
    assert outcome.next_cursor is None


def test_rate_limit_mid_walk_on_a_first_backfill_keeps_watermark_and_resume_page() -> None:
    def pages(number: int) -> httpx.Response | None:
        if number == 1:
            return _link_response([_item(1, "2026-01-05T00:00:00Z")], next_page=2)
        return None

    outcome = _run_link(pages, apply_watermark_stop=False)

    assert outcome.status == "partial"
    assert outcome.backfill_resume_cursor == "2"
    assert outcome.next_cursor == "2026-01-05T00:00:00Z"


def test_incremental_page_cap_partial_keeps_the_old_watermark() -> None:
    """Advancing the watermark to page 1's newest item would make the next
    incremental call stop at its first item and drop pages 2.. for good.
    """

    def pages(number: int) -> httpx.Response:
        return _link_response(
            [_item(number, f"2026-02-{30 - number:02d}T00:00:00Z")], next_page=number + 1
        )

    outcome = _run_link(pages, since_cursor="2026-01-01T00:00:00Z", max_pages_per_call=3)

    assert outcome.status == "partial"
    assert outcome.items_processed == 3
    assert outcome.next_cursor is None


def test_incremental_rate_limit_partial_keeps_the_old_watermark() -> None:
    def pages(number: int) -> httpx.Response | None:
        if number == 1:
            return _link_response([_item(1, "2026-02-01T00:00:00Z")], next_page=2)
        return None

    outcome = _run_link(pages, since_cursor="2026-01-01T00:00:00Z")

    assert outcome.status == "partial"
    assert outcome.items_processed == 1
    assert outcome.next_cursor is None


def test_incremental_that_completes_still_advances_the_watermark() -> None:
    outcome = _run_link(
        {1: _link_response([_item(2, "2026-02-01T00:00:00Z"), _item(1, "2026-01-01T00:00:00Z")])},
        since_cursor="2026-01-01T00:00:00Z",
    )

    assert outcome.status == "succeeded"
    assert outcome.next_cursor == "2026-02-01T00:00:00Z"


def test_empty_page_ends_the_walk_even_when_a_next_cursor_is_reported() -> None:
    requested: list[int] = []
    outcome = _run_link({1: _link_response([], next_page=2)}, requested=requested)

    assert outcome.status == "succeeded"
    assert requested == [1]


def test_stale_retry_consuming_the_last_budget_slot_reports_partial_with_no_resume() -> None:
    def fetch_page(*, cursor: str | None, page_size: int) -> httpx.Response | None:
        return httpx.Response(400) if cursor is not None else _link_response([])

    outcome = walk.walk_paginated_resource(
        resource_type="repository",
        provider_label="TestProvider",
        resource_label="repository",
        since_cursor=None,
        resume_cursor="tok",
        start_cursor="tok",
        apply_watermark_stop=False,
        fetch_page=fetch_page,
        parse_page=lambda response, *, cursor: walk.Page(items=[], next_cursor=None),
        extract_timestamp=_ts,
        upsert=lambda item: None,
        retry_without_cursor=lambda response: True,
        max_pages_per_call=1,
    )

    assert outcome.status == "partial"
    assert outcome.backfill_resume_cursor is None


def test_non_200_response_raises() -> None:
    with pytest.raises(RuntimeError, match="TestProvider repository list failed with status 500"):
        _run_link({1: httpx.Response(500)})


# --- opaque token cursors (Jira-shaped) -------------------------------------


def _run_tokens(
    pages: Mapping[str | None, httpx.Response],
    *,
    resume_cursor: str | None = None,
    since_cursor: str | None = None,
    apply_watermark_stop: bool = False,
    requested: list[str | None] | None = None,
    upserted: list[Mapping[str, Any]] | None = None,
    **kwargs: Any,
) -> SyncOutcome:
    """Token-paginated walk: an envelope body, the next token in the body,
    the first page being "no token" -- none of which the walker may assume.
    """

    def fetch_page(*, cursor: str | None, page_size: int) -> httpx.Response | None:
        if requested is not None:
            requested.append(cursor)
        return pages[cursor]

    def parse_page(response: httpx.Response, *, cursor: str | None) -> walk.Page:
        body = response.json()
        return walk.Page(items=body["issues"], next_cursor=body.get("nextPageToken"))

    return walk.walk_paginated_resource(
        resource_type="work_item",
        provider_label="TokenProvider",
        resource_label="work item",
        since_cursor=since_cursor,
        resume_cursor=resume_cursor,
        start_cursor=resume_cursor,
        apply_watermark_stop=apply_watermark_stop,
        fetch_page=fetch_page,
        parse_page=parse_page,
        extract_timestamp=_ts,
        upsert=(upserted.append if upserted is not None else lambda item: None),
        **kwargs,
    )


def _token_page(
    items: list[dict[str, object]], next_token: str | None = None, status: int = 200
) -> httpx.Response:
    body: dict[str, object] = {"issues": items}
    if next_token is not None:
        body["nextPageToken"] = next_token
    return httpx.Response(status, json=body)


def test_opaque_token_cursor_walks_pages_and_starts_with_no_cursor() -> None:
    requested: list[str | None] = []
    upserted: list[Mapping[str, Any]] = []
    outcome = _run_tokens(
        {
            None: _token_page([_item(1, "2026-01-02T00:00:00Z")], "tok-a"),
            "tok-a": _token_page([_item(2, "2026-01-01T00:00:00Z")]),
        },
        requested=requested,
        upserted=upserted,
    )

    assert requested == [None, "tok-a"]
    assert [item["id"] for item in upserted] == [1, 2]
    assert outcome.status == "succeeded"
    assert outcome.next_cursor == "2026-01-02T00:00:00Z"


def test_token_page_cap_reports_the_next_token_as_the_resume_cursor() -> None:
    outcome = _run_tokens(
        {
            None: _token_page([_item(1, "2026-01-01T00:00:00Z")], "tok-a"),
            "tok-a": _token_page([_item(2, "2026-01-01T00:00:00Z")], "tok-b"),
        },
        max_pages_per_call=2,
    )

    assert outcome.status == "partial"
    assert outcome.backfill_resume_cursor == "tok-b"


def test_token_backfill_resumes_from_the_given_token() -> None:
    requested: list[str | None] = []
    outcome = _run_tokens(
        {"tok-z": _token_page([_item(1, "2026-01-01T00:00:00Z")])},
        resume_cursor="tok-z",
        requested=requested,
    )

    assert requested == ["tok-z"]
    assert outcome.status == "succeeded"
    assert outcome.next_cursor is None


def test_resumed_page_cap_partial_reports_no_next_cursor() -> None:
    """A resumed call has nothing new to teach the watermark, so even when it
    hits the page cap it must not report a `next_cursor`.
    """
    outcome = _run_tokens(
        {
            "tok-a": _token_page([_item(1, "2026-01-01T00:00:00Z")], "tok-b"),
            "tok-b": _token_page([_item(2, "2026-01-01T00:00:00Z")], "tok-c"),
        },
        resume_cursor="tok-a",
        max_pages_per_call=2,
    )

    assert outcome.status == "partial"
    assert outcome.backfill_resume_cursor == "tok-c"
    assert outcome.next_cursor is None


def test_rate_limit_on_a_fresh_token_walk_reports_no_resume_cursor() -> None:
    outcome = walk.walk_paginated_resource(
        resource_type="work_item",
        provider_label="TokenProvider",
        resource_label="work item",
        since_cursor=None,
        resume_cursor=None,
        start_cursor=None,
        apply_watermark_stop=False,
        fetch_page=lambda *, cursor, page_size: None,
        parse_page=lambda response, *, cursor: walk.Page(items=[], next_cursor=None),
        extract_timestamp=_ts,
        upsert=lambda item: None,
    )

    assert outcome.status == "partial"
    assert outcome.backfill_resume_cursor is None


# --- retry_without_cursor (Jira's stale-token fallback) ---------------------


def _stale(response: httpx.Response) -> bool:
    return response.status_code == 400


def test_stale_cursor_is_retried_once_from_the_first_page() -> None:
    requested: list[str | None] = []
    outcome = _run_tokens(
        {
            "stale": _token_page([], status=400),
            None: _token_page([_item(1, "2026-01-01T00:00:00Z")]),
        },
        resume_cursor="stale",
        requested=requested,
        retry_without_cursor=_stale,
    )

    assert requested == ["stale", None]
    assert outcome.status == "succeeded"
    assert outcome.items_processed == 1


def test_stale_cursor_retry_draws_from_the_same_page_budget() -> None:
    """The wasted stale attempt counts against `max_pages_per_call`, not a
    refund -- otherwise a repeatedly-stale token could double the budget.
    """
    requested: list[str | None] = []
    outcome = _run_tokens(
        {
            "stale": _token_page([], status=400),
            None: _token_page([_item(1, "2026-01-01T00:00:00Z")], "tok-a"),
            "tok-a": _token_page([_item(2, "2026-01-01T00:00:00Z")]),
        },
        resume_cursor="stale",
        requested=requested,
        retry_without_cursor=_stale,
        max_pages_per_call=2,
    )

    # Budget of 2: the stale attempt + the fresh page 1; "tok-a" is never
    # fetched, and the call reports partial with more remaining.
    assert requested == ["stale", None]
    assert outcome.status == "partial"
    assert outcome.backfill_resume_cursor == "tok-a"


def test_a_failing_first_page_after_a_stale_retry_raises() -> None:
    with pytest.raises(RuntimeError, match="TokenProvider work item list failed with status 400"):
        _run_tokens(
            {"stale": _token_page([], status=400), None: _token_page([], status=400)},
            resume_cursor="stale",
            retry_without_cursor=_stale,
        )


def test_a_second_stale_cursor_mid_walk_is_not_retried_again() -> None:
    """The retry flag, not just the `cursor is not None` guard, caps it at
    one: after restarting from the first page, a *later* page's token going
    stale must raise rather than loop back to the start a second time.
    """
    requested: list[str | None] = []
    with pytest.raises(RuntimeError, match="status 400"):
        _run_tokens(
            {
                "stale": _token_page([], status=400),
                None: _token_page([_item(1, "2026-01-01T00:00:00Z")], "tok-a"),
                "tok-a": _token_page([], status=400),
            },
            resume_cursor="stale",
            requested=requested,
            retry_without_cursor=_stale,
        )

    assert requested == ["stale", None, "tok-a"]


def test_no_retry_when_the_failed_request_carried_no_cursor() -> None:
    requested: list[str | None] = []
    with pytest.raises(RuntimeError, match="status 400"):
        _run_tokens(
            {None: _token_page([], status=400)},
            requested=requested,
            retry_without_cursor=_stale,
        )
    assert requested == [None]


def test_no_retry_when_the_callback_declines() -> None:
    with pytest.raises(RuntimeError, match="status 500"):
        _run_tokens(
            {"tok": _token_page([], status=500)},
            resume_cursor="tok",
            retry_without_cursor=_stale,
        )


def test_without_a_retry_callback_a_stale_cursor_raises() -> None:
    with pytest.raises(RuntimeError, match="status 400"):
        _run_tokens({"tok": _token_page([], status=400)}, resume_cursor="tok")


# --- timestamp_key (Jira's DST-safe datetime comparison) --------------------


def test_timestamp_key_compares_by_the_derived_key_not_the_raw_string() -> None:
    """Raw strings would compare "10" <= "9" (lexicographic) and stop
    immediately; comparing by `int` keeps the newer item.
    """
    upserted: list[Mapping[str, Any]] = []
    outcome = _run_tokens(
        {None: _token_page([_item(1, "10")])},
        since_cursor="9",
        apply_watermark_stop=True,
        upserted=upserted,
        timestamp_key=int,
    )

    assert [item["id"] for item in upserted] == [1]
    # ...and the raw string is what gets reported back, never the key.
    assert outcome.next_cursor == "10"


def test_a_key_of_none_never_stops_the_walk_or_advances_the_newest_timestamp() -> None:
    def key(ts: str) -> int | None:
        return int(ts) if ts.isdigit() else None

    upserted: list[Mapping[str, Any]] = []
    outcome = _run_tokens(
        {None: _token_page([_item(1, "not-a-number"), _item(2, "12")])},
        since_cursor="9",
        apply_watermark_stop=True,
        upserted=upserted,
        timestamp_key=key,
    )

    assert [item["id"] for item in upserted] == [1, 2]
    assert outcome.next_cursor == "12"


def test_a_falsy_but_valid_key_still_participates_in_comparison() -> None:
    """A key of `0` is a real value, not "not comparable": it must stop the
    walk at the watermark and count as the newest seen.
    """
    upserted: list[Mapping[str, Any]] = []
    outcome = _run_tokens(
        {None: _token_page([_item(1, "0"), _item(2, "-1")])},
        since_cursor="-1",
        apply_watermark_stop=True,
        upserted=upserted,
        timestamp_key=int,
    )

    assert [item["id"] for item in upserted] == [1]
    assert outcome.next_cursor == "0"


def test_an_unparseable_since_cursor_never_stops_the_walk() -> None:
    def key(ts: str) -> int | None:
        return int(ts) if ts.isdigit() else None

    upserted: list[Mapping[str, Any]] = []
    _run_tokens(
        {None: _token_page([_item(1, "5"), _item(2, "3")])},
        since_cursor="garbage",
        apply_watermark_stop=True,
        upserted=upserted,
        timestamp_key=key,
    )

    assert [item["id"] for item in upserted] == [1, 2]


# --- gaps found by mutation testing -----------------------------------------


def test_rate_limit_mid_walk_reports_progress_and_the_unfetched_page_as_resume_cursor() -> None:
    """The resume cursor is the page that was *not* fetched (the advanced
    cursor), not the walk's starting cursor, and the items already upserted
    plus the first-call watermark are still reported.
    """

    def pages(number: int) -> httpx.Response | None:
        if number == 1:
            return _link_response([_item(1, "2026-01-02T00:00:00Z")], next_page=2)
        return None

    outcome = _run_link(pages, apply_watermark_stop=False)

    assert outcome.status == "partial"
    assert outcome.items_processed == 1
    assert outcome.backfill_resume_cursor == "2"
    assert outcome.next_cursor == "2026-01-02T00:00:00Z"


def test_rate_limit_mid_resumed_walk_never_reports_next_cursor() -> None:
    def pages(number: int) -> httpx.Response | None:
        if number == 5:
            return _link_response([_item(1, "2026-01-02T00:00:00Z")], next_page=6)
        return None

    outcome = _run_link(pages, resume_cursor="5", apply_watermark_stop=False)

    assert outcome.status == "partial"
    assert outcome.items_processed == 1
    assert outcome.backfill_resume_cursor == "6"
    assert outcome.next_cursor is None


@pytest.mark.parametrize("status", [201, 204, 302, 404])
def test_any_non_200_status_raises(status: int) -> None:
    with pytest.raises(RuntimeError, match=f"status {status}"):
        _run_link({1: httpx.Response(status)})


def test_watermark_stop_abandons_the_rest_of_the_page() -> None:
    """The watermark stop exits at the first at-or-before item; nothing after
    it in the same page (even something that looks newer) is upserted.
    """
    upserted: list[Mapping[str, Any]] = []
    _run_link(
        {
            1: _link_response(
                [
                    _item(3, "2026-01-05T00:00:00Z"),
                    _item(2, "2026-01-01T00:00:00Z"),
                    _item(1, "2026-01-06T00:00:00Z"),
                ]
            )
        },
        since_cursor="2026-01-03T00:00:00Z",
        upserted=upserted,
    )

    assert [item["id"] for item in upserted] == [3]


def test_nothing_newer_than_the_watermark_echoes_the_since_cursor() -> None:
    outcome = _run_link(
        {1: _link_response([_item(1, "2026-01-01T00:00:00Z")])},
        since_cursor="2026-01-03T00:00:00Z",
    )

    assert outcome.items_processed == 0
    assert outcome.next_cursor == "2026-01-03T00:00:00Z"


def test_page_size_and_page_budget_defaults_and_pass_through() -> None:
    seen: list[int] = []

    def fetch_page(*, cursor: str | None, page_size: int) -> httpx.Response | None:
        seen.append(page_size)
        n = len(seen)
        return _token_page([_item(n, "2026-01-01T00:00:00Z")], f"t{n}")

    def parse_page(response: httpx.Response, *, cursor: str | None) -> walk.Page:
        body = response.json()
        return walk.Page(items=body["issues"], next_cursor=body.get("nextPageToken"))

    def call(**kwargs: Any) -> SyncOutcome:
        seen.clear()
        return walk.walk_paginated_resource(
            resource_type="work_item",
            provider_label="P",
            resource_label="r",
            since_cursor=None,
            resume_cursor=None,
            start_cursor=None,
            apply_watermark_stop=False,
            fetch_page=fetch_page,
            parse_page=parse_page,
            extract_timestamp=_ts,
            upsert=lambda item: None,
            **kwargs,
        )

    outcome = call()
    assert seen == [100] * 10  # defaults: page_size=100, max_pages_per_call=10
    assert outcome.backfill_resume_cursor == "t10"

    call(page_size=37, max_pages_per_call=2)
    assert seen == [37, 37]


def test_link_header_page_number_defaults_to_the_first_page() -> None:
    assert walk.link_header_page_number(None) == 1
    assert walk.link_header_page_number("") == 1
    assert walk.link_header_page_number("7") == 7
