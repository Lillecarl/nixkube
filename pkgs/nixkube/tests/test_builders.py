# SPDX-License-Identifier: MIT

"""Which builder pods this node is willing to send work to.

One URI is enough for `build_builder_args` to add `--max-jobs 0`, which
turns off local building. So a builder that is listed but not listening
does not slow the node down -- it stops it building at all, and the error
blames the substituters.
"""

import pytest

from src import builders
from src.builders import build_builder_args, get_builder_uris, is_usable


class _Pod:
    """Enough of a kr8s Pod for the discovery path."""

    def __init__(self, name: str, phase: str, ready: str | None) -> None:
        conditions = [] if ready is None else [{"type": "Ready", "status": ready}]
        self.raw = {
            "metadata": {"name": name},
            "status": {"phase": phase, "conditions": conditions},
        }

    def __getitem__(self, key):
        return self.raw[key]


def _listing(monkeypatch, pods) -> None:
    async def get(*_args, **_kwargs):
        for pod in pods:
            yield pod

    monkeypatch.setattr(builders.kr8s.asyncio, "get", get)
    monkeypatch.setattr(builders, "BUILDERS_ENABLED", True)


class TestIsUsable:
    def test_a_ready_running_builder_is_usable(self):
        assert is_usable(_Pod("b1", "Running", "True"))

    def test_a_running_builder_that_is_not_ready_is_not(self):
        """The window this exists for: containers up, sshd not answering."""
        assert not is_usable(_Pod("b1", "Running", "False"))

    def test_a_builder_with_no_conditions_yet_is_not(self):
        assert not is_usable(_Pod("b1", "Running", None))

    def test_a_pending_builder_is_not(self):
        assert not is_usable(_Pod("b1", "Pending", "True"))


class TestDiscovery:
    @pytest.mark.asyncio
    async def test_only_ready_builders_are_offered(self, monkeypatch):
        _listing(
            monkeypatch,
            [_Pod("ready", "Running", "True"), _Pod("starting", "Running", "False")],
        )

        uris = await get_builder_uris()

        assert len(uris) == 1
        assert "ready" in uris[0]
        assert "starting" not in uris[0]

    @pytest.mark.asyncio
    async def test_no_ready_builder_means_the_node_builds_for_itself(self, monkeypatch):
        """The failure this guards. `--max-jobs 0` with nothing listening
        fails every build on the node, and the message names substituters
        rather than the builder that is not up."""
        _listing(monkeypatch, [_Pod("starting", "Running", "False")])

        uris = await get_builder_uris()

        assert uris == []
        assert build_builder_args(uris) == [], "a node with no builder must build"

    @pytest.mark.asyncio
    async def test_a_ready_builder_does_turn_off_local_building(self, monkeypatch):
        """The other half: when a builder is up, the delegation is the point."""
        _listing(monkeypatch, [_Pod("ready", "Running", "True")])

        args = build_builder_args(await get_builder_uris())

        assert args[:2] == ["--max-jobs", "0"]
        assert "--builders-use-substitutes" in args
