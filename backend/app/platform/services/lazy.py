from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any


class LazyService:
    """Compatibility proxy that avoids constructing services at import time.

    Production services are composed by ``bootstrap.container``. Legacy public
    module attributes remain available through this proxy without opening
    connections, files, PTYs, or threads merely because a module was imported.
    """

    def __init__(self, factory: Callable[[], Any]) -> None:
        self._factory = factory
        self._instance: Any | None = None
        self._lock = threading.Lock()

    def get(self) -> Any:
        if self._instance is None:
            with self._lock:
                if self._instance is None:
                    self._instance = self._factory()
        return self._instance

    def close_if_created(self) -> None:
        instance = self._instance
        if instance is not None and callable(close := getattr(instance, "close", None)):
            close()

    def __getattr__(self, name: str) -> Any:
        return getattr(self.get(), name)
