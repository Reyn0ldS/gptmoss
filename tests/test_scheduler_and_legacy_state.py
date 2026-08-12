import pytest

from gptmoss.core import Scheduler, StateEngine


@pytest.mark.asyncio
async def test_scheduler_orders_cancels_and_runs_due_jobs():
    now = [100.0]
    scheduler = Scheduler(clock=lambda: now[0])
    calls = []
    later = scheduler.schedule(lambda: calls.append("later"), delay=10, job_id="later")
    scheduler.schedule(lambda: calls.append("first"), delay=1, job_id="first")
    cancelled = scheduler.schedule(lambda: calls.append("cancelled"), delay=1)
    assert scheduler.cancel(cancelled)
    assert not scheduler.cancel("missing")

    assert await scheduler.run_due(now=101) == ["first"]
    assert calls == ["first"]
    assert scheduler.pending() == [{"job_id": later, "run_at": 110.0, "attempts": 0}]
    assert await scheduler.run_due(now=110) == ["later"]


@pytest.mark.asyncio
async def test_scheduler_retries_an_explicit_failure():
    scheduler = Scheduler(clock=lambda: 10)
    attempts = []

    def flaky():
        attempts.append(len(attempts) + 1)
        if len(attempts) == 1:
            raise RuntimeError("temporary")

    scheduler.schedule(flaky, max_retries=1, retry_delay=2, job_id="flaky")
    assert await scheduler.run_due(now=10) == []
    assert scheduler.pending()[0]["run_at"] == 12
    assert await scheduler.run_due(now=12) == ["flaky"]
    assert attempts == [1, 2]


def test_legacy_state_partitions_are_explicit_but_backward_compatible():
    state = StateEngine()
    with pytest.warns(DeprecationWarning, match="project configuration"):
        assert state.get_workspace("legacy").cwd == "."
    with pytest.warns(DeprecationWarning, match="governed memory"):
        assert state.get_knowledge("legacy").facts == []
    with pytest.warns(DeprecationWarning, match="scoped governed memory"):
        assert state.get_user("legacy").user_id == "legacy"
