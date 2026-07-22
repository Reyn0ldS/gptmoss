import asyncio
import inspect
import time
import uuid
import logging
from typing import Dict, Any, Callable, List, Union, Coroutine, Awaitable
from pydantic import BaseModel, Field

logger = logging.getLogger("gptmoss.event_bus")

class Event(BaseModel):
    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    type: str
    payload: Dict[str, Any] = Field(default_factory=dict)
    timestamp: float = Field(default_factory=time.time)

# A subscription callback can be a sync/async callable taking an Event
EventCallback = Callable[[Event], Union[None, Awaitable[None]]]

class EventBus:
    def __init__(self):
        self._subscribers: Dict[str, List[EventCallback]] = {}
        self._all_subscribers: List[EventCallback] = []

    def subscribe(self, event_type: str, callback: EventCallback):
        """Subscribe to a specific event type."""
        if event_type not in self._subscribers:
            self._subscribers[event_type] = []
        self._subscribers[event_type].append(callback)
        logger.debug(f"Subscribed callback to event type: {event_type}")

    def subscribe_all(self, callback: EventCallback):
        """Subscribe to all events emitted on the bus."""
        self._all_subscribers.append(callback)
        logger.debug("Subscribed callback to all events")

    def unsubscribe(self, event_type: str, callback: EventCallback):
        """Unsubscribe from a specific event type."""
        if event_type in self._subscribers and callback in self._subscribers[event_type]:
            self._subscribers[event_type].remove(callback)

    async def publish(self, event: Event):
        """
        Publish an event to all subscribers asynchronously.
        Fires all callbacks concurrently in the background.
        """
        callbacks = list(self._all_subscribers)
        if event.type in self._subscribers:
            callbacks.extend(self._subscribers[event.type])

        if not callbacks:
            return

        async def run_callback(cb: EventCallback):
            try:
                result = cb(event)
                if inspect.isawaitable(result):
                    await result
            except Exception as e:
                logger.error(f"Error in event callback {cb} for event {event.type}: {e}", exc_info=True)

        # Fire callbacks concurrently
        await asyncio.gather(*(run_callback(cb) for cb in callbacks), return_exceptions=True)
