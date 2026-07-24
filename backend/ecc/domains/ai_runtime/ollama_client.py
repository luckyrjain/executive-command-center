"""The sole module in this codebase that imports the `ollama` Python
package (`ADR-0004`, `ADR-0007`, `ADR-0012`). Every other module reaches a
model exclusively through `router.py` and, once Task 4's `runtime.py` ships,
this adapter -- never `ollama` directly.

`OllamaAdapter` wraps Ollama's **streaming** `generate` HTTP call behind a
small typed interface (`Chunk`), specifically so a budget/operator-triggered
cancellation (Task 3's `CancellationToken`) can close the stream mid-
generation instead of waiting on a non-preemptible blocking call to return
(design doc Decision 5). `generate()` itself is a generator: closing it
(`generator.close()`) propagates a `GeneratorExit` through the `finally`
below, closing the underlying Ollama HTTP stream -- the mechanism Task 3
threads a `CancellationToken` into, not reimplemented here.

Enforces the design doc's 20s per-model-call timeout two ways: the
underlying `httpx` client's own request timeout bounds any single network
read, and a wall-clock deadline checked between yielded chunks bounds the
*whole* call even if no single read is individually slow (many small
chunks that only cumulatively exceed the budget). Both surface as
`OllamaCallTimeout`, never a raw `httpx` exception, so callers depend on one
typed error regardless of which guard fired.

`generate()` also accepts an optional `budgets.py:CancellationToken`
(Task 3), checked at the exact same point, on the exact same per-chunk
cadence, as the wall-clock deadline check above -- a cancellation raises
`OllamaCallCancelled`, which unwinds through the same `finally: close()`
cleanup the deadline/exhaustion paths already use, so no second
stream-closing mechanism exists. Passing no token (the default) leaves
every Task 1 caller of `generate()` unchanged.
"""

import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass

import httpx
import ollama

from .budgets import CancellationToken

DEFAULT_OLLAMA_HOST = "http://127.0.0.1:11434"

# design doc Decision 5's budget table: "Per-model-call timeout | 20s".
DEFAULT_PER_MODEL_CALL_TIMEOUT_SECONDS = 20.0


class OllamaCallTimeout(Exception):
    """Raised when a `generate()` call exceeds its per-model-call deadline
    (design doc Decision 5) -- either a single slow network read or the
    cumulative wall-clock time across many chunks.
    """


class OllamaCallCancelled(Exception):
    """Raised when a `generate()` call's `CancellationToken` is cancelled
    mid-stream (design doc Decision 5's "Cancellation" row, Task 3). A
    caller that catches this transitions the run to `cancelled` --
    never `completed`, and distinct from `OllamaCallTimeout`/
    `OllamaCallFailed`, which are budget/provider-error outcomes, not an
    operator/budget-triggered cancellation.
    """


class OllamaCallFailed(Exception):
    """Raised when Ollama returns an error response or the connection
    fails, wrapping `ollama.ResponseError`/`ConnectionError` behind this
    module's own typed exception so no caller needs to import `ollama`
    itself to handle a failure.
    """


@dataclass(frozen=True, slots=True)
class Chunk:
    """One fragment of a streamed `generate` response. `text` is the
    fragment content (empty on the final chunk in some Ollama responses);
    `done=True` marks the last chunk. `eval_count`/`prompt_eval_count` are
    only populated on the final chunk, matching Ollama's own API shape --
    kept here (not the raw response object) so no caller needs to import
    `ollama`'s response types either.
    """

    text: str
    done: bool
    eval_count: int | None = None
    prompt_eval_count: int | None = None


