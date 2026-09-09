# SPDX-License-Identifier: MIT

"""Mount /nix and FHS store paths into a running container's mount namespace.

## Approach

/nix is mounted via one of two paths depending on whether the container
requested read-write access (nixkube/pod-rw or nixkube/{container}-rw annotation):

  RO (default): open_tree(2) clones the prepared nix tree as a detached fd while
    still in the daemonset namespace; the fd survives setns(2) so the source
    path never needs to be visible inside the container. move_mount(2) attaches
    it at /nix, followed by MS_BIND|MS_REMOUNT|MS_RDONLY.

  RW: fsopen/fsconfig/fsmount build a detached overlayfs fd before setns.
    lowerdir=volume_root/nix (the hardlink tree), upperdir=volume_root/upper,
    workdir=volume_root/work. These directories are pre-created by prepare_volume.
    move_mount attaches the result at /nix read-write.

For FHS store mounts we use the traditional mount(2) MS_BIND approach (RW),
executed inside the container namespace after /nix is attached so that
/nix/store/... paths are reachable as bind-mount sources.

Note on syscall families: open_tree/move_mount and fsopen/fsconfig/fsmount serve
distinct purposes and always coexist — open_tree clones an existing mount tree
(the new-API equivalent of MS_BIND), while fsopen creates a new filesystem instance
(overlay, tmpfs, etc.). There is no fsopen("bind"). The meaningful future migration
for store mounts is therefore open_tree+move_mount, not fsopen: grabbing the mount
fds before setns would remove the ordering constraint where /nix must be mounted
first so /nix/store/... sources are visible.

## Execution context

When our createRuntime hook fires the container init process is alive with its
mount namespace established. pivot_root has NOT happened yet — the container's
root is still the host root. We use the fchdir+chroot trick to enter the
container's rootfs: open an O_PATH fd to the bundle rootfs before setns (while
the host path is accessible), then fchdir+chroot after setns.

## Why multiprocessing.spawn

setns(2) affects the calling thread's namespace, which would contaminate the
asyncio event-loop thread. fork is unsafe inside asyncio. spawn starts a fresh
Python interpreter with no inherited async state and all state passed explicitly
via picklable arguments.

## Worker sequence

  1. /nix mount fd  — while in daemonset namespace:
       RO: open_tree clone
       RW: fsopen("overlay") → fsconfig(lowerdir/upper/work) → CMD_CREATE → fsmount
  2. rootfs fd      — O_PATH to bundle/rootfs while in daemonset namespace
  3. setns          — enter container mount namespace
  4. fchdir+chroot  — pivot into container rootfs
  5. move_mount     — attach /nix fd (RO: then remount read-only)
  6. MS_BIND        — bind each FHS store path (read-write) inside container;
                      sources resolved after step 5 so /nix/store/... is visible.
                      (Future: open_tree+move_mount before setns removes this dependency)
"""

import asyncio
import ctypes
import errno
import multiprocessing
import os
import traceback
from functools import cache
from pathlib import Path

import structlog

from ..constants import HOST_PROC_PATH, MS_BIND, MS_RDONLY, MS_REMOUNT

logger = structlog.get_logger("nixkube.nri.mount")

# setns(2) namespace flag
CLONE_NEWNS = 0x00020000

# openat/open_tree dirfd sentinel
AT_FDCWD = -100

# open_tree(2) flags (Linux 5.2, include/uapi/linux/mount.h)
OPEN_TREE_CLONE = 1  # clone the subtree into a detached mount namespace
OPEN_TREE_CLOEXEC = os.O_CLOEXEC
AT_RECURSIVE = 0x8000  # recurse into sub-mounts within the subtree

# move_mount(2) flags (Linux 5.2, include/uapi/linux/mount.h)
MOVE_MOUNT_F_EMPTY_PATH = 0x4  # from_dirfd is the mount fd; from_pathname ignored

# fsopen(2) flags (Linux 5.2, include/uapi/linux/mount.h)
FSOPEN_CLOEXEC = 0x1

# fsconfig(2) commands (Linux 5.2, include/uapi/linux/mount.h)
FSCONFIG_SET_STRING = 1  # set a string-valued parameter
FSCONFIG_CMD_CREATE = 6  # create the superblock (no key/value)

# fsmount(2) flags (Linux 5.2, include/uapi/linux/mount.h)
FSMOUNT_CLOEXEC = 0x1

# Linux syscall numbers for the new mount API.
# All introduced in Linux 5.2 via the common syscall table
# (include/uapi/asm-generic/unistd.h), so x86_64 and aarch64 share them.
_NR_OPEN_TREE = 428
_NR_MOVE_MOUNT = 429
_NR_FSOPEN = 430
_NR_FSCONFIG = 431
_NR_FSMOUNT = 432


@cache
def kernel_supports_ro() -> bool:
    """Test if kernel supports RO mounts (open_tree/move_mount). Result is cached."""
    libc = ctypes.CDLL(None, use_errno=True)
    test_path = Path("/tmp") if Path("/tmp").exists() else Path("/nix")
    try:
        fd = _open_tree(libc, test_path)
        os.close(fd)
        return True
    except OSError as e:
        if e.errno == errno.ENOSYS:
            logger.error("kernel_no_open_tree", requires="linux_5.2")
        else:
            logger.error("open_tree_test_failed", exc_info=e)
        return False


