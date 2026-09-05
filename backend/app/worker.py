from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

from .bootstrap import get_container
from .platform.services.task_queue import task_queue
from .extensions.builtin.memory import execute_memory_job, memory_job_queue
from .platform.database import init_db


def _report_finished(jobs: set[asyncio.Task[bool]]) -> None:
    logger = logging.getLogger(__name__)
    for job in tuple(jobs):
        if not job.done():
            continue
        jobs.discard(job)
        try:
            job.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Agent worker job crashed")


def _start_job(jobs: set[asyncio.Task[bool]], operation: Coroutine[Any, Any, bool]) -> None:
    job = asyncio.create_task(operation)
    jobs.add(job)
    job.add_done_callback(lambda _job: _report_finished(jobs))


async def run_worker(*, initialize: bool = True) -> None:
    container = get_container()
    if initialize:
        init_db()
    recovered = task_queue.wake_recovered()
    concurrency = max(1, container.settings.agent_worker_concurrency)
    logger = logging.getLogger(__name__)
    logger.info("Agent worker ready; queued=%s concurrency=%s", recovered, concurrency)
    jobs: set[asyncio.Task[bool]] = set()
    memory_runner = asyncio.create_task(run_memory_worker())
    try:
        while True:
            _report_finished(jobs)
            if len(jobs) >= concurrency:
                await asyncio.wait(jobs, return_when=asyncio.FIRST_COMPLETED)
                continue
            task_id = await asyncio.to_thread(task_queue.next, 2)
            if not task_id:
                continue
            _start_job(jobs, container.execute_task(task_id))
            # Let execute_queued_task atomically claim the queued database row
            # before another queue read can return the same durable task.
            await asyncio.sleep(0)
    finally:
        memory_runner.cancel()
        await asyncio.gather(memory_runner, return_exceptions=True)
        for job in jobs:
            job.cancel()
        if jobs:
            await asyncio.gather(*jobs, return_exceptions=True)


async def run_memory_worker() -> None:
    container = get_container()
    recovered = memory_job_queue.wake_recovered()
    concurrency = max(1, container.settings.memory_worker_concurrency)
    logger = logging.getLogger(__name__)
    logger.info("Memory worker ready; queued=%s concurrency=%s", recovered, concurrency)
    jobs: set[asyncio.Task[bool]] = set()
    try:
        while True:
            _report_finished(jobs)
            if len(jobs) >= concurrency:
                await asyncio.wait(jobs, return_when=asyncio.FIRST_COMPLETED)
                continue
            job_id = await asyncio.to_thread(memory_job_queue.next, 2)
            if not job_id:
                continue
            _start_job(jobs, execute_memory_job(job_id))
            await asyncio.sleep(0)
    finally:
        for job in jobs:
            job.cancel()
        if jobs:
            await asyncio.gather(*jobs, return_exceptions=True)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
