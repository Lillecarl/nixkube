# SPDX-License-Identifier: MIT

"""SSH authorized key file watching and dynamic reloading.

Watches authorized_keys files for changes using inotify and applies
updates to the asyncssh server without restarting.
"""

import os
from pathlib import Path

import anyio
import structlog
from asyncinotify import Inotify, Mask
from pynixd.instance import Server

log = structlog.get_logger(__name__)

_DEFAULT_FILES = "/etc/ssh/authorized_keys:/etc/ssh-dynauth/authorized_keys"
_KEY_FILES = [
    Path(p)
    for p in os.environ.get("NIXKUBE_AUTHORIZED_KEYS_FILES", _DEFAULT_FILES).split(":")
    if p
]
_HOST_KEY = "/etc/ssh-key/id_ed25519"


def _collect_key_paths() -> list[str]:
    """The key files that hold something.

    **Size, and not only `is_file()`.** A 0-byte file is a file, and asyncssh
    refuses a key file it parsed zero entries from rather than skipping it:
    `ValueError: No valid entries found`, measured against asyncssh 2.24. One
    such file in the list aborts the whole `update()`, so the keys in the
    others never load either.

    An empty `pynixd.authorizedKeys` renders `/etc/ssh/authorized_keys` as
    exactly that file, which makes this the ordinary case rather than a
    broken one.
    """
    return [str(p) for p in _KEY_FILES if p.is_file() and p.stat().st_size > 0]


def apply_authorized_keys(server: Server) -> None:
    """Re-read authorized key files from disk and update the SSH server.

    Safe to call repeatedly — asyncssh's ``update()`` re-parses the files
    each time and applies changes to all future connections.
    """
    paths = _collect_key_paths()
    if server.ssh_server is None:
        return
    if paths:
        try:
            server.ssh_server.update(authorized_client_keys=paths)
        except ValueError:
            # A file of only comments parses to zero entries and raises the
            # same way an empty one does, and the filter above cannot see
            # that without parsing it. Logged and stepped over: the host keys
            # below are what let anything connect at all, and one unusable
            # authorized-keys file must not cost them.
            log.warning("authorized_keys_unusable", paths=paths, exc_info=True)
            paths = []
    server.ssh_server.update(server_host_keys=[_HOST_KEY])
    log.info("ssh_authorized_keys_loaded", count=len(paths))


async def watch_authorized_keys(server: Server) -> None:
    """Reload authorized keys on change, and never raise.

    It runs in the task group that holds the server, and a group cancels
    every sibling when a child raises -- so anything escaping here takes
    pynixd down. Reloading keys is a convenience; serving the store is not.

    Loud, though. Before the group it ran as a detached task whose exception
    nothing ever read, and an unusable key file was invisible for exactly
    that reason.
    """
    try:
        await _watch(server)
    except anyio.get_cancelled_exc_class():
        raise
    except Exception:
        log.exception("authorized_keys_watch_failed")


async def _watch(server: Server) -> None:
    """Kubernetes ConfigMap updates are atomic symlink swaps — the files stay
    at the same path but their target inode changes.  We watch the *parent
    directory* for ``MOVED_TO`` / ``CREATE``, then re-read the key files.
    """
    apply_authorized_keys(server)

    filenames = {p.name for p in _KEY_FILES}
    parents: set[Path] = set()
    for p in _KEY_FILES:
        parent = p.parent
        if parent.is_dir():
            parents.add(parent)

    with Inotify() as inotify:
        for d in parents:
            inotify.add_watch(d, Mask.MOVED_TO | Mask.CREATE)

        async for event in inotify:
            if event.name is None:
                continue
            if event.name.name in filenames:
                apply_authorized_keys(server)
