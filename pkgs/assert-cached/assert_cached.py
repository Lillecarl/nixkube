# SPDX-License-Identifier: MIT

"""Assert that every store path a manifest names can be fetched.

A nixkube CSI volume names an output path, so a node substitutes it and can
never build it. A path no substituter serves is a mount that fails.

Three properties, each measured against a case that breaks without it:

- Walks the closure. A present top path with an absent member is issue #8.
- Unions the substituters. `cacheEnv` is on nix-csi.cachix.org; its member
  `python3.14-httpx` is only on cache.nixos.org. Either alone reports a
  false failure.
- Asks the caches, never the local store, so a path this machine happens to
  have built does not pass.
"""

import argparse
import os
import re
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

STORE_PATH = re.compile(r"/nix/store/([a-z0-9]{32})-([A-Za-z0-9._+?=-]*)")

# The wrapper sets this from the module, so the deployed substituter list and
# the checked one cannot drift. Kept an env var rather than a substituted
# placeholder so this file stays valid Python that ruff and a shell can run.
DEFAULT_SUBSTITUTERS = [
    s for s in os.environ.get("NIXKUBE_SUBSTITUTERS", "").split() if s
]

# `[ \t]*`, not `\s*`: `\s` matches the newline, so an empty `References:`
# line swallows the next one and `Deriver:` is read as a store path.
REFERENCES = re.compile(r"^References:[ \t]*(.*)$", re.MULTILINE)


# Cachix answers 403 to the default `Python-urllib/3.x` agent for every path,
# present or absent. Measured against one curl fetches with 200. Without this
# the gate reports everything missing.
USER_AGENT = "nixkube-assert-cached (+https://github.com/Lillecarl/nixkube)"


class Indeterminate(Exception):
    """A substituter answered something other than 200 or 404."""


def ask_one(base: str, digest: str, timeout: float) -> str | None:
    """The narinfo from this substituter, or None when it answers 404."""
    url = f"{base.rstrip('/')}/{digest}.narinfo"
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if response.status == 200:
                return response.read().decode("utf-8", "replace")
            raise Indeterminate(f"{url} returned {response.status}")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise Indeterminate(f"{url} returned {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise Indeterminate(f"{url} unreachable: {exc}") from exc


def fetch_narinfo(substituters: list[str], digest: str, timeout: float) -> str | None:
    """The first substituter serving this path, or None if none does.

    Raises Indeterminate rather than folding "could not tell" into "absent":
    a gate that conflates them fails, or passes, for no reason.
    """
    unclear: list[str] = []
    for base in substituters:
        try:
            narinfo = ask_one(base, digest, timeout)
        except Indeterminate as exc:
            unclear.append(str(exc))
            continue
        if narinfo is not None:
            return narinfo
    if unclear:
        raise Indeterminate("; ".join(unclear))
    return None


def references_of(narinfo: str) -> set[str]:
    match = REFERENCES.search(narinfo)
    if not match:
        return set()
    return {ref.split("-", 1)[0] for ref in match.group(1).split() if ref}


def walk(
    roots: set[str], substituters: list[str], jobs: int, timeout: float
) -> tuple[set[str], set[str], dict[str, str]]:
    """Return (reachable, missing, unclear) digests, closure included."""
    seen: set[str] = set()
    missing: set[str] = set()
    unclear: dict[str, str] = {}
    frontier = set(roots)

    def ask(digest: str) -> tuple[str, str | None, str | None]:
        try:
            return digest, fetch_narinfo(substituters, digest, timeout), None
        except Indeterminate as exc:
            return digest, None, str(exc)

    with ThreadPoolExecutor(max_workers=jobs) as pool:
        while frontier:
            batch = sorted(frontier - seen)
            seen.update(batch)
            frontier = set()
            for digest, narinfo, problem in pool.map(ask, batch):
                if problem is not None:
                    unclear[digest] = problem
                    continue
                if narinfo is None:
                    missing.add(digest)
                    continue
                frontier.update(references_of(narinfo) - seen)

    return seen, missing, unclear


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Assert every store path a file names is fetchable, closure included."
    )
    parser.add_argument(
        "files",
        nargs="+",
        help="Rendered manifests, or any file naming store paths.",
    )
    parser.add_argument(
        "--substituter",
        action="append",
        default=[],
        dest="substituters",
        help="Repeatable. Defaults to the substituters nixkube configures.",
    )
    parser.add_argument("--jobs", type=int, default=16)
    parser.add_argument("--timeout", type=float, default=20.0)
    args = parser.parse_args()

    substituters = args.substituters or DEFAULT_SUBSTITUTERS
    if not substituters:
        print(
            "error: no substituters. Pass --substituter, or set NIXKUBE_SUBSTITUTERS.",
            file=sys.stderr,
        )
        return 2

    roots: set[str] = set()
    names: dict[str, str] = {}
    for path in args.files:
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                text = handle.read()
        except OSError as exc:
            print(f"error: cannot read {path}: {exc}", file=sys.stderr)
            return 2
        for match in STORE_PATH.finditer(text):
            roots.add(match.group(1))
            names.setdefault(match.group(1), match.group(2))

    if not roots:
        print("error: no store paths found -- is this the right file?", file=sys.stderr)
        return 2

    print(f"asking {len(substituters)} substituter(s) about {len(roots)} named path(s)")
    for base in substituters:
        print(f"  {base}")

    seen, missing, unclear = walk(roots, substituters, args.jobs, args.timeout)

    if unclear:
        print(
            f"\nUNKNOWN: {len(unclear)} path(s) got neither a 200 nor a 404.",
            file=sys.stderr,
        )
        for digest, problem in sorted(unclear.items())[:10]:
            print(f"  {digest}: {problem}", file=sys.stderr)
        print(
            "\nNot reporting these as missing. A substituter that will not answer is\n"
            "a broken check, not a missing path, and the two need different fixes.",
            file=sys.stderr,
        )
        return 3

    if missing:
        print(
            f"\nFAIL: {len(missing)} of {len(seen)} paths in the closure are on no substituter.",
            file=sys.stderr,
        )
        for digest in sorted(missing):
            named = " (named directly)" if digest in roots else ""
            print(
                f"  /nix/store/{digest}-{names.get(digest, '?')}{named}",
                file=sys.stderr,
            )
        print(
            "\nA node cannot build these: a CSI volume names an output path, so it can\n"
            "only be substituted. Push them before deploying or releasing.",
            file=sys.stderr,
        )
        return 1

    print(f"\nOK: {len(seen)} paths, whole closure, all fetchable.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
