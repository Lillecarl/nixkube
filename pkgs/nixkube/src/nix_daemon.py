# SPDX-License-Identifier: MIT

"""Supervised nix-daemon subprocess with structured log forwarding.

Runs `nix daemon --store local --log-format internal-json` as a child process,
forwards its structured JSON log lines through structlog, and restarts on exit
with crash-loop detection via CrashLoopTracker.
"""

import json
import re
import subprocess

import anyio
import structlog
from anyio.abc import ByteReceiveStream

from .subprocessing import iter_lines
from .supervision import CrashLoopTracker

logger = structlog.get_logger("nixkube.nix_daemon")

# ANSI escape sequence pattern for stripping terminal colour codes
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


# Nix verbosity: 0=error, 1=warn, 2=notice, 3=info, 4=talkative, 5=chatty, 6=debug, 7=vomit
def _nix_level_to_structlog(verbosity: int) -> str:
    if verbosity <= 0:
        return "error"
    if verbosity == 1:
        return "warning"
    if verbosity <= 3:
        return "info"
    return "debug"


async def supervise_nix_daemon() -> None:
    """Supervised loop for the nix daemon subprocess.

    Launches nix daemon, forwards its logs, and restarts on exit.
    Raises CrashLoopError after too many rapid restarts.
    """
    tracker = CrashLoopTracker(max_restarts=5, window=60.0, name="nix-daemon")

    while True:
        logger.info("nix_daemon_starting")
        async with await anyio.open_process(
            [
                "nix",
                "daemon",
                "--store",
                "local",
                "--log-format",
                "internal-json",
                "--debug",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ) as proc:
            assert proc.stdout is not None and proc.stderr is not None
            async with anyio.create_task_group() as tg:
                tg.start_soon(_pipe_nix_logs, proc.stdout)
                tg.start_soon(_pipe_nix_logs, proc.stderr)
            rc = await proc.wait()

        logger.warning("nix_daemon_exited", returncode=rc)
        tracker.record_and_check()
        logger.info("nix_daemon_restarting", backoff_seconds=1)
        await anyio.sleep(1)


async def _pipe_nix_logs(stream: ByteReceiveStream) -> None:
    """Forward nix daemon log lines through structlog.

    Lines prefixed with `@nix ` carry internal-json structured data.
    Other lines are logged as debug.
    """
    try:
        async for raw_line in iter_lines(stream):
            line = raw_line.rstrip()
            if line.startswith("@nix "):
                payload = line.removeprefix("@nix ")
                try:
                    data = json.loads(payload)
                except json.JSONDecodeError:
                    logger.debug("nix_daemon_log", line=line)
                    continue

                action = data.get("action", "")
                if action == "msg":
                    verbosity = data.get("verbosity", 2)
                    level = _nix_level_to_structlog(verbosity)
                    msg = _ANSI_RE.sub("", data.get("msg", "")).strip()
                    getattr(logger, level)("nix_daemon", msg=msg)
                else:
                    # start / stop / result activity traces
                    logger.debug(
                        "nix_daemon_activity",
                        **{k: v for k, v in data.items() if k != "action"},
                        action=action,
                    )
            else:
                logger.debug("nix_daemon_log", line=line)
    except Exception:
        logger.exception("nix_daemon_log_error")
