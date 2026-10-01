# SPDX-License-Identifier: MIT

from src.cli import store_loops


def test_a_separate_store_runs_its_daemon_and_its_sweep():
    assert store_loops(host_store=False) == {"nix-daemon", "gc"}


def test_a_host_store_leaves_both_to_the_host():
    assert store_loops(host_store=True) == frozenset()
