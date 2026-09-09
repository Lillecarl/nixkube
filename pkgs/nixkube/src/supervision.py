# SPDX-License-Identifier: MIT

"""Crash-loop supervision for long-running coroutines.

Wraps coroutine factories with restart logic and crash-loop detection.
A crash loop is defined as max_restarts failures within a sliding time window.
When detected, CrashLoopError is raised, which propagates through asyncio.gather()
to cancel siblings and exit the process (Kubernetes restarts the pod with backoff).
"""

import asyncio
import time
from collections import deque
from collections.abc import Callable, Coroutine
from typing import Any

import structlog

logger = structlog.get_logger("nixkube.supervision")


class CrashLoopError(Exception):
    """Raised when a supervised task has crashed too many times within the window."""


class CrashLoopTracker:
    """Tracks restart timestamps and detects crash loops.

    A crash loop is detected when max_restarts restarts occur within window seconds.
    """

    def __init__(
        self, max_restarts: int = 5, window: float = 60.0, name: str = ""
    ) -> None:
        self.max_restarts = max_restarts
        self.window = window
        self.name = name
        self._timestamps: deque[float] = deque()

    def record_and_check(self) -> None:
        """Record a restart and raise CrashLoopError if the crash loop threshold is exceeded."""
        now = time.monotonic()
        self._timestamps.append(now)
        # Prune timestamps outside the window
        cutoff = now - self.window
        while self._timestamps and self._timestamps[0] < cutoff:
            self._timestamps.popleft()
        if len(self._timestamps) >= self.max_restarts:
            raise CrashLoopError(
                f"{self.name!r}: crashed {len(self._timestamps)} times "
                f"in {self.window:.0f}s (max {self.max_restarts})"
            )


async def supervised(
    factory: Callable[[], Coroutine[Any, Any, None]],
    name: str,
    *,
    max_restarts: int = 5,
    window: float = 60.0,
) -> None:
    """Run a coroutine factory in a supervised restart loop.

    Calls factory() to create a new coroutine each iteration. If the coroutine
    raises or returns, records the failure and restarts after a 1s backoff.
    CancelledError propagates immediately without recording (clean shutdown).
    CrashLoopError propagates when the threshold is exceeded.
    """
    tracker = CrashLoopTracker(max_restarts=max_restarts, window=window, name=name)
    log = logger.bind(service=name)

    while True:
        try:
            await factory()
            # Coroutine returned without raising — treat as unexpected exit
            log.warning("service_exited_unexpectedly")
        except asyncio.CancelledError:
            raise
        except CrashLoopError:
            raise
        except Exception:
            log.error("service_crashed", exc_info=True)

        tracker.record_and_check()
        log.info("service_restarting", backoff_seconds=1)
        await asyncio.sleep(1)
