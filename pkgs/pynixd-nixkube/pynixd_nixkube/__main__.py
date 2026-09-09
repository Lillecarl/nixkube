# SPDX-License-Identifier: MIT

import asyncio
from pathlib import Path

import structlog
from pynixd.config import LocalSocketStoreSpec, PynixdSettings
from pynixd.instance import Server
from pynixd.serde.ids import StoreId
from pynixd.store import LocalSocketStore

from .setup import configure_logging, install_nss
from .ssh_keys import watch_authorized_keys

log = structlog.get_logger(__name__)


async def _main():
    configure_logging()
    log.info("pynixd_nixkube_starting")
    install_nss()

    local_store = LocalSocketStore(
        LocalSocketStoreSpec(
            store_id=StoreId("local"),
            store_path=Path("/"),
            use_db=False,
            monitor=False,
        )
    )

    settings = PynixdSettings()

    server = Server(stores={StoreId("local"): local_store}, settings=settings)

    async with server:
        keys_watch = asyncio.create_task(watch_authorized_keys(server))
        server.background_tasks.append(keys_watch)
        log.info("pynixd_nixkube_running")
        await server.wait_finished()


def main():
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
