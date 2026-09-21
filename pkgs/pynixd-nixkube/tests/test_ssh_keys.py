# SPDX-License-Identifier: MIT
"""An authorized-keys file with no usable entries must not stop pynixd.

The failure this file exists for crash-looped `pynixd-0` in CI: an empty
`pynixd.authorizedKeys` renders `/etc/ssh/authorized_keys` as a 0-byte file,
`_collect_key_paths` passed it on because `is_file()` is true for it, and
asyncssh answers `ValueError: No valid entries found` rather than skipping it.
The pod never bound :8080 and `Wait for nix-csi cache pod` timed out.

Real asyncssh, not a stand-in for it. The whole defect is what that library
does with a file this code thought was fine.
"""

from __future__ import annotations

from pathlib import Path

import asyncssh.auth_keys
import pytest
from structlog.testing import capture_logs

from pynixd_nixkube import ssh_keys


class _FakeAcceptor:
    """`update()` as asyncssh's SSHAcceptor implements it, for the two keys
    this code passes: the real parse for authorized keys, and a record for
    host keys."""

    def __init__(self) -> None:
        self.authorized: list[str] | None = None
        self.host_keys: list[str] | None = None

    def update(self, **kwargs) -> None:
        if "authorized_client_keys" in kwargs:
            paths = kwargs["authorized_client_keys"]
            asyncssh.auth_keys.read_authorized_keys(paths)
            self.authorized = paths
        if "server_host_keys" in kwargs:
            self.host_keys = kwargs["server_host_keys"]


class _FakeServer:
    def __init__(self) -> None:
        self.ssh_server = _FakeAcceptor()


@pytest.fixture
def key_files(tmp_path: Path, monkeypatch):
    """Point the module at files this test owns."""

    def use(**bodies: str) -> list[Path]:
        paths = []
        for name, body in bodies.items():
            path = tmp_path / name
            path.write_text(body)
            paths.append(path)
        monkeypatch.setattr(ssh_keys, "_KEY_FILES", paths)
        return paths

    return use


class TestCollectingKeyPaths:
    def test_an_empty_file_is_left_out(self, key_files) -> None:
        key_files(empty="")

        assert ssh_keys._collect_key_paths() == []

    def test_a_file_with_content_is_kept(self, key_files) -> None:
        paths = key_files(real="# a comment\n")

        assert ssh_keys._collect_key_paths() == [str(paths[0])]

    def test_a_missing_file_is_left_out(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(ssh_keys, "_KEY_FILES", [tmp_path / "absent"])

        assert ssh_keys._collect_key_paths() == []


class TestApplyingThem:
    def test_an_empty_file_still_leaves_the_host_keys_installed(
        self, key_files
    ) -> None:
        """The crash-loop, stated. asyncssh raises on the empty file, and that
        used to take the `server_host_keys` call below it down as well."""
        key_files(empty="")
        server = _FakeServer()

        ssh_keys.apply_authorized_keys(server)  # type: ignore[arg-type]

        assert server.ssh_server.host_keys == [ssh_keys._HOST_KEY]
        assert server.ssh_server.authorized is None

    def test_a_comments_only_file_is_reported_and_stepped_over(self, key_files) -> None:
        """Not 0 bytes, so the size filter cannot see it. asyncssh parses zero
        entries from it and raises exactly as it does for an empty one."""
        key_files(comments="# no keys here\n\n")
        server = _FakeServer()

        with capture_logs() as logs:
            ssh_keys.apply_authorized_keys(server)  # type: ignore[arg-type]

        assert server.ssh_server.host_keys == [ssh_keys._HOST_KEY]
        assert [e["event"] for e in logs if e["log_level"] == "warning"] == [
            "authorized_keys_unusable"
        ]

    def test_a_real_key_is_loaded(self, key_files, ed25519_key: str) -> None:
        """The positive control. Without it every test above passes against a
        `_collect_key_paths` that returns nothing at all."""
        paths = key_files(good=ed25519_key)
        server = _FakeServer()

        ssh_keys.apply_authorized_keys(server)  # type: ignore[arg-type]

        assert server.ssh_server.authorized == [str(paths[0])]
        assert server.ssh_server.host_keys == [ssh_keys._HOST_KEY]


@pytest.fixture(scope="module")
def ed25519_key() -> str:
    """One real public key line, generated rather than pasted."""
    import asyncssh

    return asyncssh.generate_private_key("ssh-ed25519").export_public_key().decode()