@cache
def kernel_supports_rw() -> bool:
    """Test if kernel supports new mount API for overlayfs (fsopen/fsconfig/fsmount). Result is cached."""
    import tempfile

    libc = ctypes.CDLL(None, use_errno=True)
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            lowerdir = tmppath / "lower"
            upperdir = tmppath / "upper"
            workdir = tmppath / "work"

            lowerdir.mkdir()
            upperdir.mkdir()
            workdir.mkdir()

            try:
                overlay_fd = _make_overlay_fd(libc, lowerdir, upperdir, workdir)
                os.close(overlay_fd)
                return True
            except OSError as e:
                if e.errno == errno.ENOSYS:
                    logger.debug("kernel_no_overlay_mount_api", requires="linux_6.5")
                else:
                    logger.debug("overlay_mount_api_test_failed", exc_info=e)
                return False
    except Exception:
        logger.debug("overlay_mount_api_test_unexpected", exc_info=True)
        return False


def _open_tree(libc: ctypes.CDLL, path: Path) -> int:
    """Clone the mount subtree at path; return an fd to the detached clone."""
    flags = OPEN_TREE_CLONE | AT_RECURSIVE | OPEN_TREE_CLOEXEC
    fd = libc.syscall(
        ctypes.c_long(_NR_OPEN_TREE),
        ctypes.c_int(AT_FDCWD),
        ctypes.c_char_p(os.fsencode(path)),
        ctypes.c_uint(flags),
    )
    if fd < 0:
        errno = ctypes.get_errno()
        raise OSError(errno, f"open_tree({path!r}): {os.strerror(errno)}")
    return int(fd)


def _move_mount(libc: ctypes.CDLL, from_fd: int, to_path: Path) -> None:
    """Attach the detached mount fd at to_path (resolved in current root)."""
    ret = libc.syscall(
        ctypes.c_long(_NR_MOVE_MOUNT),
        ctypes.c_int(from_fd),
        ctypes.c_char_p(b""),
        ctypes.c_int(AT_FDCWD),
        ctypes.c_char_p(os.fsencode(to_path)),
        ctypes.c_uint(MOVE_MOUNT_F_EMPTY_PATH),
    )
    if ret < 0:
        errno = ctypes.get_errno()
        raise OSError(
            errno, f"move_mount(fd={from_fd} → {to_path!r}): {os.strerror(errno)}"
        )


def _make_overlay_fd(
    libc: ctypes.CDLL, lowerdir: Path, upperdir: Path, workdir: Path
) -> int:
    """Build a detached RW overlayfs mount fd via fsopen/fsconfig/fsmount.

    Paths are resolved in the caller's namespace (daemonset), so this must be
    called before setns. The returned fd can be passed to move_mount after setns.
    The internal fsopen context fd is always closed before returning.
    """
    ctx_fd = int(
        libc.syscall(
            ctypes.c_long(_NR_FSOPEN),
            ctypes.c_char_p(b"overlay"),
            ctypes.c_uint(FSOPEN_CLOEXEC),
        )
    )
    if ctx_fd < 0:
        errno = ctypes.get_errno()
        raise OSError(errno, f"fsopen(overlay): {os.strerror(errno)}")

    try:
        for key, path in (
            ("lowerdir", lowerdir),
            ("upperdir", upperdir),
            ("workdir", workdir),
        ):
            ret = libc.syscall(
                ctypes.c_long(_NR_FSCONFIG),
                ctypes.c_int(ctx_fd),
                ctypes.c_uint(FSCONFIG_SET_STRING),
                ctypes.c_char_p(key.encode()),
                ctypes.c_char_p(os.fsencode(path)),
                ctypes.c_int(0),
            )
            if ret < 0:
                errno = ctypes.get_errno()
                raise OSError(
                    errno, f"fsconfig({key!r}={path!r}): {os.strerror(errno)}"
                )

        ret = libc.syscall(
            ctypes.c_long(_NR_FSCONFIG),
            ctypes.c_int(ctx_fd),
            ctypes.c_uint(FSCONFIG_CMD_CREATE),
            ctypes.c_char_p(None),
            ctypes.c_char_p(None),
            ctypes.c_int(0),
        )
        if ret < 0:
            errno = ctypes.get_errno()
            raise OSError(errno, f"fsconfig(CMD_CREATE): {os.strerror(errno)}")

        mnt_fd = int(
            libc.syscall(
                ctypes.c_long(_NR_FSMOUNT),
                ctypes.c_int(ctx_fd),
                ctypes.c_uint(FSMOUNT_CLOEXEC),
                ctypes.c_uint(0),  # attr_flags: 0 = read-write
            )
        )
        if mnt_fd < 0:
            errno = ctypes.get_errno()
            raise OSError(errno, f"fsmount: {os.strerror(errno)}")

        return mnt_fd
    finally:
        os.close(ctx_fd)


