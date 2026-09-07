# SPDX-License-Identifier: MIT

import ctypes
import ctypes.util
import errno
import os
import shutil
import time
from pathlib import Path

import structlog

from .constants import (
    MS_BIND,
    MS_RDONLY,
    MS_REMOUNT,
    NIX_BUILD_TIMEOUT,
    VERIFY_STORE_PATHS,
)
from .errors import FailedVolumeCleanupError, MountError, UnmountError
from .hardlinks import deref_hardlink_tree, hardlink_closure
from .nix import (
    get_closure_paths,
    init_database,
    install_gcroots,
    install_result_link,
    verify_store_paths,
)

logger = structlog.get_logger("nixkube.volume")

# Load libc for mount/umount syscalls
_libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)

# Get references to syscall functions
_libc.mount.argtypes = [
    ctypes.c_char_p,
    ctypes.c_char_p,
    ctypes.c_char_p,
    ctypes.c_ulong,
    ctypes.c_char_p,
]
_libc.mount.restype = ctypes.c_int
_libc.umount2.argtypes = [ctypes.c_char_p, ctypes.c_int]
_libc.umount2.restype = ctypes.c_int


async def prepare_volume(
    volume_root: Path,
    package_paths: set[Path],
    primary_package: Path | None,
) -> None:
    """
    Prepare a volume root with hardlinked store paths and initialized database.
    """

    # Capitalized to emphasise they're Nix environment variables
    NIX_STATE_DIR = volume_root / "nix/var/nix"
    NIX_STATE_DIR.mkdir(parents=True, exist_ok=True)

    # Pre-create overlayfs upper/work dirs so they're ready if the container
    # requests a RW /nix mount; harmless when the bind-mount path is used.
    (volume_root / "upper").mkdir(parents=True, exist_ok=True)
    (volume_root / "work").mkdir(parents=True, exist_ok=True)

    # Verify all packages and their closures before processing
    if VERIFY_STORE_PATHS:
        await verify_store_paths(package_paths)

    # Get storepaths from all packages
    store_paths = await get_closure_paths(package_paths)

    # This block is essentially nix copy into a chroot store with
    # extra steps. (Hardlinking instead of dumbcopying)

    # Copy closure to substore
    hardlink_start = time.perf_counter()
    hardlink_closure(store_paths, volume_root / "nix/store")
    logger.debug(
        "hardlinked_paths",
        volume_root=volume_root,
        count=len(store_paths),
        elapsed=round(time.perf_counter() - hardlink_start, 3),
    )

    # Create Nix database
    await init_database(NIX_STATE_DIR, store_paths)

    # Install gcroots in container using chroot store. This is
    # required because the auto roots created for /nix/var/result
    # will point to Narnia while this one points into store.
    await install_gcroots(
        package_paths,
        NIX_STATE_DIR / "gcroots" / "csi",
        store=volume_root,
        timeout=NIX_BUILD_TIMEOUT,
    )

    # Install /nix/var/result in container using chroot store
    if primary_package is not None:
        await install_result_link(volume_root, primary_package)
        # Create hardlink farm of primary package to volume_root
        deref_start = time.perf_counter()
        deref_hardlink_tree(primary_package, volume_root)
        logger.debug(
            "deref_hardlink_tree_done",
            elapsed=round(time.perf_counter() - deref_start, 3),
        )


