# SPDX-License-Identifier: MIT

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from src.cache import check_cache_connectivity
from src.constants import CACHE_PING_TIMEOUT_SECONDS
from src.errors import CommandTimeoutError
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


@pytest.mark.parametrize(
    "error",
    [
        CommandTimeoutError(
            returncode=124,
            stdout="",
            stderr="",
            combined="",
            command=["nix", "store", "ping"],
        ),
        OSError("no such host"),
        TimeoutError(),
        RuntimeError("something nobody predicted"),
    ],
)
@pytest.mark.asyncio
async def test_a_raising_ping_is_false_and_not_an_error(error):
    """Every `NodePublishVolume` asks this, so raising stops every mount.

    `CommandTimeoutError` is the one that fires in practice: shellous
    suppresses the TimeoutError and `run_captured` raises rc=124 instead.
    A cluster with no DNS for `pynixd` left every workload in
    ContainerCreating until the UML test gave up.
    """

    async def raises(*_args, **_kwargs):
        raise error

    with (
        patch("src.cache.PYNIXD_ENABLED", True),
        patch("src.cache.run_captured", new=AsyncMock(side_effect=raises)),
    ):
        assert await check_cache_connectivity() is False


@pytest.mark.parametrize(
    ("result", "why"),
    [
        (fail(), "nix exited non-zero"),
        (ok("not json at all"), "the JSON it promised was not JSON"),
        (ok(""), "it said nothing"),
    ],
)
@pytest.mark.asyncio
async def test_only_a_clean_answer_is_yes(result, why):
    """Anything short of success means "do not add pynixd as a substituter"."""
    with (
        patch("src.cache.PYNIXD_ENABLED", True),
        patch("src.cache.run_captured", new=AsyncMock(return_value=result)),
    ):
        assert await check_cache_connectivity() is False, why


@pytest.mark.asyncio
async def test_a_cache_that_answers_is_true():
    with (
        patch("src.cache.PYNIXD_ENABLED", True),
        patch("src.cache.run_captured", new=AsyncMock(return_value=ok("{}"))),
    ):
        assert await check_cache_connectivity() is True


@pytest.mark.asyncio
async def test_the_ping_carries_a_short_timeout():
    """The wait is paid per mount, so it cannot be the build timeout."""
    seen: dict[str, float | None] = {}

    async def record(*_args, timeout=None, **_kwargs):
        seen["timeout"] = timeout
        return ok("{}")

    with (
        patch("src.cache.PYNIXD_ENABLED", True),
        patch("src.cache.run_captured", side_effect=record),
    ):
        await check_cache_connectivity()

    assert seen["timeout"] == CACHE_PING_TIMEOUT_SECONDS
    assert 0 < CACHE_PING_TIMEOUT_SECONDS <= 15


@pytest.mark.asyncio
async def test_no_cache_asks_nothing():
    with (
        patch("src.cache.PYNIXD_ENABLED", False),
        patch("src.cache.run_captured", new_callable=AsyncMock) as mock_run,
    ):
        assert await check_cache_connectivity() is False
        mock_run.assert_not_called()
