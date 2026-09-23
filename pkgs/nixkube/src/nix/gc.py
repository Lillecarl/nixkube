# SPDX-License-Identifier: MIT

from pathlib import Path

import structlog

from ..constants import NIX_BUILD_TIMEOUT
from ..errors import (
    CommandTimeoutError,
    InstallGCRootError,
    InstallResultLinkError,
    SubprocessError,
)
from ..subprocessing import try_captured

logger = structlog.get_logger("nixkube.nix")


async def install_gcroots(
    package_paths: set[Path],
    out_link: Path,
    store: Path | None = None,
    timeout: float = NIX_BUILD_TIMEOUT,
) -> None:
    """Install gc roots with single batch build.

    The deadline has a default rather than being optional. A caller that
    forgot one got an unbounded `nix build` on a request path, which is the
    shape issue #38 measured in the GC loop.
    """
    if not package_paths:
        return

    try:
        args: list[str | Path] = ["nix", "build"]
        if store is not None:
            args.extend(["--store", store])
        args.extend(["--out-link", out_link])
        args.extend(package_paths)

        await try_captured(*args, timeout=timeout)
    except CommandTimeoutError as e:
        raise InstallGCRootError(
            f"GC root installation timeout after {timeout}s",
            logs=e.combined,
        ) from e
    except SubprocessError as e:
        raise InstallGCRootError(
            "Failed to install garbage collection root",
            logs=e.combined,
        ) from e


async def install_result_link(
    volume_root: Path,
    package_path: Path,
) -> None:
    """Install /nix/var/result symlink in the chroot store."""
    try:
        await try_captured(
            "nix",
            "build",
            "--store",
            volume_root,
            "--out-link",
            volume_root / "nix/var/result",
            package_path,
            timeout=NIX_BUILD_TIMEOUT,
        )
    except CommandTimeoutError as e:
        raise InstallResultLinkError(
            f"Result link installation timeout after {NIX_BUILD_TIMEOUT}s",
            logs=e.combined,
        ) from e
    except SubprocessError as e:
        raise InstallResultLinkError(
            "Failed to install /nix/var/result symlink",
            logs=e.combined,
        ) from e