class OllamaAdapter:
    """Typed adapter over the `ollama` Python client's list/generate calls.

    `transport` is exposed purely for testing: `ollama.Client` passes any
    extra keyword arguments straight through to the underlying `httpx`
    client it constructs, so tests inject an `httpx.MockTransport` here to
    exercise real request/response parsing against a fully deterministic,
    in-process fake server -- no live Ollama, no new HTTP-mocking
    dependency (this codebase's own HTTPX usage, `tests/
    test_production_security.py`, has no existing mocking-library
    convention to match; `httpx.MockTransport` is part of `httpx` itself,
    already a pinned dependency, so this adds nothing new).

    `clock` is exposed purely for testing the wall-clock timeout guard
    without a real multi-second sleep.
    """

    def __init__(
        self,
        host: str = DEFAULT_OLLAMA_HOST,
        *,
        timeout_seconds: float = DEFAULT_PER_MODEL_CALL_TIMEOUT_SECONDS,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._timeout_seconds = timeout_seconds
        self._clock = clock
        client_kwargs: dict[str, object] = {"timeout": timeout_seconds}
        if transport is not None:
            client_kwargs["transport"] = transport
        self._client = ollama.Client(host=host, **client_kwargs)

    def list_models(self) -> list[str]:
        """List model tags Ollama currently reports as available locally."""
        try:
            response = self._client.list()
        except ollama.ResponseError as exc:
            raise OllamaCallFailed(str(exc)) from exc
        except ConnectionError as exc:
            raise OllamaCallFailed(str(exc)) from exc
        return [model.model for model in response.models if model.model is not None]

    def generate(
        self,
        prompt: str,
        model_id: str,
        max_tokens: int,
        *,
        cancellation_token: CancellationToken | None = None,
    ) -> Iterator[Chunk]:
        """Stream a `generate` call, yielding one `Chunk` per fragment.

        Raises `OllamaCallTimeout` if the call runs past its per-model-call
        deadline (checked before yielding each chunk, so a caller never
        observes a chunk that arrived after the deadline), `OllamaCallFailed`
        on a transport/response error, and -- when `cancellation_token` is
        supplied and gets cancelled mid-stream -- `OllamaCallCancelled`,
        checked at the same point and cadence as the deadline (Task 3).
        Iterating to exhaustion (the final `chunk.done`), letting the
        generator be garbage-collected/closed early, or either exception
        path above all release the underlying HTTP stream -- see the
        module docstring. `cancellation_token` defaults to `None`, so
        every existing caller (Task 1) is unaffected.

        `temperature: 0` and a fixed `seed` make decoding greedy/
        deterministic instead of Ollama's non-zero-temperature default --
        required by the design doc's own non-functional requirement
        ("Evaluation runs are reproducible from stored versions/hashes"),
        which stochastic sampling violates outright: the same prompt could
        legitimately produce a different response, and a different
        schema-validity/grounding outcome, on every run. Greedy decoding is
        also the standard mitigation for a small instruct model's structured-
        output reliability (malformed JSON, over-length responses,
        citing the wrong token) -- observed directly in this activation's
        live-Ollama evaluation job (`ollama-evaluation` CI): schema_invalid
        and grounding failures at non-zero temperature that a fixed,
        zero-temperature decode should reduce, though this codebase's
        sandboxed CI is the only place that claim can actually be checked
        against the real model.
        """
        deadline = self._clock() + self._timeout_seconds
        try:
            stream = self._client.generate(
                model=model_id,
                prompt=prompt,
                stream=True,
                options={"num_predict": max_tokens, "temperature": 0, "seed": 0},
            )
        except ollama.ResponseError as exc:
            raise OllamaCallFailed(str(exc)) from exc
        except ConnectionError as exc:
            raise OllamaCallFailed(str(exc)) from exc

        try:
            while True:
                if self._clock() >= deadline:
                    raise OllamaCallTimeout(
                        f"model call to {model_id!r} exceeded the "
                        f"{self._timeout_seconds}s per-model-call timeout"
                    )
                if cancellation_token is not None and cancellation_token.is_cancelled():
                    raise OllamaCallCancelled(
                        f"model call to {model_id!r} was cancelled ({cancellation_token.reason})"
                    )
                try:
                    part = next(stream)
                except StopIteration:
                    return
                except httpx.TimeoutException as exc:
                    raise OllamaCallTimeout(
                        f"model call to {model_id!r} timed out waiting for a response "
                        f"chunk (per-model-call timeout {self._timeout_seconds}s)"
                    ) from exc
                except ollama.ResponseError as exc:
                    raise OllamaCallFailed(str(exc)) from exc

                yield Chunk(
                    text=part.response or "",
                    done=bool(part.done),
                    eval_count=part.eval_count,
                    prompt_eval_count=part.prompt_eval_count,
                )
                if part.done:
                    return
        finally:
            close = getattr(stream, "close", None)
            if callable(close):
                close()
