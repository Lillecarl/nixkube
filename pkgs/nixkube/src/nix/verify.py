# SPDX-License-Identifier: MIT

from pathlib import Path

import structlog

from ..constants import NIX_VERIFY_TIMEOUT
from ..errors import CommandTimeoutError, SubprocessError, VerifyStorePathsError
from ..subprocessing import try_captured

logger = structlog.get_logger("nixkube.nix")


async def verify_store_paths(package_paths: set[Path]) -> None:
    """Verify the integrity of all packages and their closures.

    Deadlined like every other nix call here. This one runs inside
    `NodePublishVolume`, so without a bound a store on a stuck filesystem
    leaves the pod waiting for ever and kubelet with nothing to retry.
    """
    try:
        logger.debug("verify_store_paths", paths=package_paths)
        await try_captured(
            "nix",
            "store",
            "verify",
            "--recursive",
            "--no-trust",
            *package_paths,
            timeout=NIX_VERIFY_TIMEOUT,
        )
    except CommandTimeoutError as e:
        raise VerifyStorePathsError(
            f"Store path verification timeout after {NIX_VERIFY_TIMEOUT}s",
            logs=e.combined,
        ) from e
    except SubprocessError as e:
        raise VerifyStorePathsError(
            "Failed to verify store paths",
            logs=e.combined,
        ) from e
