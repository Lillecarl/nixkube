# SPDX-License-Identifier: MIT

import os
from collections.abc import Mapping, Sequence
from pathlib import Path

import anyio
import structlog

from ..errors import InitDatabaseError

logger = structlog.get_logger("nixkube.nix")


async def pipe_commands(
    producer: Sequence[str],
    consumer: Sequence[str],
    *,
    consumer_env: Mapping[str, str] | None = None,
) -> tuple[int, int]:
    """Run `producer | consumer`, and answer both return codes.

    The parent closes its copy of each descriptor as soon as the child holds
    one. A reader sees EOF only when *every* writing descriptor is shut, so a
    parent that keeps the write end until cleanup leaves the consumer reading
    for ever, and nothing times it out.

    `stderr=None` on both: the messages go where the daemon's own do. A pipe
    nobody drains fills at 64KB and stops the child there instead.
    """
    read_fd, write_fd = os.pipe()
    try:
        async with await anyio.open_process(
            list(producer), stdout=write_fd, stderr=None
        ) as upstream:
            os.close(write_fd)
            write_fd = -1
            async with await anyio.open_process(
                list(consumer),
                stdin=read_fd,
                stderr=None,
                env=None if consumer_env is None else dict(consumer_env),
            ) as downstream:
                os.close(read_fd)
                read_fd = -1
                return (await upstream.wait(), await downstream.wait())
    finally:
        for descriptor in (read_fd, write_fd):
            if descriptor != -1:
                os.close(descriptor)


async def init_database(state_dir: Path, store_paths: set[Path]) -> None:
    """
    Initialize the Nix database for a chroot store by piping dump to load.

    Equivalent to: nix-store --dump-db <paths> | NIX_STATE_DIR=<state_dir> nix-store --load-db
    """
    try:
        dump_rc, load_rc = await pipe_commands(
            [
                "nix-store",
                "--option",
                "store",
                "local",
                "--dump-db",
                *[str(path) for path in store_paths],
            ],
            ["nix-store", "--option", "store", "local", "--load-db"],
            consumer_env={
                **os.environ,
                "NIX_STATE_DIR": str(state_dir),
                "USER": "nobody",
            },
        )
        if dump_rc != 0 or load_rc != 0:
            raise RuntimeError(f"nix-store --dump-db={dump_rc} --load-db={load_rc}")
    except Exception as e:
        raise InitDatabaseError(
            "Failed to initialize Nix database",
            logs=str(e),
        ) from e

    logger.debug(
        "nix_database_initialized", state_dir=str(state_dir), count=len(store_paths)
    )
