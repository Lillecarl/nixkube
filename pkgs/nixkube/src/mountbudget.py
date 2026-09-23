# SPDX-License-Identifier: MIT

"""How many mounts this namespace may still make.

`fs.mount-max` is 100,000 and it counts **per mount namespace**. A bind farm
spends one per closure path -- 2443 for a measured node closure -- so the
question "will the next container fit" has a number, and a node that is
running out should say so long before a mount fails.

The mounts a farm makes live in the worker's own namespace, which starts as a
copy of the daemon's and dies with the worker. So the daemon's count is what
the worker inherits, and predicting from here is predicting the right thing.

A mount that fails for want of budget answers ENOSPC from mount(2), with
nothing to say which limit it was. That is the error this exists to replace.
"""

from dataclasses import dataclass
from pathlib import Path

import structlog

from .constants import MOUNT_BUDGET_WARN_RATIO
from .metrics import MOUNT_BUDGET_REFUSALS, MOUNT_NAMESPACE_LIMIT, MOUNT_NAMESPACE_USED

logger = structlog.get_logger("nixkube.mountbudget")

MOUNT_MAX_PATH = Path("/proc/sys/fs/mount-max")
MOUNTINFO_PATH = Path("/proc/self/mountinfo")

# What the kernel uses when `fs.mount-max` cannot be read. Documented default.
_FALLBACK_LIMIT = 100_000


@dataclass(frozen=True)
class Budget:
    """What this namespace has spent, and what it may still spend."""

    used: int
    limit: int
    needed: int

    @property
    def free(self) -> int:
        return self.limit - self.used

    @property
    def fits(self) -> bool:
        return self.needed <= self.free

    @property
    def ratio(self) -> float:
        """Share of the limit this request would leave used, 0.0 to above 1.0."""
        if self.limit <= 0:
            return 1.0
        return (self.used + self.needed) / self.limit


def _read_int(path: Path, fallback: int) -> int:
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        logger.warning("mount_budget_unreadable", path=str(path), exc_info=True)
        return fallback


def mount_limit() -> int:
    """`fs.mount-max` for this namespace."""
    return _read_int(MOUNT_MAX_PATH, _FALLBACK_LIMIT)


def mounts_used(mountinfo: Path | None = None) -> int:
    """How many mounts this namespace already holds."""
    try:
        return sum(1 for _ in (mountinfo or MOUNTINFO_PATH).read_text().splitlines())
    except OSError:
        logger.warning("mount_budget_unreadable", path="mountinfo", exc_info=True)
        return 0


def measure(needed: int, mountinfo: Path | None = None) -> Budget:
    """The budget for a request of `needed` mounts. Records it and says so.

    Loud on purpose past `MOUNT_BUDGET_WARN_RATIO`. Nothing outside the node
    can see a mount table fill up, and the first symptom otherwise is a
    container that will not start.
    """
    budget = Budget(used=mounts_used(mountinfo), limit=mount_limit(), needed=needed)
    MOUNT_NAMESPACE_USED.set(budget.used)
    MOUNT_NAMESPACE_LIMIT.set(budget.limit)

    if not budget.fits:
        MOUNT_BUDGET_REFUSALS.inc()
        logger.error(
            "mount_budget_exhausted",
            used=budget.used,
            limit=budget.limit,
            needed=budget.needed,
            free=budget.free,
        )
    elif budget.ratio >= MOUNT_BUDGET_WARN_RATIO:
        logger.error(
            "mount_budget_low",
            used=budget.used,
            limit=budget.limit,
            needed=budget.needed,
            would_use=round(budget.ratio, 3),
            warn_at=MOUNT_BUDGET_WARN_RATIO,
        )
    else:
        logger.debug(
            "mount_budget",
            used=budget.used,
            limit=budget.limit,
            needed=budget.needed,
        )

    return budget
