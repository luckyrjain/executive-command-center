from collections import defaultdict
from collections.abc import Awaitable, Callable

from ecc.platform.events.contracts import EventEnvelope

EventHandler = Callable[[EventEnvelope], Awaitable[None]]


class InProcessEventBus:
    """Replaceable Phase 0 event bus contract.

    Durable outbox/inbox persistence is introduced by the foundation migration;
    this class owns only in-process dispatch.
    """

    def __init__(self) -> None:
        self._handlers: dict[str, list[EventHandler]] = defaultdict(list)

    def subscribe(self, event_type: str, handler: EventHandler) -> None:
        self._handlers[event_type].append(handler)

    async def publish(self, event: EventEnvelope) -> None:
        for handler in self._handlers[event.event_type]:
            await handler(event)
