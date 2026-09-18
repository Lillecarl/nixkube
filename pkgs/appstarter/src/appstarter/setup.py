# SPDX-License-Identifier: MIT
"""What the container needs before Nix runs in it."""

import logging
import os
from pathlib import Path

log = logging.getLogger("appstarter.setup")

# `nix build` writes here, and an image that is a store and nothing else has
# neither directory.
_DIRECTORIES = (("tmp", 0o1777), ("var/tmp", 0o1777))

# The three files Nix resolves a user through. `FAKE_NSS` is unset in a test
# and in a dev shell, where the host already has them.
_NSS_FILES = ("passwd", "group", "nsswitch.conf")


def prepare(root: Path = Path("/")) -> None:
    """Give Nix a writable `/tmp` and a user database.

    Nothing here overwrites a file that exists. A node that mounts its own
    `/etc/passwd` keeps it.
    """
    for path, mode in _DIRECTORIES:
        directory = root / path
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(mode)

    source = os.environ.get("FAKE_NSS")
    if not source:
        log.debug("FAKE_NSS is unset; leaving %s alone", root / "etc")
        return

    for name in _NSS_FILES:
        origin = Path(source) / "etc" / name
        destination = root / "etc" / name
        if not origin.exists() or destination.exists():
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(origin.read_text())
        log.info("installed /etc/%s", name)