def _mount_worker(
    container_pid: int,
    bundle: str,
    nix_tree_path: Path,
    store_mounts: list[tuple[Path, Path]],
    nix_rw: bool,
    result_queue: "multiprocessing.Queue[Exception | None]",
    host_proc_path: str,
) -> None:
    """Worker that runs in a spawned process to mount /nix and store paths.

    Sends None on success or the Exception (with traceback in __notes__) on failure.
    All arguments are passed explicitly (spawn context, no inherited state).
    """
    try:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        libc.syscall.restype = ctypes.c_long

        # Step 1: Build the /nix mount fd while source paths are still visible.
        # Both open_tree and fsmount return fds that survive setns.
        if nix_rw:
            nix_fd = _make_overlay_fd(
                libc,
                lowerdir=nix_tree_path,
                upperdir=nix_tree_path.parent / "upper",
                workdir=nix_tree_path.parent / "work",
            )
        else:
            nix_fd = _open_tree(libc, nix_tree_path)

        # Step 2: Open the container rootfs while still in the daemonset namespace.
        # /host/proc/{pid}/root reaches the container init's root; pre-pivot_root
        # that equals the host root, so appending {bundle}/rootfs gives the OCI rootfs.
        rootfs_path = f"{host_proc_path}/{container_pid}/root{bundle}/rootfs"
        rootfs_fd = os.open(rootfs_path, os.O_PATH | os.O_DIRECTORY)

        # Step 3: Enter the container's mount namespace.
        with open(f"{host_proc_path}/{container_pid}/ns/mnt", "rb") as f:
            ret = libc.setns(f.fileno(), CLONE_NEWNS)
        if ret != 0:
            errno = ctypes.get_errno()
            os.close(rootfs_fd)
            raise OSError(errno, f"setns({container_pid}): {os.strerror(errno)}")

        # Step 4: Enter the container's rootfs using the fd opened before setns.
        os.fchdir(rootfs_fd)
        os.close(rootfs_fd)
        os.chroot(".")

        # Step 5: Attach /nix.
        nix_dest = Path("/nix")
        nix_dest.mkdir(exist_ok=True)
        _move_mount(libc, nix_fd, nix_dest)
        os.close(nix_fd)

        if not nix_rw:
            # Bind clone is RW by default; flip it to RO.
            # MS_BIND | MS_REMOUNT | MS_RDONLY is the documented way to change
            # only the RO flag on an existing bind-like mount.
            ret = libc.mount(
                None, b"/nix", None, MS_BIND | MS_REMOUNT | MS_RDONLY, None
            )
            if ret != 0:
                errno = ctypes.get_errno()
                # Unmount so we don't leave a writable /nix exposed
                libc.umount2(b"/nix", 0)
                raise OSError(errno, f"remount /nix RO: {os.strerror(errno)}")

        # Step 6: Bind-mount each FHS store path (read-write) into the container.
        # /nix is mounted now, so /nix/store/... source paths are reachable.
        # Resolve symlinks after chroot so they follow the container's view.
        for src, dst in store_mounts:
            src = src.resolve()
            if src.is_dir():
                dst.mkdir(parents=True, exist_ok=True)
            else:
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.touch()
            ret = libc.mount(
                os.fsencode(src),
                os.fsencode(dst),
                None,
                MS_BIND,
                None,
            )
            if ret != 0:
                errno = ctypes.get_errno()
                raise OSError(
                    errno,
                    f"mount(bind {src!r} → {dst!r}): {os.strerror(errno)}",
                )

        result_queue.put(None)
    except Exception as e:  # noqa: BLE001 -- forwarded to the parent process through the queue, traceback attached
        e.__notes__ = [traceback.format_exc()]
        result_queue.put(e)


async def mount_in_container(
    container_pid: int,
    bundle: str,
    nix_tree_path: Path,
    store_mounts: list[tuple[Path, Path]],
    nix_rw: bool = False,
) -> None:
    """Mount /nix and FHS store paths inside a container's mount namespace.

    nix_tree_path: prepared /nix tree (lowerdir for overlayfs or bind source)
    store_mounts:  additional (src, dst) pairs for bind mounts (read-write)
    nix_rw:        True → RW overlayfs over nix_tree_path; False → RO bind clone
    Raises the worker's original exception (with traceback) on failure.
    """
    ctx = multiprocessing.get_context("spawn")
    result_queue: multiprocessing.Queue = ctx.Queue()

    proc = ctx.Process(
        target=_mount_worker,
        args=(
            container_pid,
            bundle,
            nix_tree_path,
            store_mounts,
            nix_rw,
            result_queue,
            HOST_PROC_PATH,
        ),
        daemon=True,
    )
    proc.start()
    await asyncio.to_thread(proc.join)

    result = result_queue.get_nowait()
    if isinstance(result, Exception):
        logger.error("worker_spawn_failed", exc_info=result)
        raise result

    logger.info(
        "mounted_nix",
        mode="rw_overlayfs" if nix_rw else "ro_bind",
        store_mounts=len(store_mounts),
        pid=container_pid,
    )
