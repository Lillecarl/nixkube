# SPDX-License-Identifier: MIT

"""Async garbage collection loop for the Nix store.

Ports the logic from environments/modules/gc.nix:
1. Optionally copies all store paths to the cache over SSH.
2. Deletes store paths older than GC_KEEP_SECONDS.
3. Sleeps a randomised interval before repeating.
"""

import asyncio
import json
import random
import time

import structlog
from shellous import sh

from .cache import copy_to_cache
from .constants import GC_INTERVAL_SECONDS, GC_KEEP_SECONDS, PYNIXD_ENABLED
from .metrics import GC_CYCLE_DURATION, GC_CYCLES, GC_PATHS_DELETED

logger = structlog.get_logger("nixkube.gc")


async def gc_loop() -> None:
    """Run garbage collection on the local Nix store in a loop.

    Non-fatal: exceptions are logged as warnings and the loop continues.
    """
    while True:
        started = time.monotonic()
        try:
            await _run_gc_cycle()
        except asyncio.CancelledError:
            raise
        except Exception:
            # Counted, and not only logged. A cycle that fails every time
            # leaves the store growing, and the daemon says nothing about it
            # otherwise: the loop swallows the error and sleeps.
            GC_CYCLES.labels(result="error").inc()
            logger.warning("gc_error", exc_info=True)
        else:
            GC_CYCLES.labels(result="ok").inc()
        finally:
            GC_CYCLE_DURATION.observe(time.monotonic() - started)

        sleep_secs = random.uniform(GC_INTERVAL_SECONDS / 2, GC_INTERVAL_SECONDS)
        logger.debug("gc_sleeping", seconds=round(sleep_secs, 1))
        await asyncio.sleep(sleep_secs)


async def _run_gc_cycle() -> None:
    """Execute one GC cycle: optionally copy to cache, then delete old paths."""
    if PYNIXD_ENABLED:
        try:
            await copy_to_cache(None)
        except Exception:
            logger.warning("gc_cache_copy_failed", exc_info=True)

    stdout = await sh("nix", "path-info", "--store", "local", "--all", "--json").stdout(
        sh.CAPTURE
    )

    try:
        path_info: list[dict] = json.loads(stdout)
    except json.JSONDecodeError:
        logger.warning("gc_path_info_parse_error", output=str(stdout)[:200])
        return

    cutoff = time.time() - GC_KEEP_SECONDS
    old_paths = [
        entry["path"]
        for entry in path_info
        if isinstance(entry, dict)
        and entry.get("registrationTime", cutoff + 1) < cutoff
    ]

    if not old_paths:
        logger.debug("gc_nothing_to_delete")
        return

    logger.info("gc_deleting_paths", count=len(old_paths))
    await sh(
        "nix", "store", "delete", "--store", "local", "--stdin", "--skip-live"
    ).stdin("\n".join(old_paths))
    GC_PATHS_DELETED.inc(len(old_paths))
    logger.info("gc_done", deleted=len(old_paths))
