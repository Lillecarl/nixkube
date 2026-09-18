# SPDX-License-Identifier: MIT
"""The Nix operations `init` performs, and the check that they worked."""

import logging
import os
import subprocess
from pathlib import Path

log = logging.getLogger("appstarter.store")


class StoreError(RuntimeError):
    """A Nix operation failed, or left the store incomplete."""


def _run(*args: str, timeout: float | None = None) -> subprocess.CompletedProcess[str]:
    log.debug("running %s", " ".join(args))
    return subprocess.run(
        args, capture_output=True, text=True, check=False, timeout=timeout
    )


def ping(store: str, timeout: float = 30.0) -> bool:
    """Whether `store` answers a `nix store ping`."""
    try:
        return (
            _run("nix", "store", "ping", "--store", store, timeout=timeout).returncode
            == 0
        )
    except subprocess.TimeoutExpired:
        log.warning("%s did not answer a ping within %.0fs", store, timeout)
        return False


def build(target: str, into: Path, substituters: list[str], out_link: Path) -> None:
    """Realise `target` in the store at `into`, from `substituters`.

    `--out-link` writes the pointer the main container starts from. Nix
    replaces a symlink by renaming over it, so a restart part way through this
    reads the previous version rather than half of this one.
    """
    result = _run(
        "nix",
        "build",
        "--extra-substituters",
        " ".join(substituters),
        "--max-jobs",
        "auto",
        # A node's store is a chroot store this process owns. The Nix sandbox
        # wants a second one inside it, which needs mounts this container
        # cannot make.
        "--option",
        "sandbox",
        "false",
        "--store",
        str(into),
        "--out-link",
        str(out_link),
        # Build what no substituter has, rather than failing. A node that can
        # build is slower than one that substitutes and faster than one that
        # does not start.
        "--fallback",
        target,
    )
    if result.returncode != 0:
        raise StoreError(f"cannot get {target}: {result.stderr.strip()}")


def copy(target: str, into: Path, out_link: Path) -> None:
    """Copy `target` out of this container's own store into the one at `into`.

    The fallback. It reads no substituter and cannot fail for a reason outside
    this pod, which is the whole point of it.
    """
    result = _run("nix", "copy", "--no-check-sigs", "--to", str(into), target)
    if result.returncode != 0:
        raise StoreError(f"cannot copy {target}: {result.stderr.strip()}")
    link(target, out_link)


def link(target: str, out_link: Path) -> None:
    """Point `out_link` at `target`, atomically.

    `os.replace` on a symlink is a rename, so a reader sees the old target or
    the new one and never a missing link. `nix build --out-link` does the same
    thing, and this is for the copy path, which has no `--out-link`.
    """
    out_link.parent.mkdir(parents=True, exist_ok=True)
    staging = out_link.with_name(f".{out_link.name}.new")
    staging.unlink(missing_ok=True)
    staging.symlink_to(target)
    os.replace(staging, out_link)


def verify(target: str, into: Path) -> None:
    """Check that every path of `target`'s closure is on disk under `into`.

    **`nix path-info --recursive` alone is not enough.** It answers from the
    database, so a path that is registered and absent passes -- which is issue
    #8: a node that starts a container which cannot exec out of
    `/nix/var/result/bin`, and reports a `$PATH` that means nothing. Nothing is
    hashed here; `nix store verify` would, and costs minutes on a closure this
    size at every node start.

    `-e` alone is wrong, because a store path can itself be a symlink to an
    absolute path that resolves only once the store is mounted at `/nix`.
    `nix-*-man` is one, and it is why `os.path.lexists` and not `Path.exists`.
    """
    result = _run("nix", "path-info", "--store", str(into), "--recursive", target)
    if result.returncode != 0:
        raise StoreError(f"cannot list {target}: {result.stderr.strip()}")

    missing = [
        path for path in result.stdout.split() if not os.path.lexists(f"{into}{path}")
    ]
    if missing:
        for path in missing:
            log.error("registered and absent: %s", path)
        raise StoreError(f"{len(missing)} of {target}'s closure are absent from {into}")
