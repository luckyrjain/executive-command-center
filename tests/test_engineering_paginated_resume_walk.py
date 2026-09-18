"""`paginated_resume_walk.walk_paginated_resource` -- the page-walk/resume-
cursor/watermark loop extracted from `github_adapter.py`'s and
`gitlab_adapter.py`'s own byte-identical `_sync_repositories` methods
(architecture review, 2026-09-18; see that module's own docstring for the
full duplication history). Pure unit tests against fake `fetch_page`/
`extract_timestamp`/`upsert` callables -- no HTTP, no database, no
Postgres -- since the whole point of the extraction is that these
invariants no longer need either to exercise directly.

Covers exactly the invariants `93ceaf3` ("make backfill genuinely
resumable across GitHub/GitLab/Jira") found broken and fixed once in
GitHub's copy, then had to re-derive in GitLab's: resuming a backfill
from a page other than 1, the page-vs-budget counter split, the
per-call page bound reporting `partial` (not a silent `succeeded`) with
more pages remaining, and `next_cursor` only ever being reported back on
the very first backfill call.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx
import pytest

from ecc.domains.engineering.paginated_resume_walk import walk_paginated_resource


def _page_response(
    items: list[dict[str, object]], *, next_page: int | None = None
) -> httpx.Response:
    headers = {}
    if next_page is not None:
        headers["Link"] = f'<https://example.test/items?page={next_page}>; rel="next"'
    return httpx.Response(200, json=items, headers=headers)


def _item(id_: int, updated_at: str | None) -> dict[str, object]:
    return {"id": id_, "updated_at": updated_at}


def test_single_page_reports_succeeded_and_the_newest_timestamp() -> None:
    upserted: list[Mapping[str, Any]] = []
    pages = {
        1: _page_response([_item(1, "2026-01-01T00:00:00Z"), _item(2, "2026-01-02T00:00:00Z")])
    }

    outcome = walk_paginated_resource(
        resource_type="repository",
        provider_label="TestProvider",
        resource_label="repository",
        since_cursor=None,
        resume_cursor=None,
        apply_watermark_stop=True,
        fetch_page=lambda page, page_size: pages[page],
        extract_timestamp=lambda item: item["updated_at"],
        upsert=upserted.append,
    )

    assert outcome.status == "succeeded"
    assert outcome.items_processed == 2
    assert [item["id"] for item in upserted] == [1, 2]
    assert outcome.next_cursor == "2026-01-02T00:00:00Z"
    assert outcome.backfill_resume_cursor is None


def test_pagination_follows_the_link_header_across_multiple_pages() -> None:
    upserted: list[Mapping[str, Any]] = []
    pages = {
        1: _page_response([_item(1, "2026-01-01T00:00:00Z")], next_page=2),
        2: _page_response([_item(2, "2026-01-02T00:00:00Z")]),
    }

    outcome = walk_paginated_resource(
        resource_type="repository",
        provider_label="TestProvider",
        resource_label="repository",
        since_cursor=None,
        resume_cursor=None,
        apply_watermark_stop=True,
        fetch_page=lambda page, page_size: pages[page],
        extract_timestamp=lambda item: item["updated_at"],
        upsert=upserted.append,
    )

    assert outcome.status == "succeeded"
    assert [item["id"] for item in upserted] == [1, 2]
    assert outcome.next_cursor == "2026-01-02T00:00:00Z"


def test_watermark_stop_only_upserts_items_newer_than_since_cursor() -> None:
    upserted: list[Mapping[str, Any]] = []
    # Newest-first, as every real provider returns them.
    pages = {
        1: _page_response(
            [_item(2, "2026-01-05T00:00:00Z"), _item(1, "2026-01-01T00:00:00Z")], next_page=2
        )
    }

    outcome = walk_paginated_resource(
        resource_type="repository",
        provider_label="TestProvider",
        resource_label="repository",
        since_cursor="2026-01-01T00:00:00Z",
        resume_cursor=None,
        apply_watermark_stop=True,
        fetch_page=lambda page, page_size: pages[page],
        extract_timestamp=lambda item: item["updated_at"],
        upsert=upserted.append,
    )

    assert outcome.status == "succeeded"
    assert [item["id"] for item in upserted] == [2]
    assert outcome.next_cursor == "2026-01-05T00:00:00Z"


def test_backfill_resumes_from_the_given_page_not_page_1() -> None:
    """The exact bug `93ceaf3` fixed: resuming a backfill call must
    request the resume page, not restart at page 1, and the page-vs-
    budget counter must be independent of the loop's own bound.
    """
    upserted: list[Mapping[str, Any]] = []
    requested_pages: list[int] = []
    pages = {11: _page_response([_item(1, "2026-01-01T00:00:00Z")])}

    def fetch_page(page: int, page_size: int) -> httpx.Response:
        requested_pages.append(page)
        return pages[page]

    outcome = walk_paginated_resource(
        resource_type="repository",
        provider_label="TestProvider",
        resource_label="repository",
        since_cursor=None,
        resume_cursor="11",
        apply_watermark_stop=False,
        fetch_page=fetch_page,
        extract_timestamp=lambda item: item["updated_at"],
        upsert=upserted.append,
        max_pages_per_call=10,
    )

    assert requested_pages == [11]
    assert outcome.status == "succeeded"
    # A resumed backfill call never reports next_cursor -- only the very
    # first backfill call (resume_cursor is None) is allowed to, since a
    # page deep into the walk can't teach the watermark anything newer.
    assert outcome.next_cursor is None


def test_page_cap_reports_partial_with_the_resume_page_preserved() -> None:
    call_count = 0

    def fetch_page(page: int, page_size: int) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return _page_response([_item(page, "2026-01-01T00:00:00Z")], next_page=page + 1)

    outcome = walk_paginated_resource(
        resource_type="repository",
        provider_label="TestProvider",
        resource_label="repository",
        since_cursor=None,
        resume_cursor=None,
        apply_watermark_stop=False,
        fetch_page=fetch_page,
        extract_timestamp=lambda item: item["updated_at"],
        upsert=lambda item: None,
        max_pages_per_call=3,
    )

    assert call_count == 3
    assert outcome.status == "partial"
    assert outcome.items_processed == 3
    # page advanced past the last page actually fetched (3 -> 4).
    assert outcome.backfill_resume_cursor == "4"
    assert "3-page per-call bound" in (outcome.error_summary or "")
    # This is the *first* backfill call (resume_cursor=None) -- unlike a
    # resumed call, it must still report next_cursor.
    assert outcome.next_cursor == "2026-01-01T00:00:00Z"


def test_backfill_ignores_a_leftover_since_cursor() -> None:
    """`apply_watermark_stop=False` must stay purely page-driven even if a
    caller passes a non-`None` `since_cursor` -- a regression here (e.g. an
    accidentally-dropped `and not apply_watermark_stop` guard) would still
    pass every other test in this file, since they all pair
    `apply_watermark_stop=False` with `since_cursor=None`.
    """
    upserted: list[Mapping[str, Any]] = []
    pages = {1: _page_response([_item(1, "2020-01-01T00:00:00Z")])}

    outcome = walk_paginated_resource(
        resource_type="repository",
        provider_label="TestProvider",
        resource_label="repository",
        since_cursor="2026-01-01T00:00:00Z",  # would stop everything if honored
        resume_cursor=None,
        apply_watermark_stop=False,
        fetch_page=lambda *, page, page_size: pages[page],
        extract_timestamp=lambda item: item["updated_at"],
        upsert=upserted.append,
    )

    assert outcome.status == "succeeded"
    assert [item["id"] for item in upserted] == [1]


def test_empty_first_page_succeeds_with_no_items() -> None:
    # `next_page=2` (a Link header pointing past this page) makes this a
    # real regression guard for `if not items: break`, not just a proxy
    # for "no Link header" -- if that guard were dropped, the empty `for`
    # loop would no-op, `"next" not in response.links` would be False
    # (page 2 exists), and the walk would try page 2, hitting the
    # KeyError below instead of stopping cleanly at page 1.
    pages = {1: _page_response([], next_page=2)}

    outcome = walk_paginated_resource(
        resource_type="repository",
        provider_label="TestProvider",
        resource_label="repository",
        since_cursor=None,
        resume_cursor=None,
        apply_watermark_stop=True,
        fetch_page=lambda *, page, page_size: pages[page],
        extract_timestamp=lambda item: item["updated_at"],
        upsert=lambda item: None,
    )

    assert outcome.status == "succeeded"
    assert outcome.items_processed == 0
    assert outcome.next_cursor is None


def test_item_with_no_timestamp_is_still_upserted_and_does_not_crash() -> None:
    upserted: list[Mapping[str, Any]] = []
    pages = {1: _page_response([_item(1, None), _item(2, "2026-01-01T00:00:00Z")])}

    outcome = walk_paginated_resource(
        resource_type="repository",
        provider_label="TestProvider",
        resource_label="repository",
        since_cursor="2020-01-01T00:00:00Z",
        resume_cursor=None,
        apply_watermark_stop=True,
        fetch_page=lambda *, page, page_size: pages[page],
        extract_timestamp=lambda item: item["updated_at"],
        upsert=upserted.append,
    )

    assert outcome.status == "succeeded"
    # A missing timestamp never matches the watermark-stop condition, so
    # the item is upserted like any other, and never overwrites
    # newest_updated_at with None.
    assert [item["id"] for item in upserted] == [1, 2]
    assert outcome.next_cursor == "2026-01-01T00:00:00Z"


def test_rate_limit_reports_partial_and_preserves_the_current_page() -> None:
    outcome = walk_paginated_resource(
        resource_type="repository",
        provider_label="TestProvider",
        resource_label="repository",
        since_cursor=None,
        resume_cursor="5",
        apply_watermark_stop=False,
        fetch_page=lambda page, page_size: None,
        extract_timestamp=lambda item: item["updated_at"],
        upsert=lambda item: None,
    )

    assert outcome.status == "partial"
    assert outcome.backfill_resume_cursor == "5"
    assert outcome.next_cursor is None
    assert "rate limit" in (outcome.error_summary or "").lower()


def test_non_200_response_raises() -> None:
    with pytest.raises(RuntimeError, match="TestProvider repository list failed with status 500"):
        walk_paginated_resource(
            resource_type="repository",
            provider_label="TestProvider",
            resource_label="repository",
            since_cursor=None,
            resume_cursor=None,
            apply_watermark_stop=True,
            fetch_page=lambda page, page_size: httpx.Response(500),
            extract_timestamp=lambda item: item["updated_at"],
            upsert=lambda item: None,
        )
