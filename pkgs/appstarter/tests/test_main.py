# SPDX-License-Identifier: MIT

from pathlib import Path

import pytest
from appstarter import __main__


@pytest.fixture(autouse=True)
def no_store_override(monkeypatch):
    monkeypatch.delenv("APPSTARTER_STORE", raising=False)


def _parse(*argv):
    return __main__._parser().parse_args(list(argv))


def test_init_writes_through_the_whole_node_directory():
    """The image holds its own store at /nix, so this one goes elsewhere."""
    assert _parse("init").store == Path("/nix-volume")


def test_run_reads_the_store_where_it_is_already_mounted():
    """/nix is the store itself here, so a /nix prefix would read /nix/nix."""
    assert _parse("run", "nixkube").store == Path("/")


def test_a_program_keeps_its_own_flags():
    args = _parse("run", "nixkube", "--verbose", "--store", "x")
    assert args.program == "nixkube"
    assert args.argv == ["--verbose", "--store", "x"]


def test_the_environment_overrides_either_prefix(monkeypatch):
    monkeypatch.setenv("APPSTARTER_STORE", "/mnt/store")
    assert _parse("init").store == Path("/mnt/store")
    assert _parse("run", "nixkube").store == Path("/mnt/store")
