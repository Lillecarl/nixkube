# SPDX-License-Identifier: MIT

"""Supervised nix-daemon subprocess with structured log forwarding.

Runs `nix daemon --store local --log-format internal-json` as a child process,
forwards its structured JSON log lines through structlog, and restarts on exit
with crash-loop detection via CrashLoopTracker.
"""

import asyncio
import json
import re

import structlog
from shellous import sh

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
        cmd = (
            sh(
                "nix",
                "daemon",
                "--store",
                "local",
                "--log-format",
                "internal-json",
                "--debug",
            )
            .stdout(sh.CAPTURE)
            .stderr(sh.CAPTURE)
        )
        async with cmd as run:
            assert run.stdout is not None and run.stderr is not None
            await asyncio.gather(
                _pipe_nix_logs(run.stdout),
                _pipe_nix_logs(run.stderr),
            )
        result = run.result(check=False)
        rc = result.exit_code

        logger.warning("nix_daemon_exited", returncode=rc)
        tracker.record_and_check()
        logger.info("nix_daemon_restarting", backoff_seconds=1)
        await asyncio.sleep(1)


async def _pipe_nix_logs(stream: asyncio.StreamReader) -> None:
    """Forward nix daemon log lines through structlog.

    Lines prefixed with `@nix ` carry internal-json structured data.
    Other lines are logged as debug.
    """
    try:
        while True:
            try:
                raw = await stream.readline()
                if not raw:
                    break
            except ValueError:
                # Line exceeds asyncio's 64KB StreamReader limit.
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

            line = raw.decode(errors="replace").rstrip()
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
