from __future__ import annotations

import asyncio

from app.worker import _start_job


def test_worker_jobs_do_not_block_behind_waiting_task() -> None:
    async def scenario() -> None:
        waiting_started = asyncio.Event()
        release_waiting = asyncio.Event()
        fast_finished = asyncio.Event()

        async def waiting_job() -> bool:
            waiting_started.set()
            await release_waiting.wait()
            return True

        async def fast_job() -> bool:
            fast_finished.set()
            return True

        jobs: set[asyncio.Task[bool]] = set()
        _start_job(jobs, waiting_job())
        await waiting_started.wait()
        _start_job(jobs, fast_job())

        await asyncio.wait_for(fast_finished.wait(), timeout=0.2)
        assert any(not job.done() for job in jobs)
        release_waiting.set()
        await asyncio.gather(*tuple(jobs))

    asyncio.run(scenario())
