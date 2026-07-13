from collections import defaultdict
from collections.abc import Awaitable, Callable

from ecc.platform.events.contracts import EventEnvelope

EventHandler = Callable[[EventEnvelope], Awaitable[None]]


class NonDurableInProcessEventBus:
    """Test and development adapter for synchronous in-process dispatch.

    This adapter provides no persistence, retry, deduplication, inbox, outbox,
    or dead-letter guarantees. Production code that requires durable delivery
    must use a transactional outbox-backed implementation.
    """

    def __init__(self) -> None:
        self._handlers: dict[str, list[EventHandler]] = defaultdict(list)

    def subscribe(self, event_type: str, handler: EventHandler) -> None:
        self._handlers[event_type].append(handler)

    async def publish(self, event: EventEnvelope) -> None:
        for handler in self._handlers[event.event_type]:
            await handler(event)


InProcessEventBus = NonDurableInProcessEventBus
