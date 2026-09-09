# SPDX-License-Identifier: MIT

import re
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

STORE_PATH_RE = re.compile(r"/nix/store/[a-z0-9]{32}-[^\s/]+")


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
