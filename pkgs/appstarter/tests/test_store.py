# SPDX-License-Identifier: MIT

import subprocess

import pytest
from appstarter import store


def _completed(returncode: int, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )


def test_link_replaces_an_existing_pointer(tmp_path):
    link = tmp_path / "result"
    store.link("/nix/store/first", link)
    store.link("/nix/store/second", link)
    assert link.readlink().as_posix() == "/nix/store/second"


def test_link_leaves_no_staging_file_behind(tmp_path):
    link = tmp_path / "nested" / "result"
    store.link("/nix/store/first", link)
    assert [p.name for p in link.parent.iterdir()] == ["result"]


def test_verify_accepts_a_closure_that_is_on_disk(tmp_path, monkeypatch):
    (tmp_path / "nix" / "store").mkdir(parents=True)
    (tmp_path / "nix" / "store" / "aaa").write_text("")
    monkeypatch.setattr(
        store, "_run", lambda *a, **k: _completed(0, "/nix/store/aaa\n")
    )
    store.verify("/nix/store/aaa", tmp_path)


def test_verify_rejects_a_path_that_is_registered_and_absent(tmp_path, monkeypatch):
    """Issue #8. `nix path-info` answers from the database, so it passes."""
    monkeypatch.setattr(
        store, "_run", lambda *a, **k: _completed(0, "/nix/store/gone\n")
    )
    with pytest.raises(store.StoreError, match="absent"):
        store.verify("/nix/store/gone", tmp_path)


def test_verify_accepts_a_dangling_symlink(tmp_path, monkeypatch):
    """`nix-*-man` points at an absolute path that resolves only under /nix."""
    (tmp_path / "nix" / "store").mkdir(parents=True)
    (tmp_path / "nix" / "store" / "man").symlink_to("/nix/store/elsewhere")
    monkeypatch.setattr(
        store, "_run", lambda *a, **k: _completed(0, "/nix/store/man\n")
    )
    store.verify("/nix/store/man", tmp_path)


def test_build_reports_what_nix_said(tmp_path, monkeypatch):
    monkeypatch.setattr(
        store, "_run", lambda *a, **k: _completed(1, stderr="no substituter\n")
    )
    with pytest.raises(store.StoreError, match="no substituter"):
        store.build("/nix/store/aaa", tmp_path, ["local"], tmp_path / "result")


def test_ping_treats_a_timeout_as_no_answer(monkeypatch):
    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd="nix", timeout=1)

    monkeypatch.setattr(store, "_run", timeout)
    assert store.ping("ssh-ng://nix@pynixd") is False
