# SPDX-License-Identifier: MIT

import asyncio
import logging  # for log level constants (logging.DEBUG, logging.NOTSET, etc.)
import shlex
import time
from typing import NamedTuple

import structlog
from shellous import sh

from .errors import CommandTimeoutError, SubprocessError

logger = structlog.get_logger("nixkube.subprocessing")


class SubprocessResult(NamedTuple):
    returncode: int
    stdout: str
    stderr: str
    combined: str
    elapsed: float


async def try_captured(*args, timeout: float | None = None) -> SubprocessResult:
    """Run a command, capture output, and raise SubprocessError if it fails.

    Args:
        *args: Command and arguments
        timeout: Optional timeout in seconds (raises CommandTimeoutError on timeout)

    Returns:
        SubprocessResult with stdout, stderr, combined output, and elapsed time

    Raises:
        SubprocessError: If the command returns non-zero exit code
        CommandTimeoutError: If timeout is exceeded
    """
    result = await run_captured(*args, timeout=timeout)
    if result.returncode != 0:
        raise SubprocessError(
            result.returncode,
            result.stdout,
            result.stderr,
            result.combined,
            list(args),
        )
    return result


async def try_console(
    *args, log_level: int = logging.DEBUG, timeout: float | None = None
) -> SubprocessResult:
    """Run a command with output forwarded to logs, and raise SubprocessError if it fails.

    Args:
        *args: Command and arguments
        log_level: Logging level for output (default DEBUG)
        timeout: Optional timeout in seconds (raises CommandTimeoutError on timeout)

    Returns:
        SubprocessResult with stdout, stderr, combined output, and elapsed time

    Raises:
        SubprocessError: If the command returns non-zero exit code
        CommandTimeoutError: If timeout is exceeded
    """
    result = await run_console(*args, log_level=log_level, timeout=timeout)
    if result.returncode != 0:
        raise SubprocessError(
            result.returncode,
            result.stdout,
            result.stderr,
            result.combined,
            list(args),
        )
    return result


async def run_captured(*args, timeout: float | None = None) -> SubprocessResult:
    """Run a command and capture output without raising on non-zero exit.

    Args:
        *args: Command and arguments
        timeout: Optional timeout in seconds (raises CommandTimeoutError on timeout)

    Returns:
        SubprocessResult with stdout, stderr, combined output, returncode, and elapsed time
    """
    return await run_console(*args, log_level=logging.NOTSET, timeout=timeout)


async def run_console(
    *args, log_level: int = logging.DEBUG, timeout: float | None = None
) -> SubprocessResult:
    """Run a command with output forwarded to logs, without raising on non-zero exit.

    Args:
        *args: Command and arguments
        log_level: Logging level for output (default DEBUG)
        timeout: Optional timeout in seconds (raises CommandTimeoutError on timeout)

    Returns:
        SubprocessResult with stdout, stderr, combined output, returncode, and elapsed time
    """
    start_time = time.perf_counter()
    log_command(*args, log_level=log_level)

    stdout_data: list[str] = []
    stderr_data: list[str] = []
    combined_data: list[str] = []

    try:
        async with asyncio.timeout(timeout):
            # Use shellous's byte-by-byte (low level) API for direct stream access
            cmd = sh(*[str(arg) for arg in args]).stdout(sh.CAPTURE).stderr(sh.CAPTURE)
            async with cmd as run:
                # Multiplex streams while maintaining interleaved order for combined output
                # We explicitly called .stdout(sh.CAPTURE) and .stderr(sh.CAPTURE),
                # so stdout and stderr should not be None
                assert run.stdout is not None and run.stderr is not None
                tasks = [
                    _read_stream(run.stdout, stdout_data, combined_data, log_level),
                    _read_stream(run.stderr, stderr_data, combined_data, log_level),
                ]
                await asyncio.gather(*tasks)
            # Use check=False to get exit code without raising on non-zero status
            # (error checking is done by try_captured/try_console)
            result = run.result(check=False)
            returncode = result.exit_code

            # Check if the command was cancelled due to timeout (shellous suppresses
            # the TimeoutError but sets cancelled=True in the Result)
            if result.cancelled:
                raise CommandTimeoutError(
                    returncode=124,
                    stdout="\n".join(stdout_data).strip(),
                    stderr="\n".join(stderr_data).strip(),
                    combined="\n".join(combined_data).strip(),
                    command=list(args),
                )
    except (asyncio.TimeoutError, TimeoutError):
        # asyncio.timeout raises TimeoutError when the deadline is reached.
        # Use return code 124 (conventional timeout code).
        raise CommandTimeoutError(
            returncode=124,
            stdout="\n".join(stdout_data).strip(),
            stderr="\n".join(stderr_data).strip(),
            combined="\n".join(combined_data).strip(),
            command=list(args),
        )

    elapsed_time = time.perf_counter() - start_time
    cmd_str = shlex.join([str(arg) for arg in args])

    # Log all command timings for profiling (skip if NOTSET = silent capture)
    if log_level != logging.NOTSET:
        logger.log(
            log_level,
            "command_completed",
            elapsed_time=round(elapsed_time, 3),
            returncode=returncode,
            command=cmd_str,
        )

    # Also log slow commands at INFO regardless of caller's log_level
    if elapsed_time > 5:
        logger.info(
            "slow_command", elapsed_time=round(elapsed_time, 3), command=cmd_str
        )

    return SubprocessResult(
        returncode,
        "\n".join(stdout_data).strip(),
        "\n".join(stderr_data).strip(),
        "\n".join(combined_data).strip(),
        elapsed_time,
    )


async def _read_stream(
    stream: asyncio.StreamReader,
    stream_buffer: list[str],
    combined_buffer: list[str],
    log_level: int,
) -> None:
    """Read lines from a stream and append to both stream-specific and combined buffers."""
    try:
        while True:
            try:
                raw = await stream.readline()
                if not raw:
                    break
            except ValueError:
                # Line exceeds asyncio's 64KB StreamReader limit (e.g. nix path-info --json).
                # Drain the rest of the oversized line in chunks until newline or EOF.
                chunks: list[bytes] = []
                while True:
                    chunk = await stream.read(65536)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    if b"\n" in chunk:
                        break
                if not chunks:
                    break
                raw = b"".join(chunks)

            decoded = raw.decode(errors="replace").strip()
            stream_buffer.append(decoded)
            combined_buffer.append(decoded)
            if log_level != logging.NOTSET:
                logger.log(log_level, "subprocess_output", line=decoded)
    except Exception:
        logger.exception("stream_read_error")


def log_command(*args, log_level: int) -> None:
    """Log a command with its arguments at the specified log level.

    Args:
        *args: Command and arguments to log
        log_level: Logging level (e.g., logging.DEBUG, logging.INFO). NOTSET (0) suppresses logging.
    """
    if log_level == logging.NOTSET:
        return
    logger.log(
        log_level,
        "running_command",
        command=shlex.join([str(arg) for arg in args]),
    )
