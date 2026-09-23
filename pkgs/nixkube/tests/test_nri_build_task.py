# SPDX-License-Identifier: MIT

"""What `_spawn_build_task` leaves behind, however it ends.

`pending_builds` is not bookkeeping. The volume sweep reads it to decide
that a directory is still being filled (issue #64), so an id left in it
pins that container's hardlink farm against both collection rules for the
life of the process.
"""

import anyio
import pytest

from src.nri.builds import BuildRegistry
from src.nri.server import NriPlugin


class _Zmq:
    def __init__(self) -> None:
        self.pending_builds = BuildRegistry()
        self.build_status: dict[str, dict[str, str]] = {}

    async def publish_build_progress(self, _container_id: str) -> None:
        pass

    async def publish_build_complete(self, _container_id: str) -> None:
        pass


def _plugin(build_and_mount) -> NriPlugin:
    """A plugin with only what `_spawn_build_task` touches.

    `__new__` and not `__init__`: the real constructor wants a CRI socket,
    a kr8s client and an NRI connection, none of which this path uses.
    The method under test is the real one.
    """
    plugin = NriPlugin.__new__(NriPlugin)
    plugin.zmq_server = _Zmq()
    plugin._build_and_mount = build_and_mount
    return plugin


CONTAINER = "ad22c728"
PATHS = {"/nix/store/abc-pkg"}


@pytest.mark.asyncio
async def test_a_cancelled_build_does_not_stay_pending():
    """A cancelled task reaches neither the success nor the except path.

    `CancelledError` is a BaseException, so `except Exception` never sees
    it. Only `finally` runs.
    """
    started = anyio.Event()

    async def hang(*_args, **_kwargs):
        started.set()
        await anyio.sleep_forever()

    plugin = _plugin(hang)
    plugin.zmq_server.pending_builds.add(CONTAINER)

    with anyio.fail_after(10):
        async with anyio.create_task_group() as tg:
            tg.start_soon(plugin._spawn_build_task, CONTAINER, "app", None, PATHS)
            await started.wait()
            tg.cancel_scope.cancel()

    assert CONTAINER not in plugin.zmq_server.pending_builds, (
        "a cancelled build left its container pinned against the volume sweep"
    )


@pytest.mark.asyncio
async def test_a_failed_build_does_not_stay_pending(monkeypatch):
    async def refuse(*_args, **_kwargs):
        raise RuntimeError("no substituter that can build it")

    reported = []

    async def report(*_args, **kwargs):
        reported.append(kwargs.get("reason"))

    monkeypatch.setattr("src.nri.server.report_event", report)

    plugin = _plugin(refuse)
    plugin.zmq_server.pending_builds.add(CONTAINER)

    await plugin._spawn_build_task(CONTAINER, "app", None, PATHS)

    assert CONTAINER not in plugin.zmq_server.pending_builds
    assert reported == ["BuildFailed"], "a failed build must say so on the pod"
