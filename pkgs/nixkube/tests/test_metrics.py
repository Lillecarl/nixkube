# SPDX-License-Identifier: MIT
"""The metrics endpoint answers, and says something about this node.

A DaemonSet that cannot be scraped is one nobody can see. These tests state
that the series exist and hold real numbers: a registry that answered only the
collectors `prometheus_client` installs for the process would pass a check
that reads the status alone.
"""

from __future__ import annotations

from pathlib import Path

import prometheus_client
from src import metrics


def _series() -> dict[str, float]:
    """Every sample this daemon exposes, by name.

    `rsplit` and not `split`: a label value may hold a space, and `command`
    does -- `nixkube_subprocess_calls_total{command="nix build",...}`. The
    value is always the last field of the line.
    """
    body = prometheus_client.generate_latest(prometheus_client.REGISTRY).decode()
    return {
        line.rsplit(" ", 1)[0]: float(line.rsplit(" ", 1)[1])
        for line in body.splitlines()
        if line and not line.startswith("#")
    }


class TestStoreSpace:
    """`StoreSpaceCollector`, which answers the size of this node's /nix.

    It reads the file system and not the store's own record of itself. The
    docstring there holds the two measurements that decided it: the record is
    58,000 times slower to read and answers a larger number than the disk
    holds.
    """

    def test_both_numbers_are_served(self):
        series = _series()
        assert "nixkube_store_filesystem_size_bytes" in series
        assert "nixkube_store_filesystem_available_bytes" in series

    def test_the_numbers_describe_a_real_file_system(self):
        series = _series()
        total = series["nixkube_store_filesystem_size_bytes"]
        available = series["nixkube_store_filesystem_available_bytes"]

        assert total > 0
        assert 0 <= available <= total

    def test_an_unreadable_store_serves_no_series(self, monkeypatch):
        """A store that is gone must not take the rest of the scrape with it.

        Every other series is still an answer, so these two go absent.
        """
        monkeypatch.setattr(metrics, "NIX_ROOT", Path("/nonexistent-root"))

        assert list(metrics.StoreSpaceCollector().collect()) == []


class TestCounters:
    def test_the_gc_counters_are_registered(self):
        """`gc_loop` counts a failed cycle as well as a finished one.

        The loop logs an error and sleeps, so a cycle that fails every time
        is otherwise silent while the store grows.
        """
        metrics.GC_CYCLES.labels(result="ok").inc()
        metrics.GC_CYCLES.labels(result="error").inc()
        metrics.GC_PATHS_DELETED.inc(3)

        series = _series()
        assert series['nixkube_gc_cycles_total{result="ok"}'] >= 1
        assert series['nixkube_gc_cycles_total{result="error"}'] >= 1
        assert series["nixkube_gc_paths_deleted_total"] >= 3

    def test_the_volume_counters_are_registered(self):
        metrics.VOLUME_MOUNTS.labels(result="ok").inc()
        metrics.VOLUME_UNMOUNTS.labels(result="ok").inc()

        series = _series()
        assert series['nixkube_volume_mounts_total{result="ok"}'] >= 1
        assert series['nixkube_volume_unmounts_total{result="ok"}'] >= 1


class TestCommandLabel:
    """`command` must be bounded, and argv is not.

    Every subprocess passes through `run_console`, and the label comes from
    its arguments. A rule of "the leading tokens that are not flags" reads a
    store path, a flake reference or a temporary file name as a label, and
    that is one series per value.
    """

    def test_a_store_path_argument_does_not_reach_the_label(self):
        assert metrics.command_label(("nix", "build", "/nix/store/aaa-hello")) == (
            "nix build"
        )

    def test_a_flake_reference_does_not_reach_the_label(self):
        """The case that looks safe: a bare word after the subcommand."""
        assert metrics.command_label(("nix", "build", "nixpkgs#hello")) == "nix build"

    def test_a_store_subcommand_is_kept(self):
        """`nix store ping` and `nix store sign` are different work and both
        are in the allow-list, so the label keeps the third token."""
        assert (
            metrics.command_label(("nix", "store", "ping", "--json"))
            == "nix store ping"
        )

    def test_an_unknown_subcommand_falls_back_to_the_binary(self):
        assert metrics.command_label(("nix", "frobnicate", "x")) == "nix"

    def test_a_binary_is_named_without_its_store_path(self):
        assert (
            metrics.command_label(("/nix/store/abc-nix/bin/nix", "copy", "--to", "x"))
            == "nix copy"
        )

    def test_no_arguments_answers_a_name_and_not_an_error(self):
        assert metrics.command_label(()) == "unknown"


