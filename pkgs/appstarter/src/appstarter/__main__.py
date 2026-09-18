# SPDX-License-Identifier: MIT
"""`appstarter init` and `appstarter run`."""

import argparse
import logging
import os
import sys
from pathlib import Path

from . import seed, start, store

# The initContainer sees the node's store here, because the image's own store
# is at /nix and a container cannot have two. The main container sees the same
# store at /nix, because by then it is the only one that matters.
_INIT_STORE = Path("/nix-volume")
_RUN_STORE = Path("/nix")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="appstarter", description=__doc__)
    modes = parser.add_subparsers(dest="mode", required=True)

    init = modes.add_parser(
        "init", help="fetch the wanted version into the node's store"
    )
    init.add_argument(
        "--store",
        type=Path,
        default=Path(os.environ.get("APPSTARTER_STORE", _INIT_STORE)),
        help="where the node's store is mounted",
    )

    run = modes.add_parser("run", help="start the application the store holds")
    run.add_argument("program", help="the program to exec out of the store")
    run.add_argument("argv", nargs=argparse.REMAINDER, help="its arguments")
    run.add_argument("--store", type=Path, default=_RUN_STORE)

    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=os.environ.get("APPSTARTER_LOG_LEVEL", "INFO"),
        format="%(name)s %(levelname)s %(message)s",
    )
    args = _parser().parse_args(argv)

    if args.mode == "run":
        return start.run(args.program, args.argv, args.store)

    wanted = os.environ.get("APPSTARTER_WANTED")
    if not wanted:
        print("APPSTARTER_WANTED is unset; nothing to fetch", file=sys.stderr)
        return 2
    try:
        return seed.run(wanted, os.environ.get("APPSTARTER_FALLBACK"), args.store)
    except (store.StoreError, KeyError, ValueError) as error:
        print(f"appstarter init: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
