# SPDX-License-Identifier: MIT

from pathlib import Path

import structlog

from ..constants import NIX_PATH_INFO_TIMEOUT
from ..errors import CommandTimeoutError, StorePathClosureError, SubprocessError
from ..subprocessing import try_captured

logger = structlog.get_logger("nixkube.nix")


async def get_closure_paths(package_paths: set[Path]) -> set[Path]:
    """Get all store paths in the closure of the given packages.

    Deadlined like every other nix call here: this runs inside
    `NodePublishVolume` and inside the NRI build task, and neither has
    anything above it that would give up.
    """
    try:
        return {
            Path(p)
            for p in (
                await try_captured(
                    "nix",
                    "path-info",
                    "--recursive",
                    *package_paths,
                    timeout=NIX_PATH_INFO_TIMEOUT,
                )
            ).stdout.splitlines()
        }
    except CommandTimeoutError as e:
        raise StorePathClosureError(
            f"Store path closure timeout after {NIX_PATH_INFO_TIMEOUT}s",
            logs=e.combined,
        ) from e
    except SubprocessError as e:
        raise StorePathClosureError(
            "Failed to get store path closure",
            logs=e.combined,
        ) from e
