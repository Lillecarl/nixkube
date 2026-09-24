# SPDX-License-Identifier: MIT

"""Recycling a builder at `max_age`, without a cluster (#69).

A builder Job never completes on its own, so kube-prometheus raises
KubeJobNotCompleted when one outlives 12 hours. The manager replaces a builder
past `max_age`: the replacement starts first, the old one drains once the
replacement is Ready, and its Job goes only when it has nothing in flight.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import anyio
import pytest

from pynixd_nixkube import builder_manager
from pynixd_nixkube.builder_manager import (
    BuilderManager,
    PodState,
    RecycleView,
    plan_recycle,
)

SYSTEM = "x86_64-linux"
HOUR = 3600.0


def view(
    store_id: str,
    system: str = SYSTEM,
    aged: bool = False,
    ready: bool = True,
    draining: bool = False,
    busy: bool = False,
) -> RecycleView:
    return RecycleView(store_id, system, aged, ready, draining, busy)


def test_an_aged_builder_waits_for_a_ready_replacement():
    assert plan_recycle([view("old", aged=True)], min_builders=1) == ([], [])
    starting = view("new", ready=False)
    assert plan_recycle([view("old", aged=True), starting], 1) == ([], [])
    assert plan_recycle([view("old", aged=True), view("new")], 1) == (["old"], [])


def test_the_replacement_must_be_of_the_same_system():
    other = view("new", system="aarch64-linux")
    assert plan_recycle([view("old", aged=True), other], 1) == ([], [])


def test_another_aged_builder_is_not_a_replacement():
    old = [view("a", aged=True), view("b", aged=True)]
    assert plan_recycle(old, 1) == ([], [])


def test_with_no_minimum_an_aged_builder_drains_at_once():
    assert plan_recycle([view("old", aged=True)], 0) == (["old"], [])


def test_a_draining_builder_is_retired_only_when_idle():
    busy = view("old", aged=True, draining=True, busy=True)
    assert plan_recycle([busy, view("new")], 1) == ([], [])
    idle = view("old", aged=True, draining=True)
    assert plan_recycle([idle, view("new")], 1) == ([], ["old"])


def test_a_fresh_builder_is_never_touched():
    assert plan_recycle([view("new")], 1) == ([], [])


class FakeStore:
    def __init__(self, store_id: str) -> None:
        self.store_id = store_id
        self.in_flight = 0
        self.is_healthy = True
        self.draining = False


class FakeScheduler:
    def __init__(self) -> None:
        self.queue = SimpleNamespace(queue=[])
        self.triggered = 0

    def trigger(self) -> None:
        self.triggered += 1


class FakeServer:
    def __init__(self) -> None:
        self.stores: dict[str, FakeStore] = {}
        self.scheduler = FakeScheduler()

    async def remove_store(self, store_id, drain_timeout=300.0) -> None:
        self.stores.pop(store_id, None)

    async def add_store(self, store, dynamic=False) -> None:
        self.stores[str(store.store_id)] = FakeStore(str(store.store_id))


def job(name: str, age: float, now: datetime) -> SimpleNamespace:
    started = (now - timedelta(seconds=age)).isoformat()
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name, labels={"nixkube/system": SYSTEM}),
        raw={"metadata": {"name": name}, "status": {"startTime": started}},
    )


class Cluster:
    """The Jobs the API server holds, and what the manager did to them."""

    def __init__(self, monkeypatch, m: Any) -> None:
        self.now = datetime.now(UTC)
        self.jobs: dict[str, SimpleNamespace] = {}
        self.pods: dict[str, PodState] = {}
        self.created: list[str] = []
        self.deleted: list[str] = []
        self.m = m

        async def list_jobs():
            return list(self.jobs.values())

        async def create(system, overrides=None):
            name = f"job-{len(self.created) + 1}"
            self.created.append(name)
            return name

        async def get(name, namespace=None):
            return self.jobs[name]

        async def delete(j):
            self.deleted.append(j.metadata.name)

        async def pod_state(j):
            return self.pods.get(j.metadata.name, PodState())

        async def not_stale(j):
            return False

        monkeypatch.setattr(builder_manager.Job, "get", staticmethod(get))
        m._list_builder_jobs = list_jobs
        m._create_builder_job = create
        m._delete_job = delete
        m._get_job_pod_state = pod_state
        m._is_stale_builder = not_stale

    def add(self, name: str, age: float, ready: bool = True) -> None:
        self.jobs[name] = job(name, age, self.now)
        self.pods[name] = PodState(ip="10.0.0.1", ready=ready, phase="Running")

    def store(self, name: str) -> FakeStore:
        return self.m.server.stores[f"builder-{name}"]

    async def tick(self) -> None:
        """One `_periodic_reconcile` pass."""
        for j in list(self.jobs.values()):
            await self.m._reconcile_job(j)
        await self.m._ensure_min_builders()
        await self.m._recycle_aged()


def manager(**kwargs) -> Any:
    """Typed `Any`: the tests swap its API calls and server for fakes."""
    m = BuilderManager(server=FakeServer(), namespace="nixkube", **kwargs)  # type: ignore[arg-type]
    m._available_systems = {SYSTEM}
    return m


@pytest.mark.asyncio
async def test_the_last_builder_is_replaced_first_and_retired_when_idle(monkeypatch):
    m = manager(min_builders=1, max_builders=1, max_age=6 * HOUR)
    cluster = Cluster(monkeypatch, m)
    cluster.add("old", age=7 * HOUR)
    await cluster.tick()
    cluster.store("old").in_flight = 1

    # The old builder is past its age: a replacement starts, even at the
    # maximum, and the old one still takes builds while it does.
    assert cluster.created == ["job-1"]
    assert not cluster.store("old").draining

    cluster.add("job-1", age=10, ready=False)
    await cluster.tick()
    assert not cluster.store("old").draining
    assert cluster.created == ["job-1"]

    # The replacement is Ready: the old one takes no new builds, and keeps
    # the one it has.
    cluster.pods["job-1"].ready = True
    await cluster.tick()
    assert cluster.store("old").draining
    assert m.server.scheduler.triggered == 1
    assert cluster.deleted == []

    cluster.store("old").in_flight = 0
    await cluster.tick()
    assert cluster.deleted == ["old"]
    assert "builder-old" not in m.server.stores
    assert not cluster.store("job-1").draining
    assert cluster.created == ["job-1"]


@pytest.mark.asyncio
async def test_an_assigned_build_keeps_a_draining_builder(monkeypatch):
    """`in_flight` misses a build assigned but not yet sent."""
    m = manager(min_builders=1, max_age=6 * HOUR)
    cluster = Cluster(monkeypatch, m)
    cluster.add("old", age=7 * HOUR)
    cluster.add("new", age=60)
    await cluster.tick()
    assert cluster.store("old").draining

    build = SimpleNamespace(assigned_store_id="builder-old", is_done=False)
    m.server.scheduler.queue.queue.append(build)
    await cluster.tick()
    assert cluster.deleted == []

    build.is_done = True
    await cluster.tick()
    assert cluster.deleted == ["old"]


@pytest.mark.asyncio
async def test_a_queued_build_replaces_a_draining_builder_at_the_maximum(monkeypatch):
    """With no minimum, an aged builder drains at once, and only the queue
    watch starts another. Counted against the maximum, it would block that."""
    m = manager(min_builders=0, max_builders=1, max_age=6 * HOUR)
    cluster = Cluster(monkeypatch, m)
    cluster.add("old", age=7 * HOUR)
    await cluster.tick()
    assert cluster.store("old").draining

    await m._maybe_create_builder(SYSTEM)
    assert cluster.created == ["job-1"]
    # The cooldown still holds the next one back.
    await m._maybe_create_builder(SYSTEM)
    assert cluster.created == ["job-1"]


@pytest.mark.asyncio
async def test_a_young_builder_is_kept(monkeypatch):
    """The negative control: the same path, under the age."""
    m = manager(min_builders=1, max_age=6 * HOUR)
    cluster = Cluster(monkeypatch, m)
    cluster.add("young", age=5 * HOUR)
    await cluster.tick()
    await cluster.tick()
    assert cluster.created == []
    assert cluster.deleted == []
    assert not cluster.store("young").draining


@pytest.mark.asyncio
async def test_zero_means_never(monkeypatch):
    m = manager(min_builders=1, max_age=0)
    cluster = Cluster(monkeypatch, m)
    cluster.add("old", age=100 * HOUR)
    await cluster.tick()
    assert cluster.created == []
    assert not cluster.store("old").draining


@pytest.mark.asyncio
async def test_the_idle_reaper_keeps_the_replacement(monkeypatch):
    """While the old builder drains a long build, its idle replacement is the
    minimum. Counting the old one would reap the replacement, and the next
    reconcile would start another, every `idle_timeout`."""
    m = manager(min_builders=1, max_age=6 * HOUR, idle_timeout=0)
    cluster = Cluster(monkeypatch, m)
    cluster.add("old", age=7 * HOUR)
    cluster.add("new", age=60)
    await cluster.tick()
    cluster.store("old").in_flight = 1

    with anyio.move_on_after(0.5):
        await m._reap_idle()
    assert cluster.deleted == []


@pytest.mark.asyncio
async def test_the_idle_reaper_stops_at_the_minimum_in_one_pass(monkeypatch):
    """A deleted Job stays registered until the watch sees it go, so the
    count is kept by the pass itself."""
    m = manager(min_builders=1, idle_timeout=0)
    cluster = Cluster(monkeypatch, m)
    for name in ("a", "b", "c"):
        cluster.add(name, age=60)
    await cluster.tick()

    with anyio.move_on_after(0.5):
        await m._reap_idle()
    assert len(cluster.deleted) == 2
