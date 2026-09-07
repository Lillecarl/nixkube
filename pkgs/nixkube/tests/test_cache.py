# SPDX-License-Identifier: MIT

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from src.subprocessing import SubprocessResult

STORE_PATH = Path("/nix/store/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-test-1.0")
PATHS = {STORE_PATH}


def ok(stdout: str = "") -> SubprocessResult:
    return SubprocessResult(
        returncode=0, stdout=stdout, stderr="", combined="", elapsed=0.0
    )


def fail() -> SubprocessResult:
    return SubprocessResult(
        returncode=1, stdout="", stderr="error", combined="error", elapsed=0.0
    )


def make_mock_run(copy_results: list[SubprocessResult]):
    """Return an async mock for run_captured that routes by nix subcommand."""
    copy_iter = iter(copy_results)

    async def mock_run(*args, **kwargs):
        args_list = list(args)
        if "path-info" in args_list:
            if "--derivation" in args_list:
                return ok("")
            return ok(str(STORE_PATH))
        if "sign" in args_list:
            return ok()
        if "copy" in args_list:
            return next(copy_iter)
        return ok()

    return mock_run


class TestCopyToCacheRetry:
    """Tests for copy_to_cache() retry loop and backoff behavior."""

    @pytest.fixture(autouse=True)
    def pynixd_enabled(self, monkeypatch):
        """There is a cache to copy to. Without one copy_to_cache returns at once."""
        monkeypatch.setattr("src.cache.PYNIXD_ENABLED", True)

    @pytest.mark.asyncio
    async def test_empty_paths_returns_early(self):
        """Empty package_paths should skip all subprocess calls."""
        from src.cache import copy_to_cache

        with patch("src.cache.run_captured", new_callable=AsyncMock) as mock_run:
            await copy_to_cache(set())
            mock_run.assert_not_called()

    @pytest.mark.asyncio
    async def test_first_attempt_success_no_sleep(self):
        """Successful first copy attempt should not trigger any sleep."""
        from src.cache import copy_to_cache

        mock_run = make_mock_run([ok()])
        with (
            patch("src.cache.run_captured", side_effect=mock_run),
            patch("src.cache.sleep", new_callable=AsyncMock) as mock_sleep,
        ):
            await copy_to_cache(PATHS)
            mock_sleep.assert_not_called()

    @pytest.mark.asyncio
    async def test_all_attempts_fail_sleeps_five_times(self):
        """All 6 copy attempts failing should sleep 5 times (not before attempt 0)."""
        from src.cache import copy_to_cache

        mock_run = make_mock_run([fail()] * 6)
        with (
            patch("src.cache.run_captured", side_effect=mock_run),
            patch("src.cache.sleep", new_callable=AsyncMock) as mock_sleep,
        ):
            await copy_to_cache(PATHS)
            assert mock_sleep.call_count == 5

    @pytest.mark.asyncio
    async def test_retry_succeeds_on_third_attempt(self):
        """Two failures then success → only 2 sleeps."""
        from src.cache import copy_to_cache

        mock_run = make_mock_run([fail(), fail(), ok()])
        with (
            patch("src.cache.run_captured", side_effect=mock_run),
            patch("src.cache.sleep", new_callable=AsyncMock) as mock_sleep,
        ):
            await copy_to_cache(PATHS)
            assert mock_sleep.call_count == 2

    @pytest.mark.asyncio
    async def test_backoff_values(self):
        """Verify exponential backoff sequence: 5, 10, 20, 40, 60 seconds."""
        from src.cache import copy_to_cache

        sleep_calls: list[float] = []

        async def track_sleep(secs: float) -> None:
            sleep_calls.append(secs)

        mock_run = make_mock_run([fail()] * 6)
        with (
            patch("src.cache.run_captured", side_effect=mock_run),
            patch("src.cache.sleep", side_effect=track_sleep),
        ):
            await copy_to_cache(PATHS)

        assert sleep_calls == [5, 10, 20, 40, 60]

    @pytest.mark.asyncio
    async def test_sign_failure_does_not_abort_copy(self):
        """A sign failure should log a warning but still attempt the copy."""
        from src.cache import copy_to_cache

        sign_called = False
        copy_called = False

        async def mock_run(*args, **kwargs):
            nonlocal sign_called, copy_called
            args_list = list(args)
            if "path-info" in args_list:
                return ok(str(STORE_PATH))
            if "sign" in args_list:
                sign_called = True
                return fail()
            if "copy" in args_list:
                copy_called = True
                return ok()
            return ok()

        with (
            patch("src.cache.run_captured", side_effect=mock_run),
            patch("src.cache.sleep", new_callable=AsyncMock),
        ):
            await copy_to_cache(PATHS)

        assert sign_called
        assert copy_called


class TestCopyToCacheWithoutPynixd:
    """copy_to_cache() when the deployment has no cache at all."""

    @pytest.fixture(autouse=True)
    def pynixd_disabled(self, monkeypatch):
        monkeypatch.setattr("src.cache.PYNIXD_ENABLED", False)

    @pytest.mark.asyncio
    async def test_paths_run_nothing(self):
        """No cache means no sign, no copy and no retries."""
        from src.cache import copy_to_cache

        with (
            patch("src.cache.run_captured", new_callable=AsyncMock) as mock_run,
            patch("src.cache.sleep", new_callable=AsyncMock) as mock_sleep,
        ):
            await copy_to_cache(PATHS)

        mock_run.assert_not_called()
        mock_sleep.assert_not_called()

    @pytest.mark.asyncio
    async def test_all_paths_run_nothing(self):
        """The GC pass asks for every path in the store. It gets the same answer."""
        from src.cache import copy_to_cache

        with (
            patch("src.cache.run_captured", new_callable=AsyncMock) as mock_run,
            patch("src.cache.sleep", new_callable=AsyncMock) as mock_sleep,
        ):
            await copy_to_cache(None)

        mock_run.assert_not_called()
        mock_sleep.assert_not_called()