class TestTheNewCounters:
    """Declared and incremented. A metric nothing moves exports a flat zero,
    which reads on a dashboard exactly like a quiet node."""

    def test_the_subprocess_counters_are_registered(self):
        metrics.SUBPROCESS_CALLS.labels(command="nix build", result="ok").inc()
        metrics.SUBPROCESS_DURATION.labels(command="nix build").observe(1.0)

        series = _series()
        assert (
            series['nixkube_subprocess_calls_total{command="nix build",result="ok"}']
            >= 1
        )
        assert (
            series['nixkube_subprocess_duration_seconds_count{command="nix build"}']
            >= 1
        )

    def test_the_build_counters_are_registered(self):
        metrics.NIX_BUILDS.labels(kind="store_path", result="ok").inc()

        series = _series()
        assert series['nixkube_nix_builds_total{kind="store_path",result="ok"}'] >= 1

    def test_the_hardlink_counters_are_registered(self):
        metrics.HARDLINK_CLOSURES.labels(result="ok").inc()
        metrics.HARDLINK_PATHS.inc(2)

        series = _series()
        assert series['nixkube_hardlink_closures_total{result="ok"}'] >= 1
        assert series["nixkube_hardlink_paths_total"] >= 2

    def test_the_supervision_counter_is_registered(self):
        """A restart count was the diagnosis twice on the nixlab2 cluster, and
        both times it came from `kubectl` and not from a series."""
        metrics.SERVICE_RESTARTS.labels(service="csi").inc()

        assert _series()['nixkube_service_restarts_total{service="csi"}'] >= 1

    def test_the_cache_gauge_is_registered(self):
        metrics.CACHE_REACHABLE.set(1)

        assert _series()["nixkube_cache_reachable"] == 1

    def test_configured_is_reported_beside_reachable(self):
        """`reachable == 0` is the correct state on a cluster with no pynixd,
        so an alert on it alone fires everywhere. The pair is what says
        whether zero is a fault."""
        assert "nixkube_cache_configured" in _series()

    def test_the_loop_lag_gauges_are_registered(self):
        """nixkube blocks its own loop, and a stalled loop looks exactly like
        a slow build from outside."""
        series = _series()
        assert "nixkube_event_loop_lag_seconds" in series
        assert "nixkube_event_loop_lag_max_seconds" in series


class TestTheLoopLagMonitor:
    def test_the_window_forgets_an_old_stall(self, monkeypatch):
        """`lifetime_max` keeps a stall for the operator; `window_max` decays,
        or one bad closure holds the node's lag high forever.

        Time is moved, not waited for, so this asserts the decay rule rather
        than this machine's timing.
        """
        now = [1000.0]
        monkeypatch.setattr(metrics.time, "monotonic", lambda: now[0])
        monitor = metrics.LoopLagMonitor(window=10.0)

        monitor._samples.append((now[0], 0.4))
        monitor.lifetime_max = 0.4
        assert monitor.window_max == 0.4

        now[0] += 9.0
        assert monitor.window_max == 0.4, "pruned a sample still inside the window"

        now[0] += 2.0
        assert monitor.window_max == 0.0, "kept a stall past the end of the window"
        assert monitor.lifetime_max == 0.4, "lifetime_max must not decay"


class TestServe:
    def test_a_port_it_cannot_bind_does_not_end_the_daemon(self, monkeypatch):
        """A node that cannot serve metrics still has a CSI driver to run.

        Refusing to start one because the other is unavailable turns an
        observability gap into an outage.
        """

        def refuse(*_args, **_kwargs):
            raise OSError("address already in use")

        monkeypatch.setattr(metrics, "start_http_server", refuse)

        metrics.serve(port=9099, addr="127.0.0.1")
