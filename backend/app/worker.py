from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

from .core.task_executor import execute_queued_task
from .core.task_queue import task_queue
from .infra.config import get_settings
from .infra.database import init_db


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
    if initialize:
        init_db()
    recovered = task_queue.wake_recovered()
    concurrency = max(1, get_settings().agent_worker_concurrency)
    logger = logging.getLogger(__name__)
    logger.info("Agent worker ready; queued=%s concurrency=%s", recovered, concurrency)
    jobs: set[asyncio.Task[bool]] = set()
    try:
        while True:
            _report_finished(jobs)
            if len(jobs) >= concurrency:
                await asyncio.wait(jobs, return_when=asyncio.FIRST_COMPLETED)
                continue
            task_id = await asyncio.to_thread(task_queue.next, 2)
            if not task_id:
                continue
            _start_job(jobs, execute_queued_task(task_id))
            # Let execute_queued_task atomically claim the queued database row
            # before another queue read can return the same durable task.
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
