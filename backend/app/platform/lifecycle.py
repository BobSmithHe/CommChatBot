from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class BackgroundService:
    name: str
    run: Callable[[], Awaitable[None]]


class PlatformRuntime:
    """Owns platform background-service startup and deterministic shutdown."""

    def __init__(self, services: list[BackgroundService] | None = None) -> None:
        self.services = list(services or [])
        self._tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        if self._tasks:
            return
        self._tasks = [
            asyncio.create_task(service.run(), name=f"platform:{service.name}")
            for service in self.services
        ]

    async def stop(self) -> None:
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
