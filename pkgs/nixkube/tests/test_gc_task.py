# SPDX-License-Identifier: MIT
"""The GC loop, which collected nothing on any node for 33 hours.

Issue #38 counted four independent stops in one loop. Each one of them was
silent, and the first visible symptom on a Talos node is the kubelet evicting
pods for disk. These state what each fix does.

Every line of nix output quoted here was measured against nix 2.34.8, not
invented.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

import pytest
from src import gc_task
from src.gc_task import _count_deleted, _select_old_paths

# `nix store delete --store <s> --stdin` over one dead path and one live one.
# Exit code 1.
MIXED = [
    "finding garbage collector roots...",
    "deleting '/nix/store/bmwscnrzx2567yy91z3sp4nb1ixsv1vp-dead'",
    (
        "error: Cannot delete path '/nix/store/wwvlxqk32f7r78mwg7b57y3g5ql4hbsx-alive' "
        "since it is still alive. To find out why, use: nix-store --query --roots "
        "and nix-store --query --referrers"
    ),
    "1 store paths deleted, 0.0 KiB freed",
]


class TestSelectOldPaths:
    """`nix path-info --all --json` answers an object, not a list.

    Read as a list, iteration walks the keys, which are strings. The old filter
    asked `isinstance(entry, dict)` of each and got False every time, so the
    delete step received an empty list on every cycle on every node.
    """

    def test_a_path_registered_before_the_cutoff_is_selected(self):
        info = {"/nix/store/aaa-old": {"registrationTime": 100, "narSize": 1}}
        assert _select_old_paths(info, cutoff=200) == ["/nix/store/aaa-old"]

    def test_a_path_registered_after_the_cutoff_is_kept(self):
        info = {"/nix/store/bbb-new": {"registrationTime": 300, "narSize": 1}}
        assert _select_old_paths(info, cutoff=200) == []

    def test_the_key_is_the_path_and_the_value_carries_no_path(self):
        """The shape that made the old code answer nothing.

        A value with no `path` key is what Nix really sends, so a reader that
        looks for one inside the value finds nothing however it iterates.
        """
        info = {"/nix/store/ccc-old": {"registrationTime": 1, "narSize": 120}}
        assert "path" not in info["/nix/store/ccc-old"]
        assert _select_old_paths(info, cutoff=2) == ["/nix/store/ccc-old"]

    def test_a_path_with_no_registration_time_is_kept(self):
        """Absent means "do not touch", not "old"."""
        assert _select_old_paths({"/nix/store/ddd": {"narSize": 1}}, cutoff=200) == []

    def test_the_real_output_of_nix_path_info_parses(self):
        """One entry, copied from `nix path-info --all --json --json-format 1`."""
        raw = (
            '{"/nix/store/slqhbc4rf1c8j67bjzjdrh32cmwwyrhn-probe-a":'
            '{"ca":"fixed:r:sha256:0v89ynsmc3yl5a6npyinc7fvmwl5cw1dd00lygb1193q3ghd53d0",'
            '"deriver":null,"narHash":"sha256-oI3S4Bt4pBDW8xSA1gJnhfK63WE2+muNKtQPVrX1CW0=",'
            '"narSize":120,"references":[],"registrationTime":1789602613,'
            '"signatures":[],"ultimate":false}}'
        )
        selected = _select_old_paths(json.loads(raw), cutoff=1789602614)
        assert selected == ["/nix/store/slqhbc4rf1c8j67bjzjdrh32cmwwyrhn-probe-a"]


class TestCountDeleted:
    """A refused live path is the normal outcome, and not a failure.

    The loop offers every path older than GC_KEEP_SECONDS, and most of them are
    in use. Nix deletes what it can and exits 1 for the rest.
    """

    def test_a_clean_run_counts_what_it_deleted(self):
        lines = [
            "finding garbage collector roots...",
            "deleting '/nix/store/aaa-one'",
            "deleting '/nix/store/bbb-two'",
            "2 store paths deleted, 0.0 KiB freed",
        ]
        assert _count_deleted(lines) == 2

    def test_a_live_path_is_tolerated_and_not_counted(self):
        assert _count_deleted(MIXED) == 1

    def test_an_all_live_run_deletes_nothing_and_does_not_raise(self):
        lines = [
            (
                "error: Cannot delete path '/nix/store/wwv-alive' since it is still "
                "alive. To find out why, use: nix-store --query --roots"
            ),
            "0 store paths deleted, 0.0 KiB freed",
        ]
        assert _count_deleted(lines) == 0

    def test_any_other_error_still_fails_the_cycle(self):
        """Tolerating exit 1 must not tolerate a broken store.

        Without this the fix for the liveness case would swallow every failure
        the delete step can have.
        """
        lines = ["error: unrecognised flag '--skip-live'"]
        with pytest.raises(RuntimeError, match="unrecognised flag"):
            _count_deleted(lines)

    def test_a_real_error_beside_a_liveness_one_still_fails(self):
        lines = [*MIXED, "error: opening lock file: Permission denied"]
        with pytest.raises(RuntimeError, match="Permission denied"):
            _count_deleted(lines)

    def test_the_count_is_not_the_length_of_the_offered_list(self):
        """It used to be, so a cycle that deleted one of 300 reported 300."""
        assert _count_deleted(MIXED) != 2


class TestTheCommands:
    """What the loop actually runs.

    `--skip-live` is not a flag on nix 2.34.8 -- `error: unrecognised flag` --
    so the delete step failed on the flag alone and no cycle had ever finished
    on that version. Refusing a live path is already the default.
    """

    @pytest.fixture
    def recorded(self, monkeypatch) -> list[list[str]]:
        """Record each argv, and answer as a successful nix would."""
        calls: list[list[str]] = []

        class FakeCommand:
            def __init__(self, args: tuple[str, ...]):
                self.args = [str(a) for a in args]
                self._out: Path | None = None

            def stdin(self, _value):
                return self

            def stdout(self, value):
                if isinstance(value, Path):
                    self._out = value
                return self

            def stderr(self, _value):
                return self

            def set(self, **_kwargs):
                return self

            def __await__(self):
                calls.append(self.args)
                if self._out is not None:
                    if "path-info" in self.args:
                        self._out.write_text(
                            json.dumps({"/nix/store/aaa-old": {"registrationTime": 1}})
                        )
                    else:
                        self._out.write_text("deleting '/nix/store/aaa-old'\n")

                async def done():
                    return ""

                return done().__await__()

        class FakeSh:
            """`sh` is called and also read for `sh.STDOUT`, so both must work."""

            STDOUT = object()
            CAPTURE = object()

            def __call__(self, *args):
                return FakeCommand(args)

        monkeypatch.setattr(gc_task, "sh", FakeSh())
        monkeypatch.setattr(gc_task, "PYNIXD_ENABLED", False)
        return calls

    @pytest.mark.asyncio
    async def test_the_delete_does_not_pass_skip_live(self, recorded):
        await gc_task._run_gc_cycle()

        delete = next(c for c in recorded if "delete" in c)
        assert "--skip-live" not in delete
        assert "--ignore-liveness" not in delete, "that flag deletes live paths"

    @pytest.mark.asyncio
    async def test_path_info_asks_for_a_json_format(self, recorded):
        """Bare `--json` is deprecated on 2.34 and warns on every cycle."""
        await gc_task._run_gc_cycle()

        info = next(c for c in recorded if "path-info" in c)
        assert "--json-format" in info
        assert info[info.index("--json-format") + 1] == "1"

    @pytest.mark.asyncio
    async def test_a_cycle_counts_what_nix_deleted(self, recorded):
        before = gc_task.GC_PATHS_DELETED._value.get()
        await gc_task._run_gc_cycle()
        assert gc_task.GC_PATHS_DELETED._value.get() == before + 1


class TestTimeouts:
    """No nix call in this loop may run without a deadline.

    `gc_loop` awaits one cycle at a time, so one call that never returns ends
    collection on that node until the pod restarts -- and the restart re-enters
    the same call.
    """

    def test_every_step_has_a_timeout_constant(self):
        from src.constants import (
            GC_COPY_TIMEOUT_SECONDS,
            GC_DELETE_TIMEOUT_SECONDS,
            GC_PATH_INFO_TIMEOUT_SECONDS,
        )

        assert GC_COPY_TIMEOUT_SECONDS > 0
        assert GC_PATH_INFO_TIMEOUT_SECONDS > 0
        assert GC_DELETE_TIMEOUT_SECONDS > 0

    @pytest.mark.asyncio
    async def test_a_path_info_that_never_returns_ends_the_cycle(self, monkeypatch):
        """The measured failure: the call hangs and the loop waits for ever."""

        class Hang:
            def stdout(self, _value):
                return self

            def __await__(self):
                async def never():
                    await asyncio.sleep(3600)

                return never().__await__()

        monkeypatch.setattr(gc_task, "sh", lambda *_args: Hang())
        monkeypatch.setattr(gc_task, "PYNIXD_ENABLED", False)
        monkeypatch.setattr(gc_task, "GC_PATH_INFO_TIMEOUT_SECONDS", 0.05)

        with pytest.raises(TimeoutError):
            await gc_task._run_gc_cycle()


class TestFileRedirect:
    """`nix path-info --all --json` writes the whole store as one line.

    Measured: `sh(...).stdout(sh.CAPTURE)` awaited over a single 300,000
    character line did not return in 20 s, and over a short one it answers the
    empty string rather than the output. Both are silent. A file takes it.
    """

    @pytest.mark.asyncio
    async def test_a_giant_single_line_survives_a_file_redirect(self):
        from shellous import sh

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "big.txt"
            async with asyncio.timeout(20):
                await sh("python3", "-c", "print('x' * 300000)").stdout(out)
            assert out.stat().st_size == 300001

    @pytest.mark.asyncio
    async def test_the_same_line_through_a_capture_does_not_arrive(self):
        """The negative control. Without it the test above proves nothing."""
        from shellous import sh

        with pytest.raises(TimeoutError):
            async with asyncio.timeout(5):
                await sh("python3", "-c", "print('x' * 300000)").stdout(sh.CAPTURE)
