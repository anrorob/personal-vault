import asyncio
import inspect

import pytest

from app.incoming import complete_arrival_hall_publication
from app.main import run_vault_master_worker
from app.vault_master_work_wake import VaultMasterWorkWake


def test_signal_wakes_an_idle_worker_without_waiting_for_poll_timeout() -> None:
    async def scenario() -> None:
        wake = VaultMasterWorkWake()
        seen = wake.generation()
        waiter = asyncio.create_task(wake.wait_for_work(seen, 60))
        await asyncio.sleep(0)
        wake.signal_work_available()
        assert await asyncio.wait_for(waiter, timeout=0.1) > seen

    asyncio.run(scenario())


def test_poll_timeout_remains_the_worker_fallback() -> None:
    async def scenario() -> None:
        wake = VaultMasterWorkWake()
        seen = wake.generation()
        assert await wake.wait_for_work(seen, 0) == seen

    asyncio.run(scenario())


def test_repeated_signals_coalesce_into_one_pending_wake() -> None:
    async def scenario() -> None:
        wake = VaultMasterWorkWake()
        seen = wake.generation()
        waiter = asyncio.create_task(wake.wait_for_work(seen, 60))
        await asyncio.sleep(0)
        for _ in range(9):
            wake.signal_work_available()
        assert await asyncio.wait_for(waiter, timeout=0.1) == seen + 9
        assert await wake.wait_for_work(seen + 9, 0) == seen + 9

    asyncio.run(scenario())


def test_wait_cancels_promptly_for_worker_shutdown() -> None:
    async def scenario() -> None:
        wake = VaultMasterWorkWake()
        waiter = asyncio.create_task(wake.wait_for_work(wake.generation(), 60))
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        wake.detach_worker_loop()

    asyncio.run(scenario())


def test_wake_preserves_existing_worker_priority_order() -> None:
    worker_source = inspect.getsource(run_vault_master_worker)
    publication_source = inspect.getsource(complete_arrival_hall_publication)
    assert worker_source.index("process_next_ingestion_ai_job") < worker_source.index(
        "process_autopilot_batch"
    )
    assert "process_autopilot_batch" not in publication_source