async def mount_volume(
    volume_root: Path,
    target_path: Path,
    readonly: bool,
) -> None:
    """Mount the volume root to the target path using syscalls."""
    # Check source and target paths before attempting mount
    if not volume_root.exists():
        raise MountError(
            f"Source path does not exist: {volume_root}",
            logs="",
        )

    target_path.mkdir(parents=True, exist_ok=True)
    if not target_path.exists():
        raise MountError(
            f"Target path could not be created: {target_path}",
            logs="",
        )

    if readonly:
        # For readonly we use a bind mount, the benefit is that different
        # container stores using bindmounts will get the same inodes and
        # share page cache with others, reducing memory usage.
        logger.debug(
            "mounting_bind_readonly", src=str(volume_root), dst=str(target_path)
        )
        ret = _libc.mount(
            ctypes.c_char_p(os.fsencode(volume_root)),
            ctypes.c_char_p(os.fsencode(target_path)),
            None,
            MS_BIND | MS_RDONLY,
            None,
        )
        if ret != 0:
            err = ctypes.get_errno()
            if err == errno.EEXIST:
                pass  # Already mounted is fine
            else:
                logger.error(
                    "volume_mount_failed",
                    error=os.strerror(err),
                    error_code=err,
                    source=volume_root,
                    target=target_path,
                    source_exists=volume_root.exists(),
                    target_exists=target_path.exists(),
                )
                raise MountError(
                    f"Failed to mount bind volume: {os.strerror(err)} (errno {err}). "
                    f"source_exists={volume_root.exists()}, target_exists={target_path.exists()}, "
                    f"target_parent_exists={target_path.parent.exists()}",
                    logs="",
                )
        # Older kernels silently ignore MS_RDONLY on initial bind mounts, so
        # remount to guarantee the RO flag is enforced.
        ret = _libc.mount(
            None,
            ctypes.c_char_p(os.fsencode(target_path)),
            None,
            MS_BIND | MS_REMOUNT | MS_RDONLY,
            None,
        )
        if ret != 0:
            err = ctypes.get_errno()
            # Unmount the writable bind mount so we don't leave it exposed
            _libc.umount2(ctypes.c_char_p(os.fsencode(target_path)), ctypes.c_int(0))
            raise MountError(
                f"Failed to remount bind volume read-only: {os.strerror(err)} (errno {err})",
                logs="",
            )
    else:
        # For readwrite we use an overlayfs mount, the benefit here is that
        # it works as CoW even if the underlying filesystem doesn't support
        # it, reducing host storage usage.
        workdir = volume_root / "work"
        upperdir = volume_root / "upper"
        workdir.mkdir(parents=True, exist_ok=True)
        upperdir.mkdir(parents=True, exist_ok=True)

        if not workdir.exists() or not upperdir.exists():
            raise MountError(
                f"Failed to create overlay directories: workdir={workdir.exists()}, upperdir={upperdir.exists()}",
                logs="",
            )

        options = (
            f"lowerdir={volume_root},upperdir={upperdir},workdir={workdir}".encode()
        )
        logger.debug("mounting_overlay", src=str(volume_root), dst=str(target_path))
        ret = _libc.mount(
            ctypes.c_char_p(b"overlay"),
            ctypes.c_char_p(os.fsencode(target_path)),
            ctypes.c_char_p(b"overlay"),
            0,
            ctypes.c_char_p(options),
        )
        if ret != 0:
            err = ctypes.get_errno()
            if err == errno.EEXIST:
                pass  # Already mounted is fine
            else:
                raise MountError(
                    f"Failed to mount overlay volume: {os.strerror(err)} (errno {err}). "
                    f"lowerdir_exists={volume_root.exists()}, target_exists={target_path.exists()}, "
                    f"target_parent_exists={target_path.parent.exists()}",
                    logs="",
                )


def cleanup_failed_volume(gc_root: Path, volume_root: Path) -> None:
    """Clean up resources after a failed volume operation."""
    failed_paths = []
    for path in [gc_root, volume_root]:
        if path.exists():
            try:
                shutil.rmtree(path)
            except OSError as e:
                failed_paths.append(f"{path}: {e}")

    if failed_paths:
        raise FailedVolumeCleanupError(
            "Failed to clean up volume resources",
            logs="\n".join(failed_paths),
        )


def is_mount(path: Path, mounts_file: Path | None = None) -> bool:
    """Check if a path is a mount point by reading the mounts file.

    Format: filesystem mountpoint fstype options dump pass
    We just need to check if path matches index 1 (mountpoint).

    /proc/self/mounts is a virtual file that never blocks, so synchronous read is safe.

    Args:
        path: Path to check if it's a mount point
        mounts_file: Path to mounts file (default: /proc/self/mounts)
    """
    if mounts_file is None:
        mounts_file = Path("/proc/self/mounts")

    try:
        path_resolved = path.resolve()
        path_str = str(path_resolved)

        content = mounts_file.read_text()
        for line in content.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[1] == path_str:
                return True
        return False
    except (FileNotFoundError, OSError):
        logger.warning(
            "mounts_check_failed", mounts_file=str(mounts_file), exc_info=True
        )
        return False


async def unmount(path: Path, mounts_file: Path | None = None) -> None:
    """Unmount a path using syscall. Raises UnmountError if the mount persists.

    Args:
        path: Path to unmount
        mounts_file: Path to mounts file for checking (default: /proc/self/mounts)
    """
    logger.debug("unmounting", path=str(path))
    ret = _libc.umount2(
        ctypes.c_char_p(os.fsencode(path)),
        ctypes.c_int(0),
    )
    err = ctypes.get_errno() if ret != 0 else 0

    if ret != 0:
        logger.warning(
            "umount2_error",
            path=str(path),
            error=os.strerror(err),
            errno=err,
        )

    # Always verify the mount is actually gone — umount2 can return 0
    # but leave the mount (e.g. propagation), or return non-zero for a
    # non-fatal reason (e.g. EINVAL when already unmounted).
    if is_mount(path, mounts_file=mounts_file):
        raise UnmountError(
            f"Mount still present after unmount: {os.strerror(err)} (errno {err})"
            if err
            else f"umount2 succeeded but mount still present at {path}",
            logs="",
        )
