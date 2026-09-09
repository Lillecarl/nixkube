# SPDX-License-Identifier: MIT

import asyncio
import json
import logging
import sys
from pathlib import Path

import anyio
import structlog

from .constants import (
    BUILDERS_ENABLED,
    ENABLE_COMPAT_DRIVER,
    HOST_MOUNT_PATH,
    KUBE_NODE_NAME,
    NAMESPACE,
    NIX_BUILD_TIMEOUT,
    NRI_ENABLED,
    NRI_PLUGIN_IDX,
    NRI_PLUGIN_NAME,
    PYNIXD_ENABLED,
    VERIFY_STORE_PATHS,
)
from .csi.server import csi_serve
from .gc_task import gc_loop
from .nix_daemon import supervise_nix_daemon
from .nri.server import nri_serve
from .startup import run_setup
from .supervision import supervised


def configure_structlog(renderer: str = "json") -> None:
    """Configure structlog with stdlib integration and chosen renderer.

    Sets up a shared processor chain for both structlog and stdlib loggers
    (third-party libraries like kr8s, grpclib). The ProcessorFormatter routes
    all log records through structlog processors so output format is uniform
    regardless of source.

    Args:
        renderer: "json" (default, for production/Kubernetes) or "console" (for dev)
    """
    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.StackInfoRenderer(),
    ]

    if renderer == "console":
        # Auto-detect colors: use ANSI only when stdout is a real terminal.
        # In a Kubernetes container stdout is a pipe (isatty=False), so colors are
        # disabled and the output stays plain text — safe for log aggregators and
        # copy-paste. Colors are enabled automatically when running in a local terminal.
        final_renderer: structlog.types.Processor = structlog.dev.ConsoleRenderer(
            colors=sys.stdout.isatty()
        )
    elif renderer == "logfmt":
        # Logfmt: key=value pairs on a single line, human-readable and machine-parseable.
        final_renderer = structlog.processors.LogfmtRenderer()
    else:
        # JSON (default). No asctime — Kubernetes captures stdout with its own timestamps.
        # default=str converts non-serializable types (e.g. Path values) to strings.
        final_renderer = structlog.processors.JSONRenderer(default=str)

    structlog.configure(
        processors=shared_processors
        + [
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    # ConsoleRenderer formats exceptions natively; ExceptionRenderer is only
    # needed for machine-readable formats (json/logfmt) to serialize exc_info.
    pre_render: list[structlog.types.Processor] = (
        [] if renderer == "console" else [structlog.processors.ExceptionRenderer()]
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            *pre_render,
            final_renderer,
        ],
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(handler)


def log_effective_log_config(renderer: str) -> None:
    """Print the effective logging configuration for debugging.

    Uses print() instead of logger to ensure output is always visible
    regardless of configured log levels.
    """
    root = logging.getLogger()
    lines = [
        "Effective logging configuration:",
        f"  structlog renderer: {renderer}",
        f"  Root logger: level={logging.getLevelName(root.level)}",
    ]
    for name in sorted(logging.Logger.manager.loggerDict.keys()):
        log = logging.getLogger(name)
        if log.level != logging.NOTSET:
            lines.append(f"  Logger '{name}': level={logging.getLevelName(log.level)}")
    print("\n".join(lines))


def log_effective_app_config() -> None:
    """Print the effective application configuration for debugging.

    Uses print() instead of logger to ensure output is always visible
    regardless of configured log levels.
    """
    print(
        "Effective application configuration:\n"
        f"  {NIX_BUILD_TIMEOUT=}\n"
        f"  {PYNIXD_ENABLED=}\n"
        f"  {BUILDERS_ENABLED=}\n"
        f"  {VERIFY_STORE_PATHS=}\n"
        f"  {HOST_MOUNT_PATH=}\n"
        f"  {NRI_PLUGIN_NAME=}\n"
        f"  {NRI_PLUGIN_IDX=}\n"
        f"  {NAMESPACE=}\n"
        f"  {KUBE_NODE_NAME=}"
    )


async def async_main():
    """Run one-shot setup, then supervise nix-daemon, GC, CSI, and NRI concurrently.

    Loads logging configuration from /etc/nix/logging.json (ConfigMap-mounted).
    Runs run_setup() synchronously first, then spawns supervised tasks for
    nix-daemon, GC loop, CSI server(s), and optionally the NRI plugin.
    CrashLoopError from any task propagates through asyncio.gather(), cancels
    siblings, and exits the process (Kubernetes restarts the pod with backoff).
    """
    config: dict = {}
    # `anyio.Path`, not `pathlib.Path`: `open()` in an async function blocks
    # the event loop. It costs nothing here, since nothing else is running
    # yet, but the rule is worth keeping whole -- this is the only place in
    # the service that reads a file from async code.
    logging_config_path = anyio.Path("/etc/nix/logging.json")
    config_present = await logging_config_path.exists()

    if config_present:
        config = json.loads(await logging_config_path.read_text())

    renderer = config.pop("renderer", "json")
    configure_structlog(renderer)

    # Apply per-logger levels from config
    for name, settings in config.get("loggers", {}).items():
        if isinstance(settings, dict) and "level" in settings:
            logging.getLogger(name).setLevel(settings["level"])

    root_level = config.get("root", {}).get("level", "WARNING")
    logging.getLogger().setLevel(root_level)

    logger = structlog.get_logger("nixkube")
    if config_present:
        logger.info("logging_config_loaded", path=str(logging_config_path))
    else:
        logger.info("logging_config_fallback", path=str(logging_config_path))

    log_effective_log_config(renderer)
    log_effective_app_config()

    logger.info("nri_plugin", enabled=NRI_ENABLED)
    logger.info(
        "csi_drivers",
        drivers=["nixkube"] + (["nix.csi.store"] if ENABLE_COMPAT_DRIVER else []),
    )

    await run_setup()

    tasks: list[asyncio.Task] = [
        asyncio.create_task(supervise_nix_daemon(), name="nix-daemon"),
        asyncio.create_task(gc_loop(), name="gc"),
        asyncio.create_task(
            supervised(
                lambda: csi_serve(
                    plugin_name="nixkube", socket_path=Path("/csi/nixkube/csi.sock")
                ),
                "csi",
            ),
            name="csi",
        ),
    ]
    if ENABLE_COMPAT_DRIVER:
        tasks.append(
            asyncio.create_task(
                supervised(
                    lambda: csi_serve(
                        plugin_name="nix.csi.store",
                        socket_path=Path("/csi/nix.csi.store/csi.sock"),
                    ),
                    "csi-compat",
                ),
                name="csi-compat",
            )
        )
    if NRI_ENABLED:
        tasks.append(
            asyncio.create_task(
                supervised(lambda: nri_serve(), "nri"),
                name="nri",
            )
        )
    await asyncio.gather(*tasks)


def main():
    """Entry point for the nixkube daemon."""
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
