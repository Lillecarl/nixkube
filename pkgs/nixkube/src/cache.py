# SPDX-License-Identifier: MIT

import asyncio
import json
from asyncio import Semaphore, sleep
from collections import defaultdict
from pathlib import Path

import structlog

from .constants import PYNIXD_ENABLED
from .subprocessing import run_captured

logger = structlog.get_logger("nixkube.cache")

# Prevent concurrent cache uploads of the same store paths.
# Uses frozenset of paths as key (all paths in a copy operation are serialized together)
# and Semaphore as value (asyncio.Semaphore allows concurrent access limits).
# Ensures only one copy_to_cache() call per unique path set can run at a time.
copy_lock: defaultdict[frozenset[Path], Semaphore] = defaultdict(Semaphore)


async def check_cache_connectivity() -> bool:
    """Check if the cache is reachable via SSH."""
    if not PYNIXD_ENABLED:
        return False

    try:
        logger.debug("cache_connectivity_check")
        result = await asyncio.wait_for(
            run_captured(
                "nix",
                "store",
                "ping",
                "--json",
                "--store",
                "ssh-ng://nix@pynixd",
            ),
            timeout=10.0,
        )
        if result.returncode != 0:
            logger.warning("cache_connectivity_failed", stderr=result.stderr)
            return False
        try:
            ping_data = json.loads(result.stdout)
        except json.JSONDecodeError:
            ping_data = {}
        logger.debug("cache_connectivity_ok", **ping_data)
        return True
    except (OSError, asyncio.TimeoutError):
        logger.warning("cache_connectivity_failed")
        return False


def get_substituter_args() -> list[str]:
    """Get nix command arguments for using the cache as a substituter."""
    return [
        "--extra-substituters",
        "ssh-ng://nix@pynixd?trusted=1&priority=20",
    ]


async def copy_to_cache(package_paths: set[Path] | None) -> None:
    """
    Copy packages and their closures to the cache.

    If package_paths is None, copies all paths in the local store (used by GC).

    TODO: Rewrite this entire copy process to support user-supplied copy scripts.
    This will allow end-users to copy to arbitrary destinations (S3, GCS, custom caches, etc.)
    rather than hard-coding ssh-ng://nix@nix-cache.

    TODO: Building should be moved to separate builder pods rather than happening
    within the CSI daemonset. The daemonset should only handle mounting pre-built paths.
    This will improve separation of concerns and allow dedicated builder infrastructure.
    """
    if not PYNIXD_ENABLED:
        # There is no cache to copy to. Without this the node signs the
        # closure and then runs `nix copy` six times against a name that
        # does not resolve, sleeping 5, 10, 20, 40 and 60 seconds between
        # the attempts. Measured on a node with pynixd off: 135s of that
        # per container, and the same again on every GC pass.
        logger.debug("copy_to_cache_skipped", reason="pynixd_disabled")
        return

    if package_paths is not None and not package_paths:
        logger.debug("copy_to_cache_skipped", reason="no_paths")
        return

    lock_key = frozenset(package_paths) if package_paths is not None else frozenset()

    async with copy_lock[lock_key]:
        if package_paths is None:
            # All-paths mode: sign and copy everything in the local store.
            path_args: list[str | Path] = ["--all"]
            log = logger.bind(all=True)
        else:
            logger.debug("copy_to_cache_start", count=len(package_paths))
            paths: set[Path] = {Path(p) for p in package_paths}

            # Run path-info calls concurrently (regular + derivation)
            path_info, path_info_drv = await asyncio.gather(
                run_captured(
                    "nix",
                    "path-info",
                    "--recursive",
                    *package_paths,
                ),
                run_captured(
                    "nix",
                    "path-info",
                    "--recursive",
                    "--derivation",
                    *package_paths,
                ),
            )

            # Get regular closure paths for all packages
            if path_info.returncode == 0:
                paths.update(Path(p) for p in path_info.stdout.splitlines())
            else:
                logger.warning(
                    "path_info_failed",
                    returncode=path_info.returncode,
                    stderr=path_info.stderr,
                )

            # Try to get derivation paths recursively. This may fail if we only have
            # store paths without .drv files (e.g., fetched from substituters), which is normal.
            if path_info_drv.returncode == 0:
                paths.update(Path(p) for p in path_info_drv.stdout.splitlines())
            # Filter out .drv files and deduplicate
            paths = {p for p in paths if p.suffix != ".drv"}
            path_args = list(paths)
            log = logger.bind(count=len(paths))

        sign_result = await run_captured(
            "nix",
            "store",
            "sign",
            "--key-file",
            "/etc/nix-key/nix_ed25519",
            *path_args,
        )
        if sign_result.returncode != 0:
            log.warning(
                "sign_paths_failed",
                returncode=sign_result.returncode,
                stderr=sign_result.stderr,
            )

        for attempt in range(6):
            if attempt > 0:
                exp_backoff = min(5 * (2 ** (attempt - 1)), 60)
                log.warning(
                    "copy_retry",
                    attempt=attempt,
                    max_attempts=6,
                    backoff=exp_backoff,
                )
                await sleep(exp_backoff)

            nix_copy = await run_captured(
                "nix",
                "copy",
                "--no-check-sigs",
                "--to",
                "ssh-ng://nix@pynixd",
                *path_args,
            )
            if nix_copy.returncode == 0:
                log.debug("copy_to_cache_done")
                break
            else:
                log.warning(
                    "copy_attempt_failed",
                    attempt=attempt + 1,
                    max_attempts=6,
                    returncode=nix_copy.returncode,
                    stdout=nix_copy.stdout,
                    stderr=nix_copy.stderr,
                )
        else:
            log.error("copy_to_cache_exhausted")


def schedule_copy_to_cache(package_paths: set[Path]) -> None:
    """Fire-and-forget background task to copy packages to cache."""
    if not package_paths:
        return
    task = asyncio.create_task(copy_to_cache(package_paths))
    task.add_done_callback(
        lambda t: (
            logger.error("copy_to_cache_failed", exc_info=t.exception())
            if t.exception()
            else None
        )
    )
