"""Small building blocks the API clients share: values refreshed in the background, and
rates computed from monotonically increasing counters."""
from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Hashable
from typing import Any


class Refresher:
    """Keyed values that are slow or expensive to fetch (guest configs, ZFS pools, guest
    agents...). A poll asks for each one with a time budget: a fresh value comes back at once,
    a stale one triggers a refresh that the poll waits for only up to the budget. A refresh
    that takes longer keeps running in the background and is picked up by a later poll, so a
    slow endpoint never holds the dashboard up and is never cancelled halfway."""

    def __init__(self, retry_after_error_s: float = 30.0) -> None:
        self.retry_after_error_s = retry_after_error_s
        self._values: dict[Hashable, tuple[float, Any]] = {}
        self._errors: dict[Hashable, tuple[float, Exception]] = {}
        self._tasks: dict[Hashable, asyncio.Task] = {}

    async def get(self, key: Hashable, ttl: float, fetch: Callable[[], Awaitable[Any]],
                  budget: float, default: Any = None) -> Any:
        now = time.monotonic()
        ts, value = self._values.get(key, (None, default))
        if ts is not None and now - ts < ttl:
            return value
        err = self._errors.get(key)
        if err and now - err[0] < min(ttl, self.retry_after_error_s):
            return value
        task = self._tasks.get(key)
        if task is None:
            task = asyncio.create_task(self._run(key, fetch))
            self._tasks[key] = task
        await asyncio.wait({task}, timeout=max(0.0, budget))   # never cancels the refresh
        return self._values.get(key, (None, default))[1]

    async def _run(self, key: Hashable, fetch: Callable[[], Awaitable[Any]]) -> None:
        try:
            value = await fetch()
        except Exception as e:  # noqa: BLE001
            self._errors[key] = (time.monotonic(), e)
        else:
            self._values[key] = (time.monotonic(), value)
            self._errors.pop(key, None)
        finally:
            self._tasks.pop(key, None)

    def error(self, key: Hashable) -> Exception | None:
        """The last refresh's error, until a refresh succeeds."""
        err = self._errors.get(key)
        return err[1] if err else None

    def age(self, key: Hashable) -> float | None:
        ts = self._values.get(key, (None, None))[0]
        return None if ts is None else time.monotonic() - ts

    def forget(self, key: Hashable) -> None:
        self._values.pop(key, None)
        self._errors.pop(key, None)

    def keys(self) -> list[Hashable]:
        return list(self._values)

    async def close(self) -> None:
        tasks = list(self._tasks.values())
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


class Rates:
    """Per-second rates from counters, e.g. bytes in/out. Feeding the same sample twice (a
    cached response) keeps the last rate instead of reporting zero."""

    def __init__(self, scale: float = 1.0) -> None:
        self.scale = scale
        self._prev: dict[Hashable, tuple[float, tuple[float, ...]]] = {}
        self._last: dict[Hashable, tuple[float, ...]] = {}

    def update(self, key: Hashable, ts: float, values: tuple[float, ...]) -> tuple[float, ...]:
        prev = self._prev.get(key)
        if prev is None or ts > prev[0]:
            if prev is not None and len(prev[1]) == len(values):
                dt = max(0.5, ts - prev[0])
                # A counter that went backwards was reset (reboot, migration): no rate this time.
                self._last[key] = tuple(max(0.0, v - p) * self.scale / dt for v, p in zip(values, prev[1]))
            self._prev[key] = (ts, values)
        return self._last.get(key, tuple(0.0 for _ in values))


class LastGood:
    """The last successful value of each section of a poll, so a short outage shows slightly
    old data (flagged as stale) instead of an empty dashboard."""

    def __init__(self, max_age_s: float) -> None:
        self.max_age_s = max_age_s
        self._values: dict[Hashable, tuple[float, Any]] = {}

    def put(self, key: Hashable, value: Any) -> None:
        self._values[key] = (time.monotonic(), value)

    def get(self, key: Hashable) -> Any:
        ts, value = self._values.get(key, (0.0, None))
        return value if value is not None and time.monotonic() - ts <= self.max_age_s else None
