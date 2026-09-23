# SPDX-License-Identifier: MIT

import subprocess
from functools import cache

from ..constants import NIX_SYSTEM_TIMEOUT
from ..errors import SystemDetectionError


@cache
def get_current_system() -> str:
    """Get system string evaluated by nix (cached after first call).

    Synchronous, and therefore deadlined harder than the async calls around
    it. This blocks the event loop rather than one task, so a `nix eval` that
    never returns takes the CSI server, the NRI plugin and the heartbeat with
    it. `--store dummy://` keeps it off the real store, and the cache means
    only the first call pays anything at all.
    """
    try:
        result = subprocess.run(
            [
                "nix",
                "eval",
                "--raw",
                "--impure",
                "--store",
                "dummy://",
                "--expr",
                "builtins.currentSystem",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=NIX_SYSTEM_TIMEOUT,
        )
        return result.stdout
    except subprocess.TimeoutExpired as e:
        raise SystemDetectionError(
            f"System detection timeout after {NIX_SYSTEM_TIMEOUT}s",
            logs=str(e),
        ) from e
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        raise SystemDetectionError(
            "Failed to detect system type",
            logs=str(e),
        ) from e
