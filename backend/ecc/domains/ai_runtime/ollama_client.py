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
"""

import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass

import httpx
import ollama

DEFAULT_OLLAMA_HOST = "http://127.0.0.1:11434"

# design doc Decision 5's budget table: "Per-model-call timeout | 20s".
DEFAULT_PER_MODEL_CALL_TIMEOUT_SECONDS = 20.0


class OllamaCallTimeout(Exception):
    """Raised when a `generate()` call exceeds its per-model-call deadline
    (design doc Decision 5) -- either a single slow network read or the
    cumulative wall-clock time across many chunks.
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

    def generate(self, prompt: str, model_id: str, max_tokens: int) -> Iterator[Chunk]:
        """Stream a `generate` call, yielding one `Chunk` per fragment.

        Raises `OllamaCallTimeout` if the call runs past its per-model-call
        deadline (checked before yielding each chunk, so a caller never
        observes a chunk that arrived after the deadline), and
        `OllamaCallFailed` on a transport/response error. Iterating to
        exhaustion (the final `chunk.done`) or letting the generator be
        garbage-collected/closed early both release the underlying HTTP
        stream -- see the module docstring.
        """
        deadline = self._clock() + self._timeout_seconds
        try:
            stream = self._client.generate(
                model=model_id,
                prompt=prompt,
                stream=True,
                options={"num_predict": max_tokens},
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
