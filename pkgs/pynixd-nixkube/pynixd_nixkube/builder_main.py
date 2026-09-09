# SPDX-License-Identifier: MIT

import asyncio
from pathlib import Path

import structlog
from pynixd.config import LocalSocketStoreSpec, PynixdSettings
from pynixd.instance import Server
from pynixd.serde.ids import StoreId
from pynixd.store import LocalSocketStore

from .setup import configure_logging, install_nss

log = structlog.get_logger(__name__)


async def _main() -> None:
    configure_logging()
    log.info("pynixd_nixkube_builder_starting")
    install_nss()

    local_store = LocalSocketStore(
        LocalSocketStoreSpec(
            store_id=StoreId("local"),
            store_path=Path("/"),
            use_db=False,
            monitor=False,
            extra_args=["--option", "build-dir", "/nix/var/nix/builds"],
        )
    )

    settings = PynixdSettings()

    server = Server(stores={StoreId("local"): local_store}, settings=settings)

    async with server:
        log.info("pynixd_nixkube_builder_running")
        await server.wait_finished()


def main() -> None:
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
