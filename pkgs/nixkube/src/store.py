# SPDX-License-Identifier: MIT

import re
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

# The name charset Nix itself allows, and not "anything but whitespace or a
# slash". The looser form swallowed whatever followed the path, and container
# env is JSON now -- `APPSTARTER_WANTED` is `builtins.toJSON` of a map from
# system to store path -- so it took the closing quote and brace with it:
#
#     {"x86_64-linux":"/nix/store/lqd2v8...-cacheEnv"}
#       -> /nix/store/lqd2v8...-cacheEnv"}
#
# The clean path matches as well, so the mangled one is pure noise. It is not
# harmless noise: `nix build` takes all of its arguments or none, so one path
# that cannot exist fails the build that fetches a node's environment, and
# `appstarter-init` never starts. Measured on a cluster as 240 restarts of
# `Init:CrashLoopBackOff`.
STORE_PATH_RE = re.compile(r"/nix/store/[a-z0-9]{32}-[a-zA-Z0-9+._?=-]+")


def _extract_store_paths(value: Any) -> Iterator[Path]:
    match value:
        case str():
            for m in STORE_PATH_RE.finditer(value):
                yield Path(m.group())
        case Mapping():
            for k, v in value.items():
                # volumeAttributes might contain multiarch paths which we don't want to include.
                # storePaths in volumeAttributes are handled as "primary package".
                if k != "volumeAttributes":
                    yield from _extract_store_paths(v)
        case Sequence():
            for item in value:
                yield from _extract_store_paths(item)


def extract_store_paths(value: Any) -> set[Path]:
    """Convenience wrapper that returns a deduplicated set of store paths."""
    return set(_extract_store_paths(value))


def extract_store_name(path: Path | str) -> str:
    """Extract the store name from a Nix store path.

    Strips the /nix/store/ prefix, returning just the hash-name portion.
    For example: /nix/store/abc123-hello → abc123-hello

    Args:
        path: A Nix store path (absolute or relative)

    Returns:
        The store name without the /nix/store/ prefix
    """
    return str(path).removeprefix("/nix/store/")
