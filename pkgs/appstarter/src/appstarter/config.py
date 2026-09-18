# SPDX-License-Identifier: MIT
"""What the two modes read from the environment, and what they leave behind."""

import json
import os
import platform
from dataclasses import dataclass
from pathlib import Path

# Written by `init` into the store it filled, and read by `run` out of the
# same store mounted at `/nix`. It is the only thing the two modes share:
# they run in different containers, and only one of them knows what was asked
# for.
STATE_PATH = Path("nix/var/appstarter/state.json")

# Where `init` points the store's "this is the current version" symlink, and
# where `run` looks for the program. Relative to the store root.
RESULT_PATH = Path("nix/var/result")


def nix_system() -> str:
    """This machine, spelled the way Nix spells it."""
    machine = platform.machine()
    return f"{'x86_64' if machine == 'AMD64' else machine}-{platform.system().lower()}"


def store_path_for(value: str) -> str:
    """One store path out of `APPSTARTER_WANTED`.

    The deployment names one environment per architecture, because one
    DaemonSet runs on all of them. A plain store path is accepted too, which
    is what a test or a single-architecture deployment writes.
    """
    value = value.strip()
    if not value.startswith("{"):
        return value

    by_system = json.loads(value)
    system = nix_system()
    if system not in by_system:
        raise KeyError(f"no store path for {system} in APPSTARTER_WANTED")
    return by_system[system]


@dataclass(frozen=True)
class State:
    """What `init` did, for `run` to report."""

    wanted: str
    running: str

    @property
    def degraded(self) -> bool:
        return self.wanted != self.running

    def write(self, store: Path) -> None:
        path = store / STATE_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        staging = path.with_suffix(".new")
        staging.write_text(json.dumps({"wanted": self.wanted, "running": self.running}))
        os.replace(staging, path)

    @classmethod
    def read(cls, store: Path) -> "State | None":
        """What `init` recorded, or `None` where it recorded nothing.

        `None` is not an error. `run` starts what the store holds either way;
        it only loses the ability to say which version that is.
        """
        try:
            recorded = json.loads((store / STATE_PATH).read_text())
        except (OSError, ValueError):
            return None
        try:
            return cls(wanted=recorded["wanted"], running=recorded["running"])
        except (KeyError, TypeError):
            return None
