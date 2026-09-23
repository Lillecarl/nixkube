# SPDX-License-Identifier: MIT

"""The CRI query the volume sweep waits on.

Issue #38 gave every nix call in the GC loop a deadline, for one reason:
the loop awaits one step at a time, so a call that never returns ends
collection on that node until the pod restarts. `list_container_ids` is
the same shape on a socket those timeouts never covered, and the NRI
plugin also asks it once before it registers.
"""

from contextlib import asynccontextmanager
from pathlib import Path

import anyio
import pytest

from src import cri

SOCKET = Path("/run/containerd/containerd.sock")


@asynccontextmanager
async def _channel(_socket):
    yield object()


class _Containers:
    def __init__(self, ids):
        self.containers = [type("C", (), {"id": i})() for i in ids]


def _stub_with(answer):
    class Stub:
        def __init__(self, _channel):
            pass

        async def ListContainers(self, _request):
            return await answer()

    return Stub


@pytest.mark.asyncio
async def test_it_returns_the_runtimes_container_ids(monkeypatch):
    async def answer():
        return _Containers(["aaa", "bbb"])

    monkeypatch.setattr(cri, "cri_channel", _channel)
    monkeypatch.setattr(cri.cri_grpc, "RuntimeServiceStub", _stub_with(answer))

    assert await cri.list_container_ids(SOCKET) == {"aaa", "bbb"}


@pytest.mark.asyncio
async def test_a_runtime_that_never_answers_raises_rather_than_hangs(monkeypatch):
    """Without the deadline this call never returns, and the sweep never ends.

    The sweep is detached, so nothing above it notices: volume collection
    on that node simply stops, which is #38 with a different socket.
    """

    async def never():
        await anyio.sleep_forever()

    monkeypatch.setattr(cri, "cri_channel", _channel)
    monkeypatch.setattr(cri.cri_grpc, "RuntimeServiceStub", _stub_with(never))
    monkeypatch.setattr(cri, "CRI_LIST_TIMEOUT_SECONDS", 0.05)

    with anyio.fail_after(5):
        with pytest.raises(RuntimeError, match="did not answer ListContainers"):
            await cri.list_container_ids(SOCKET)


@pytest.mark.asyncio
async def test_a_socket_error_still_names_the_socket(monkeypatch):
    async def refuse():
        raise ConnectionRefusedError("no such socket")

    monkeypatch.setattr(cri, "cri_channel", _channel)
    monkeypatch.setattr(cri.cri_grpc, "RuntimeServiceStub", _stub_with(refuse))

    with pytest.raises(RuntimeError, match=str(SOCKET)):
        await cri.list_container_ids(SOCKET)
