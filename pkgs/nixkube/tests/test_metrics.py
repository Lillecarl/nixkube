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
    """Every sample this daemon exposes, by name."""
    body = prometheus_client.generate_latest(prometheus_client.REGISTRY).decode()
    return {
        line.split(" ")[0]: float(line.split(" ")[1])
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
