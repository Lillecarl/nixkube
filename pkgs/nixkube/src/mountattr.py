# SPDX-License-Identifier: MIT

"""mount_setattr(2): the only way to make a mount subtree read-only.

`MS_BIND | MS_REMOUNT | MS_RDONLY` changes the top mount and nothing under
it. Measured on Linux 6.18 against a directory holding three bind mounts: the
submounts stayed writable, and a write through one reaches the host store by
way of the shared inode. `mount_setattr` with `AT_RECURSIVE` covers the
subtree (Linux 5.12).
"""

import ctypes
import ctypes.util
import errno
import os
from pathlib import Path

# Introduced via the common syscall table, so x86_64 and aarch64 share it.
_NR_MOUNT_SETATTR = 442

MOUNT_ATTR_RDONLY = 0x1
AT_RECURSIVE = 0x8000
AT_FDCWD = -100

_libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
_libc.syscall.restype = ctypes.c_long


class MountAttr(ctypes.Structure):
    """struct mount_attr, the argument to mount_setattr(2)."""

    _fields_ = [
        ("attr_set", ctypes.c_uint64),
        ("attr_clr", ctypes.c_uint64),
        ("propagation", ctypes.c_uint64),
        ("userns_fd", ctypes.c_uint64),
    ]


class MountSetattrUnsupported(OSError):
    """The kernel has no mount_setattr. Raised only for ENOSYS."""


def set_readonly(path: Path | str, recursive: bool) -> None:
    """Make the mount at `path` read-only, and everything under it if asked.

    Raises MountSetattrUnsupported on a kernel before 5.12, so a caller with a
    fallback can tell that case from a real failure. Every other errno is an
    ordinary OSError.
    """
    attr = MountAttr(attr_set=MOUNT_ATTR_RDONLY, attr_clr=0, propagation=0, userns_fd=0)
    ret = _libc.syscall(
        ctypes.c_long(_NR_MOUNT_SETATTR),
        ctypes.c_int(AT_FDCWD),
        ctypes.c_char_p(os.fsencode(path)),
        ctypes.c_uint(AT_RECURSIVE if recursive else 0),
        ctypes.byref(attr),
        ctypes.c_size_t(ctypes.sizeof(attr)),
    )
    if ret == 0:
        return

    err = ctypes.get_errno()
    message = f"mount_setattr({path!s}, recursive={recursive}): {os.strerror(err)}"
    if err == errno.ENOSYS:
        raise MountSetattrUnsupported(err, message)
    raise OSError(err, message)
