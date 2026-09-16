# SPDX-License-Identifier: MIT

"""Async garbage collection loop for the Nix store.

Ports the logic from environments/modules/gc.nix:
1. Optionally copies all store paths to the cache over SSH.
2. Deletes store paths older than GC_KEEP_SECONDS.
3. Sleeps a randomised interval before repeating.

Every nix call here carries a timeout. The loop awaits one cycle at a time, so
one call that never returns ends collection on that node. Issue #38 measured
that: 33 hours on a four-node cluster, zero completed cycles.
"""

import asyncio
import json
import random
import tempfile
import time
from pathlib import Path

import structlog
from shellous import sh

from .cache import copy_to_cache
from .constants import (
    GC_DELETE_TIMEOUT_SECONDS,
    GC_INTERVAL_SECONDS,
    GC_KEEP_SECONDS,
    GC_PATH_INFO_TIMEOUT_SECONDS,
    GC_STALL_CYCLES,
    PYNIXD_ENABLED,
)
from .metrics import (
    GC_CYCLE_DURATION,
    GC_CYCLES,
    GC_LAST_SUCCESS,
    GC_PATHS_DELETED,
    STORE_NAR_BYTES,
    STORE_PATHS,
)

logger = structlog.get_logger("nixkube.gc")

# `nix store delete` says this, one line per path, and exits 1. It is the
# normal outcome and not a failure: the loop offers every path older than
# GC_KEEP_SECONDS, and most of them are still in use.
_STILL_ALIVE = "since it is still alive"


async def gc_loop() -> None:
    """Run garbage collection on the local Nix store in a loop.

    Non-fatal: exceptions are logged as warnings and the loop continues.
    """
    last_ok = time.monotonic()
    while True:
        stalled_for = time.monotonic() - last_ok
        if stalled_for > GC_STALL_CYCLES * GC_INTERVAL_SECONDS:
            # Loud, because the store keeps growing while this is true and
            # nothing else on the node says so.
            logger.error(
                "gc_stalled",
                seconds_since_last_cycle=round(stalled_for),
                cycles=GC_STALL_CYCLES,
            )

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
            last_ok = time.monotonic()
            # `time.time` and not `time.monotonic`: this one leaves the node.
            GC_LAST_SUCCESS.set(time.time())
        finally:
            GC_CYCLE_DURATION.observe(time.monotonic() - started)

        sleep_secs = random.uniform(GC_INTERVAL_SECONDS / 2, GC_INTERVAL_SECONDS)
        logger.debug("gc_sleeping", seconds=round(sleep_secs, 1))
        await asyncio.sleep(sleep_secs)


async def _read_path_info(work: Path) -> dict[str, dict]:
    """Every path in the store, keyed by path, with its registration time.

    **To a file, and not to a pipe.** `nix path-info --all --json` writes the
    whole store as one JSON line. A capture that reads lines cannot take it:
    measured, `sh(...).stdout(sh.CAPTURE)` awaited over a single 300,000
    character line did not return in 20 s, and over a short one it answers the
    empty string rather than the output. Both are silent. A file redirect took
    the same 300,000 characters with no deadline at all.

    `--json-format 2` is not a way out. It is also one line, and it keys on the
    base name instead of the path.
    """
    out = work / "path-info.json"
    async with asyncio.timeout(GC_PATH_INFO_TIMEOUT_SECONDS):
        await sh(
            "nix",
            "path-info",
            "--store",
            "local",
            "--all",
            "--json",
            "--json-format",
            "1",
        ).stdout(out)
    return json.loads(out.read_text())


def _record_store_size(path_info: dict[str, dict]) -> None:
    """How big this node's store is, from the answer the cycle already has.

    There is no way to see a node's store grow from outside it, which is how
    issue #38 stayed invisible for 33 hours: the file system alert fired
    against `/var`, and nothing said which share was the store. Issue #39.

    The cost is nothing. `nix path-info --all --json` carries `narSize` beside
    the `registrationTime` the cycle reads, so this is a sum over a dict that
    is already in memory, on a cadence that is already slow. A `du`-style walk
    over a multi-GB store is what this avoids.

    `narSize` over-counts. See `STORE_NAR_BYTES` for the measurement.
    """
    STORE_PATHS.set(len(path_info))
    STORE_NAR_BYTES.set(
        sum(
            facts.get("narSize", 0)
            for facts in path_info.values()
            if isinstance(facts, dict)
        )
    )


def _select_old_paths(path_info: dict[str, dict], cutoff: float) -> list[str]:
    """The paths registered before `cutoff`.

    `nix path-info --json` answers an object keyed by store path, and the value
    holds no `path` of its own. Reading it as a list of entries walked the
    keys, which are strings, so the filter matched nothing and the loop deleted
    nothing on any node, ever. Issue #38.
    """
    return [
        path
        for path, facts in path_info.items()
        if isinstance(facts, dict)
        and facts.get("registrationTime", cutoff + 1) < cutoff
    ]


def _count_deleted(lines: list[str]) -> int:
    """How many paths Nix reported deleting, and a raise for anything else.

    Nix decides which of the offered paths are safe. It deletes those, refuses
    the rest with `_STILL_ALIVE`, and **exits 1 whenever it refused any**.
    Measured on nix 2.34.8: a mixed list exits 1 with the dead path gone, an
    all-live list exits 1 with nothing gone, an all-dead list exits 0.

    So a liveness error is not a failure. Any other `error:` is.

    The count is what Nix reported, not the length of the offered list. Those
    are different numbers on every cycle that offers a live path.
    """
    errors = [line for line in lines if line.startswith("error:")]
    unexpected = [line for line in errors if _STILL_ALIVE not in line]
    if unexpected:
        raise RuntimeError(f"nix store delete failed: {unexpected[0]}")
    if errors:
        logger.debug("gc_paths_still_alive", count=len(errors))
    return sum(1 for line in lines if line.startswith("deleting '"))


async def _delete(work: Path, old_paths: list[str]) -> int:
    """Offer the old paths to Nix, and answer how many it took."""
    paths_file = work / "paths.txt"
    # The trailing newline is deliberate: `"\n".join(...)` leaves the last path
    # without one.
    paths_file.write_text("\n".join(old_paths) + "\n")

    out = work / "delete.log"
    async with asyncio.timeout(GC_DELETE_TIMEOUT_SECONDS):
        await (
            sh("nix", "store", "delete", "--store", "local", "--stdin")
            .stdin(paths_file)
            .stdout(out)
            .stderr(sh.STDOUT)
            .set(exit_codes={0, 1})
        )

    return _count_deleted(out.read_text().splitlines())


async def _run_gc_cycle() -> None:
    """Execute one GC cycle: optionally copy to cache, then delete old paths."""
    if PYNIXD_ENABLED:
        try:
            await copy_to_cache(None)
        except Exception:
            logger.warning("gc_cache_copy_failed", exc_info=True)

    with tempfile.TemporaryDirectory(prefix="nixkube-gc-") as tmp:
        work = Path(tmp)

        try:
            path_info = await _read_path_info(work)
        except json.JSONDecodeError:
            logger.warning("gc_path_info_parse_error", exc_info=True)
            return

        _record_store_size(path_info)
        old_paths = _select_old_paths(path_info, time.time() - GC_KEEP_SECONDS)

        if not old_paths:
            logger.debug("gc_nothing_to_delete", known=len(path_info))
            return

        logger.info("gc_deleting_paths", count=len(old_paths))
        deleted = await _delete(work, old_paths)

    GC_PATHS_DELETED.inc(deleted)
    logger.info("gc_done", offered=len(old_paths), deleted=deleted)
