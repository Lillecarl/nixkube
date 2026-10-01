# SPDX-License-Identifier: MIT

"""A CSI volume as a composefs image. Issues #68 and #25.

A composefs mount is one overlay mount: an EROFS image holds the tree, and
each regular file's content is a file under a base directory. With
/nix/store as that directory, a volume is one mount that reads straight from
the store: no mount per store path, no hardlink out of the store, and real
files that kubelet's subPath -- a non-recursive bind -- carries.

The image is written from a composefs-dump(5) text file, which this module
writes from the closure. The layout is a volume root:

    /<the primary package's tree>   symlinks followed, for subPath
    /nix/store/<every closure path>
    /nix/var/result                 -> the primary package
    /nix/var/nix/db/*               the closure's Nix database

`nix/composefs.nix` runs `main` in a derivation, so an image is built once
and substituted everywhere after.
"""

from __future__ import annotations

import argparse
import os
import stat
import sys
from pathlib import Path
from typing import TextIO

STORE = Path("/nix/store")

# A store path's files are root's, and a fixed time keeps the image the same
# for the same closure.
_MTIME = "1.0"


def escape(text: str) -> str:
    """A dump field: every byte outside printable ASCII, and the separators
    backslash and '=', as \\xNN. Space is outside the range, so it is too."""
    return "".join(
        chr(b) if 0x21 <= b <= 0x7E and b not in (0x5C, 0x3D) else f"\\x{b:02x}"
        for b in text.encode()
    )


class Dump:
    """Writes the lines of one image. A directory comes before what is in it,
    as composefs-dump(5) requires, because each walk writes the parent first."""

    def __init__(self, out: TextIO, store: Path = STORE) -> None:
        self.out = out
        self.store = store
        self.seen: set[str] = set()

    def _line(self, path: str, size: int, mode: int, nlink: int, payload: str) -> None:
        if path in self.seen:
            raise ValueError(f"{path} is in the image twice")
        self.seen.add(path)
        # PATH SIZE MODE NLINK UID GID RDEV MTIME PAYLOAD CONTENT DIGEST
        self.out.write(
            f"{escape(path)} {size} {mode:o} {nlink} 0 0 0 {_MTIME} {payload} - -\n"
        )

    def directory(self, path: str, mode: int = stat.S_IFDIR | 0o555) -> None:
        self._line(path, 0, mode, 2, "-")

    def symlink(self, path: str, target: str) -> None:
        self._line(path, len(target.encode()), stat.S_IFLNK | 0o777, 1, escape(target))

    def file(self, path: str, real: Path, st: os.stat_result) -> None:
        """A regular file whose content is `real`, which must be in the store."""
        payload = escape(str(real.relative_to(self.store)))
        self._line(path, st.st_size, st.st_mode, 1, payload)

    def tree(self, path: str, real: Path) -> None:
        """`real` as it is: a symlink stays a symlink."""
        st = real.lstat()
        if stat.S_ISLNK(st.st_mode):
            self.symlink(path, os.readlink(real))
        elif stat.S_ISDIR(st.st_mode):
            self.directory(path, st.st_mode)
            with os.scandir(real) as entries:
                for entry in sorted(entries, key=lambda e: e.name):
                    self.tree(f"{path}/{entry.name}", Path(entry.path))
        else:
            self.file(path, real, st)

    def deref(self, path: str, real: Path, depth: int = 0) -> None:
        """`real` with every symlink into the store followed, as subPath needs:
        it refuses a symlink. One that leaves the store, or points nowhere,
        stays a symlink."""
        if depth > 40:
            raise ValueError(f"symlinks under {real} do not end")
        resolved = real.resolve()
        inside = resolved.is_relative_to(self.store)
        if real.is_symlink() and (not inside or not resolved.exists()):
            self.symlink(path, os.readlink(real))
        elif resolved.is_dir():
            if path:
                self.directory(path)
            with os.scandir(resolved) as entries:
                for entry in sorted(entries, key=lambda e: e.name):
                    self.deref(f"{path}/{entry.name}", Path(entry.path), depth + 1)
        else:
            self.file(path, resolved, resolved.stat())


def volume(
    out: TextIO,
    closure: list[Path],
    primary: Path | None,
    database: Path | None,
    store: Path = STORE,
) -> None:
    """The dump of one volume. `database` is a directory whose files become
    /nix/var/nix/db; it must be in the store, like everything else."""
    dump = Dump(out, store)
    dump.directory("/")
    if primary is not None:
        dump.deref("", primary)
    if "/nix" in dump.seen:
        raise ValueError(f"{primary} has a /nix of its own, and the store goes there")
    dump.directory("/nix")
    dump.directory("/nix/store")
    for path in sorted(closure):
        dump.tree(f"/nix/store/{path.name}", path)
    dump.directory("/nix/var")
    if primary is not None:
        dump.symlink("/nix/var/result", str(primary))
    if database is not None:
        dump.directory("/nix/var/nix")
        dump.directory("/nix/var/nix/db")
        for entry in sorted(database.iterdir()):
            dump.file(f"/nix/var/nix/db/{entry.name}", entry, entry.stat())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nixkube-composefs-dump", description=__doc__)
    parser.add_argument(
        "--closure", type=Path, required=True, help="one store path per line"
    )
    parser.add_argument("--primary", type=Path, help="its tree goes at the root")
    parser.add_argument("--database", type=Path, help="the files of /nix/var/nix/db")
    args = parser.parse_args(argv)
    closure = [Path(line) for line in args.closure.read_text().split()]
    volume(sys.stdout, closure, args.primary, args.database)
    return 0


if __name__ == "__main__":
    sys.exit(main())
