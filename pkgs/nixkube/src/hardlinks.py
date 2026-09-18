# SPDX-License-Identifier: MIT

import os
from collections.abc import AsyncIterator
from pathlib import Path

import anyio
import structlog
from anyio.lowlevel import checkpoint

from .errors import HardlinkClosureError

logger = structlog.get_logger("nixkube.hardlinks")

_YIELD_EVERY = 256
"""Directory entries between two checkpoints."""


async def _entries(src: Path) -> AsyncIterator[os.DirEntry[str]]:
    """The entries of `src`, giving the event loop a turn as it goes.

    The loop this runs on serves the NRI heartbeat (`_pump_build_progress`,
    every 10s) and the ZeroMQ REP socket, and `nri-wait` gives up after 30s of
    silence. A walk that never yields therefore kills the container it is
    linking a store into. Issue #45.

    `os.scandir` and not `anyio.Path.iterdir`: hardlinking is a metadata
    syscall that moves no bytes, and every `anyio.Path` call is a thread round
    trip. Measured over 20,000 files: 11.75s through `anyio.Path` against
    1.28s this way, and both hold the loop's longest gap under 0.15s. The
    thread buys responsiveness this already has, at 9x the time.
    """
    with os.scandir(src) as scan:
        for index, entry in enumerate(scan):
            if index % _YIELD_EVERY == 0:
                await checkpoint()
            yield entry


async def hardlink_tree(src: Path, dst: Path) -> None:
    """Hardlink a single path (file or directory tree), preserving symlinks."""
    if src.is_symlink():
        dst.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(os.readlink(src), dst)
    elif src.is_file():
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.is_file():
            # Destination is a pre-created empty placeholder file (e.g. for a bind mount).
            # Write content in-place so the inode stays the same — the bind mount is
            # tied to the inode, so unlinking and re-creating would break it.
            #
            # `anyio.Path` here and nowhere else in this file: this is the one
            # branch that moves bytes, so it is the one that can block for long.
            await anyio.Path(dst).write_bytes(await anyio.Path(src).read_bytes())
        else:
            dst.hardlink_to(src)
    elif src.is_dir():
        dst.mkdir(parents=True, exist_ok=True)
        async for entry in _entries(src):
            dst_path = dst / entry.name
            if entry.is_symlink():
                os.symlink(os.readlink(entry.path), dst_path)
            elif entry.is_dir(follow_symlinks=False):
                await hardlink_tree(Path(entry.path), dst_path)
            elif entry.is_file(follow_symlinks=False):
                dst_path.hardlink_to(entry.path)


async def hardlink_closure(store_paths: set[Path], dst: Path) -> None:
    """
    Hardlink multiple store paths into dst.

    store_paths: {/nix/store/abc-foo, /nix/store/def-bar, ...}
    dst: volume_root/nix/store
    result: dst/abc-foo/..., dst/def-bar/...
    """
    logger.debug("hardlink_closure", paths=store_paths, dst=dst)
    try:
        dst.mkdir(parents=True, exist_ok=True)

        for store_path in store_paths:
            target = dst / store_path.name
            if target.exists():
                continue  # already copied (deduplication across volumes)
            try:
                await hardlink_tree(store_path, target)
            except Exception as e:
                raise HardlinkClosureError(
                    f"Failed to hardlink {store_path.name}",
                    logs=str(e),
                ) from e
    except HardlinkClosureError:
        # Re-raise to prevent outer except Exception from double-wrapping
        raise
    except Exception as e:
        raise HardlinkClosureError(
            "Failed to hardlink store paths to volume",
            logs=str(e),
        ) from e


async def deref_hardlink_tree(src: Path, dst: Path) -> None:
    """
    Recursively copy src to dst, dereferencing symlinks and
    hardlinking files for space efficiency.

    Symlink handling:
    - Symlinks to /nix/store that exist: dereference recursively
    - Symlinks to /nix/store that are broken: log warning and copy as-is
    - Symlinks to outside /nix/store: log warning and copy as-is
    """
    src = Path(src)

    if src.is_symlink():
        target = os.readlink(src)
        resolved = (src.parent / target).resolve()

        # Check if target is outside /nix/store
        try:
            resolved.relative_to("/nix/store")
            in_store = True
        except ValueError:
            in_store = False

        if not in_store:
            # Target is outside /nix/store, log warning and copy symlink as-is
            logger.warning("symlink_outside_store", src=str(src), target=target)
            dst.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(target, dst)
        elif not resolved.exists():
            # Target is in /nix/store but broken, log warning and copy as-is
            logger.warning("broken_symlink", src=str(src), target=target)
            dst.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(target, dst)
        else:
            # Target is in /nix/store and exists, dereference it
            await deref_hardlink_tree(resolved, dst)
    elif src.is_file():
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.hardlink_to(src)
    elif src.is_dir():
        dst.mkdir(parents=True, exist_ok=True)
        async for entry in _entries(src):
            await deref_hardlink_tree(Path(entry.path), dst / entry.name)
