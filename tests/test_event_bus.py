import pytest
from gptmoss.core.event_bus import EventBus, Event

@pytest.mark.asyncio
async def test_event_bus_publish_subscribe():
    bus = EventBus()
    events_received = []

    async def on_test_event(event: Event):
        events_received.append(event)

    # Subscribe specific
    bus.subscribe("TestEvent", on_test_event)

    # Publish matching event
    ev1 = Event(type="TestEvent", payload={"value": "hello"})
    await bus.publish(ev1)

    assert len(events_received) == 1
    assert events_received[0].type == "TestEvent"
    assert events_received[0].payload["value"] == "hello"

    # Publish non-matching event
    ev2 = Event(type="OtherEvent", payload={"value": "ignored"})
    await bus.publish(ev2)

    assert len(events_received) == 1

@pytest.mark.asyncio
async def test_event_bus_subscribe_all():
    bus = EventBus()
    all_events = []

    async def on_any_event(event: Event):
        all_events.append(event)

    bus.subscribe_all(on_any_event)

    await bus.publish(Event(type="EventA"))
    await bus.publish(Event(type="EventB"))

    assert len(all_events) == 2
    assert all_events[0].type == "EventA"
    assert all_events[1].type == "EventB"


@pytest.mark.asyncio
async def test_event_bus_subscriptions_are_idempotent_and_removable():
    bus = EventBus()
    events = []

    async def callback(event: Event):
        events.append(event.type)

    assert bus.subscribe_all(callback) is callback
    bus.subscribe_all(callback)
    bus.subscribe("Selected", callback)
    bus.subscribe("Selected", callback)
    assert bus.subscriber_count() == 2

    await bus.publish(Event(type="Selected"))
    assert events == ["Selected", "Selected"]
    assert bus.unsubscribe("Selected", callback)
    assert not bus.unsubscribe("Selected", callback)
    assert bus.unsubscribe_all(callback)
    assert not bus.unsubscribe_all(callback)
    assert bus.subscriber_count() == 0
