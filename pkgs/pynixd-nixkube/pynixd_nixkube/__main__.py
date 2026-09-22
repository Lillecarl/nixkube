# SPDX-License-Identifier: MIT

from pathlib import Path

import anyio
import structlog
from pynixd.config import LocalSocketStoreSpec, PynixdSettings
from pynixd.instance import Server
from pynixd.serde import StoreId

from .setup import configure_logging, install_nss
from .ssh_keys import watch_authorized_keys

log = structlog.get_logger(__name__)


async def _main():
    configure_logging()
    log.info("pynixd_nixkube_starting")
    install_nss()

    # `spec.to_store`, not `LocalSocketStore(spec)`. See central_main.py.
    local_store = LocalSocketStoreSpec(
        store_id=StoreId("local"),
        store_path=Path("/"),
        monitor=False,
    ).to_store(str(StoreId("local")))

    settings = PynixdSettings()

    server = Server(stores={StoreId("local"): local_store}, settings=settings)

    # The watcher gets a group, rather than `server.background_tasks`: that
    # list is typed `asyncio.Task` and the server cancels it at the end of its
    # own shutdown. A group says the lifetime here, where the reader is. It is
    # inside `server`, so it closes first.
    async with server, anyio.create_task_group() as tasks:
        tasks.start_soon(watch_authorized_keys, server, name="authorized-keys")
        log.info("pynixd_nixkube_running")
        try:
            await server.wait_finished()
        finally:
            tasks.cancel_scope.cancel()


def main():
    try:
        anyio.run(_main, backend="asyncio")
    except KeyboardInterrupt:
        pass
