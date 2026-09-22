# SPDX-License-Identifier: MIT

"""What the volume sweep removes, and what it must leave alone.

Issue #64: a build task fills a volume for minutes, yielding to the loop
every 256 directory entries, so this sweep can run in the middle of one.
Removing the directory then deletes the destination mid-link, and the
`FileNotFoundError` that follows names the *source* store path -- so the
failure reads as a corrupt store rather than as a race.
"""

from pathlib import Path

import anyio
import pytest

from src.errors import HardlinkClosureError
from src.hardlinks import _YIELD_EVERY, hardlink_closure
from src.nri import cleanup


@pytest.fixture
def volumes(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "containers"
    root.mkdir()
    monkeypatch.setattr(cleanup, "NRI_CONTAINERS", root)
    return root


def _volume(root: Path, name: str) -> Path:
    made = root / name
    (made / "nix" / "store").mkdir(parents=True)
    return made


def _flatten(error: BaseException) -> list[BaseException]:
    """Every leaf of a possibly nested ExceptionGroup."""
    if isinstance(error, BaseExceptionGroup):
        return [leaf for child in error.exceptions for leaf in _flatten(child)]
    return [error]


def _cri(monkeypatch, active: set[str]) -> None:
    async def listing(_socket):
        return active

    monkeypatch.setattr(cleanup, "list_container_ids", listing)


class TestRemovedId:
    @pytest.mark.asyncio
    async def test_it_removes_the_container_it_was_told_about(
        self, volumes, monkeypatch
    ):
        gone = _volume(volumes, "gone")
        _cri(monkeypatch, {"gone"})  # still listed: the removed_id rule is why

        await cleanup.garbage_collect_stale_volumes(Path("/cri.sock"), "gone")

        assert not gone.exists()

    @pytest.mark.asyncio
    async def test_it_leaves_one_whose_build_is_still_running(
        self, volumes, monkeypatch
    ):
        building = _volume(volumes, "busy")
        _cri(monkeypatch, {"busy"})

        await cleanup.garbage_collect_stale_volumes(Path("/cri.sock"), "busy", {"busy"})

        assert building.exists(), "deleted a volume a build task is writing into"


class TestItNeverTakesThePluginDown:
    """The sweep runs detached, so anything it raises reaches supervision.

    Every one of these used to be a plausible way to lose the NRI plugin,
    and a plugin that is down leaves every container created after it with
    no /nix.
    """

    @pytest.mark.asyncio
    async def test_a_removal_that_cannot_be_deleted_is_logged_not_raised(
        self, volumes, monkeypatch
    ):
        _volume(volumes, "stuck")
        _cri(monkeypatch, set())
        monkeypatch.setattr(
            cleanup.shutil,
            "rmtree",
            lambda *_a, **_k: (_ for _ in ()).throw(PermissionError("read-only")),
        )

        await cleanup.garbage_collect_stale_volumes(Path("/cri.sock"), "stuck")

    @pytest.mark.asyncio
    async def test_a_cri_that_will_not_answer_does_not_stop_the_removal(
        self, volumes, monkeypatch
    ):
        """`list_container_ids` raises RuntimeError on any socket trouble.

        The named removal happens before that call and must still stand: a
        node whose CRI socket is briefly unavailable would otherwise keep
        every farm it was told to drop.
        """
        gone = _volume(volumes, "gone")

        async def refuse(_socket):
            raise RuntimeError("Failed to list containers from CRI socket")

        monkeypatch.setattr(cleanup, "list_container_ids", refuse)

        await cleanup.garbage_collect_stale_volumes(Path("/cri.sock"), "gone")

        assert not gone.exists()

    @pytest.mark.asyncio
    async def test_a_farm_directory_that_does_not_exist_yet(
        self, tmp_path, monkeypatch
    ):
        """Before the first container, NRI_CONTAINERS is not there."""
        monkeypatch.setattr(cleanup, "NRI_CONTAINERS", tmp_path / "never-made")
        _cri(monkeypatch, set())

        await cleanup.garbage_collect_stale_volumes(Path("/cri.sock"), "absent")

    @pytest.mark.asyncio
    async def test_a_plain_file_in_the_farm_is_left_alone(self, volumes, monkeypatch):
        stray = volumes / "not-a-container"
        stray.write_text("")
        _cri(monkeypatch, set())

        await cleanup.garbage_collect_stale_volumes(Path("/cri.sock"))

        assert stray.exists(), "removed something that is not a volume"


class TestCriBackstop:
    @pytest.mark.asyncio
    async def test_it_removes_a_volume_the_cri_no_longer_lists(
        self, volumes, monkeypatch
    ):
        stale = _volume(volumes, "stale")
        _cri(monkeypatch, {"alive"})

        await cleanup.garbage_collect_stale_volumes(Path("/cri.sock"))

        assert not stale.exists()

    @pytest.mark.asyncio
    async def test_it_leaves_a_volume_that_is_still_being_built(
        self, volumes, monkeypatch
    ):
        """The dangerous window: a container mid-create is not yet in the CRI.

        Its volume root exists and a build is filling it, so the backstop
        sees an id the CRI does not list -- and without the guard, another
        container's removal would collect it.
        """
        fresh = _volume(volumes, "creating")
        _cri(monkeypatch, {"someone-else"})

        await cleanup.garbage_collect_stale_volumes(
            Path("/cri.sock"), "someone-else", {"creating"}
        )

        assert fresh.exists(), "another container's removal ate a live volume"

    @pytest.mark.asyncio
    async def test_a_live_build_is_read_now_and_not_when_it_was_scheduled(
        self, volumes, monkeypatch
    ):
        """`schedule_garbage_collection` passes the set, not a copy of it."""
        later = _volume(volumes, "later")
        _cri(monkeypatch, set())
        pending: set[str] = set()

        async def listing(_socket):
            # The build starts after the sweep was queued but before it looks.
            pending.add("later")
            return set()

        monkeypatch.setattr(cleanup, "list_container_ids", listing)

        await cleanup.garbage_collect_stale_volumes(Path("/cri.sock"), None, pending)

        assert later.exists()


class TestTheRaceItself:
    """A sweep running while a build walks, which is the actual #64.

    The tests above check what the sweep *decides*. These run the two
    against each other and check what survives, because a guard tested
    only through its own rule is a guard tested against my reading of it.

    The interleaving is deterministic, not lucky. `hardlink_tree`
    checkpoints at entry index 0, so the build yields before it links
    anything; the sweep's `removed_id` branch then rmtrees before its own
    first await. The build resumes into a destination that is gone.
    """

    CONTAINER = "ad22c728"

    def _closure(self, tmp_path: Path) -> Path:
        """A store path with enough entries to span several checkpoints."""
        src = tmp_path / "store" / "abc-pkg"
        src.mkdir(parents=True)
        for i in range(_YIELD_EVERY * 3):
            (src / f"f{i:05d}").write_text("x")
        return src

    async def _build_against_sweep(
        self, volumes: Path, src: Path, building: set[str]
    ) -> None:
        volume_root = volumes / self.CONTAINER
        async with anyio.create_task_group() as tg:
            tg.start_soon(hardlink_closure, {src}, volume_root / "nix" / "store")
            tg.start_soon(
                cleanup.garbage_collect_stale_volumes,
                Path("/cri.sock"),
                self.CONTAINER,
                building,
            )

    @pytest.mark.asyncio
    async def test_a_guarded_build_survives_a_sweep_of_its_own_container(
        self, volumes, tmp_path, monkeypatch
    ):
        src = self._closure(tmp_path)
        _cri(monkeypatch, set())

        await self._build_against_sweep(volumes, src, {self.CONTAINER})

        linked = volumes / self.CONTAINER / "nix" / "store" / src.name
        assert sorted(p.name for p in linked.iterdir()) == sorted(
            p.name for p in src.iterdir()
        ), "the sweep took files out from under the build"

    @pytest.mark.asyncio
    async def test_without_the_guard_the_same_sweep_breaks_the_build(
        self, volumes, tmp_path, monkeypatch
    ):
        """The negative control: this is the failure seen on nixlab2.

        Without it, the test above passes whether or not the guard works --
        it would only be showing that nothing raced.
        """
        src = self._closure(tmp_path)
        _cri(monkeypatch, set())

        # A task group reports a child's failure as an ExceptionGroup, so
        # `pytest.raises(HardlinkClosureError)` does not match it.
        with pytest.raises(BaseExceptionGroup) as caught:
            await self._build_against_sweep(volumes, src, set())

        failures = [
            error
            for error in _flatten(caught.value)
            if isinstance(error, HardlinkClosureError)
        ]
        assert failures, f"expected a hardlink failure, got {caught.value!r}"
        logs = failures[0].logs
        assert f"source {src}" in logs and "exists=True" in logs, (
            "the store path is intact; the message must not blame it"
        )
        assert "exists=False" in logs, "the target side is the one that went"
