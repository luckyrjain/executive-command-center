"""Shared bounded-single-retry envelope -- extracted from `github_adapter.
py`/`gitlab_adapter.py`/`jira_adapter.py`/`datadog_adapter.py`, each of
which reimplemented the identical outer control flow (one rate-limit
check, one bounded wait, one retry, give up on a still-limited retry) in
their own `_request_with_rate_limit_retry` (Loop 2 architecture review's
"rate-limit retry divergence across 4 API adapters" finding). Only
*detection* of rate-limiting and *how long to wait* genuinely differ per
provider (GitHub also treats `403` + `X-RateLimit-Remaining: 0` as
rate-limited and falls back from `Retry-After` to `X-RateLimit-Reset`;
GitLab/Jira key off `Retry-After` alone; Datadog keys off `X-RateLimit-
Reset` alone) -- each adapter keeps its own `is_rate_limited`/`wait_
seconds` predicates and its own request-wrapping (for its own `RuntimeError`
message text), and passes them in here.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx


def bounded_single_retry(
    make_request: Callable[[], httpx.Response],
    *,
    is_rate_limited: Callable[[httpx.Response], bool],
    wait_seconds: Callable[[httpx.Response], float | None],
    max_wait_seconds: float,
    sleep: Callable[[float], None],
) -> httpx.Response | None:
    """Returns `None` only when rate-limited beyond the bounded wait --
    callers treat that as "give up for now, resume next sync call." A
    second rate-limit hit right after the wait is never trusted as a
    normal response (must not fall through to a caller's own `status_code
    != 200` branch, which would raise instead of degrading to the same
    clean `partial` outcome a first hit gets) -- one retry is the bound;
    a still-limited retry gives up exactly like the first check does.
    """
    response = make_request()
    if not is_rate_limited(response):
        return response

    wait = wait_seconds(response)
    if wait is None or wait > max_wait_seconds:
        return None
    sleep(wait)

    retry_response = make_request()
    if is_rate_limited(retry_response):
        return None
    return retry_response
