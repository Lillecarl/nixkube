# SPDX-License-Identifier: MIT

import asyncio
from pathlib import Path

import structlog
from pynixd.config import LocalSocketStoreSpec, PynixdSettings
from pynixd.instance import Server
from pynixd.serde.ids import StoreId
from pynixd.store import LocalSocketStore, Store

from .builder_manager import BuilderManager
from .config import NixkubeCentralSettings
from .setup import configure_logging, install_nss
from .ssh_keys import watch_authorized_keys

log = structlog.get_logger(__name__)


async def _main() -> None:
    configure_logging()
    log.info("pynixd_nixkube_central_starting")
    install_nss()

    local_store = LocalSocketStore(
        LocalSocketStoreSpec(
            store_id=StoreId("local"),
            store_path=Path("/data"),
            use_db=False,
            monitor=False,
        )
    )

    pynixd_settings = PynixdSettings()
    settings = NixkubeCentralSettings()

    stores: dict[StoreId, Store] = {StoreId("local"): local_store}
    if pynixd_settings.stores:
        config_stores = pynixd_settings.to_stores()
        for store_id, store in config_stores.items():
            if store_id != StoreId("local"):
                stores[store_id] = store

    server = Server(stores=stores, settings=pynixd_settings)

    builder_manager: BuilderManager | None = None

    async with server:
        keys_watch = asyncio.create_task(watch_authorized_keys(server))
        server.background_tasks.append(keys_watch)

        if settings.kube_namespace:
            builder_manager = BuilderManager(
                server=server,
                namespace=settings.kube_namespace,
                max_builders=settings.builder_max,
                min_builders=settings.builder_min,
                idle_timeout=settings.idle_timeout,
                systems=[s.strip() for s in settings.systems.split(",") if s.strip()],
            )
            await builder_manager.start()

        log.info("pynixd_nixkube_central_running")
        try:
            await server.wait_finished()
        finally:
            if builder_manager:
                await builder_manager.stop()


def main() -> None:
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
