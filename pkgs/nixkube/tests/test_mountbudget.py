# SPDX-License-Identifier: MIT

"""What a node says before its mount table fills up.

`fs.mount-max` counts per mount namespace and a bind farm spends one per
closure path. Past the limit, mount(2) answers ENOSPC and names no limit --
partway through a closure, leaving a store that is half there. Nothing
outside a node can see this coming, so the daemon has to say it.
"""

from pathlib import Path

import pytest
from structlog.testing import capture_logs

from src import mountbudget
from src.mountbudget import Budget, measure


def _mountinfo(tmp_path: Path, lines: int) -> Path:
    path = tmp_path / "mountinfo"
    path.write_text(
        "".join(f"{n} 1 0:1 / /m{n} rw - tmpfs tmpfs rw\n" for n in range(lines))
    )
    return path


@pytest.fixture
def limit(monkeypatch):
    def _set(value: int) -> None:
        monkeypatch.setattr(mountbudget, "mount_limit", lambda: value)

    return _set


class TestBudgetArithmetic:
    def test_it_fits_when_there_is_room(self):
        assert Budget(used=10, limit=100, needed=50).fits

    def test_it_does_not_fit_when_the_request_is_the_overflow(self):
        """91 into 90 free. The one that matters: the request itself decides."""
        assert not Budget(used=10, limit=100, needed=91).fits

    def test_exactly_filling_the_limit_still_fits(self):
        assert Budget(used=10, limit=100, needed=90).fits

    def test_a_limit_of_zero_does_not_divide_by_it(self):
        assert Budget(used=0, limit=0, needed=1).ratio == 1.0


class TestMeasure:
    """`capture_logs`, not `caplog`: these are structlog's loggers, and
    nothing in this project routes them through the stdlib handler `caplog`
    reads. A `caplog` assertion here passes against silence."""

    def test_a_request_that_fits_is_quiet(self, tmp_path, limit):
        limit(100_000)
        with capture_logs() as logs:
            budget = measure(2443, mountinfo=_mountinfo(tmp_path, 200))

        assert budget.fits
        assert [entry["event"] for entry in logs] == ["mount_budget"]

    def test_a_request_that_does_not_fit_says_so_at_error_level(self, tmp_path, limit):
        limit(1000)
        with capture_logs() as logs:
            budget = measure(2443, mountinfo=_mountinfo(tmp_path, 200))

        assert not budget.fits
        said = [entry for entry in logs if entry["event"] == "mount_budget_exhausted"]
        assert said, f"stayed quiet about a farm that cannot be built: {logs}"
        assert said[0]["log_level"] == "error"

    def test_approaching_the_limit_is_loud_before_it_fails(self, tmp_path, limit):
        """The point of the whole module: complain while it still works.

        7500 held plus 2443 wanted is 99% of 10,000 -- it fits, and the next
        container will not.
        """
        limit(10_000)
        with capture_logs() as logs:
            budget = measure(2443, mountinfo=_mountinfo(tmp_path, 7500))

        assert budget.fits, "this request still fits; that is what makes it a warning"
        said = [entry for entry in logs if entry["event"] == "mount_budget_low"]
        assert said, f"said nothing while the table filled: {logs}"
        assert said[0]["log_level"] == "error"

    def test_the_numbers_reach_the_log(self, tmp_path, limit):
        limit(1000)
        with capture_logs() as logs:
            measure(2443, mountinfo=_mountinfo(tmp_path, 200))

        said = next(
            entry for entry in logs if entry["event"] == "mount_budget_exhausted"
        )
        assert said["needed"] == 2443 and said["limit"] == 1000, (
            f"a reader needs the closure size and the limit, not the verdict alone:"
            f" {said}"
        )


class TestItNeverTakesTheCallerDown:
    """Reading /proc is not a reason to fail a container."""

    def test_an_unreadable_mountinfo_counts_zero(self, tmp_path):
        assert mountbudget.mounts_used(tmp_path / "not-there") == 0

    def test_an_unreadable_limit_falls_back_to_the_kernel_default(self, monkeypatch):
        monkeypatch.setattr(
            mountbudget, "MOUNT_MAX_PATH", Path("/nonexistent/mount-max")
        )
        assert mountbudget.mount_limit() == 100_000
