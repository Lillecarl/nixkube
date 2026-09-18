# SPDX-License-Identifier: MIT

import asyncio
import os
import tempfile
from pathlib import Path

import pytest
from src.hardlinks import _YIELD_EVERY, deref_hardlink_tree, hardlink_tree


class TestHardlinkTree:
    """Tests for hardlink_tree function."""

    @pytest.mark.asyncio
    async def test_hardlink_single_file(self):
        """Hardlink a single file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            src_file = Path(tmpdir) / "src" / "file.txt"
            src_file.parent.mkdir(parents=True)
            src_file.write_text("content")

            dst_file = Path(tmpdir) / "dst" / "file.txt"
            await hardlink_tree(src_file, dst_file)

            assert dst_file.exists()
            assert dst_file.read_text() == "content"
            # Check they're hardlinks (same inode)
            assert os.stat(src_file).st_ino == os.stat(dst_file).st_ino

    @pytest.mark.asyncio
    async def test_hardlink_directory(self):
        """Hardlink a directory tree."""
        with tempfile.TemporaryDirectory() as tmpdir:
            src_dir = Path(tmpdir) / "src"
            src_dir.mkdir()
            (src_dir / "file1.txt").write_text("data1")
            (src_dir / "subdir").mkdir()
            (src_dir / "subdir" / "file2.txt").write_text("data2")

            dst_dir = Path(tmpdir) / "dst"
            await hardlink_tree(src_dir, dst_dir)

            assert (dst_dir / "file1.txt").read_text() == "data1"
            assert (dst_dir / "subdir" / "file2.txt").read_text() == "data2"

    @pytest.mark.asyncio
    async def test_hardlink_symlink_preserved(self):
        """Hardlink should preserve symlinks."""
        with tempfile.TemporaryDirectory() as tmpdir:
            src_dir = Path(tmpdir) / "src"
            src_dir.mkdir()
            target_file = src_dir / "target.txt"
            target_file.write_text("target")
            link_path = src_dir / "link"
            os.symlink("target.txt", link_path)

            dst_dir = Path(tmpdir) / "dst"
            await hardlink_tree(src_dir, dst_dir)

            dst_link = dst_dir / "link"
            assert dst_link.is_symlink()
            assert os.readlink(dst_link) == "target.txt"


class TestDerefHardlinkTree:
    """Tests for deref_hardlink_tree function."""

    @pytest.mark.asyncio
    async def test_dereference_symlink_in_store(self):
        """Symlink to /nix/store target should be dereferenced."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create fake /nix/store structure
            store_dir = Path(tmpdir) / "nix" / "store"
            store_dir.mkdir(parents=True)

            target = store_dir / "target-file"
            target.write_text("target content")

            src_dir = store_dir / "source-dir"
            src_dir.mkdir()
            link = src_dir / "link"
            os.symlink(str(target), str(link))

            dst = Path(tmpdir) / "output"
            await deref_hardlink_tree(src_dir, dst)

            # Symlink should be dereferenced (file should exist, not symlink)
            result_link = dst / "link"
            assert result_link.exists()
            assert result_link.read_text() == "target content"

    @pytest.mark.asyncio
    async def test_broken_symlink_in_store_copied(self):
        """Broken symlink in /nix/store should be copied as-is."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store_dir = Path(tmpdir) / "nix" / "store"
            store_dir.mkdir(parents=True)

            src_dir = store_dir / "source"
            src_dir.mkdir()
            broken_link = src_dir / "broken"
            # Create symlink to non-existent target in /nix/store
            os.symlink("/nix/store/nonexistent-path", broken_link)

            dst = Path(tmpdir) / "output"
            await deref_hardlink_tree(src_dir, dst)

            result_link = dst / "broken"
            # Should be symlink, not dereferenced
            assert result_link.is_symlink()
            assert os.readlink(result_link) == "/nix/store/nonexistent-path"

    @pytest.mark.asyncio
    async def test_symlink_outside_store_copied(self):
        """Symlink pointing outside /nix/store should be copied as-is."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store_dir = Path(tmpdir) / "nix" / "store"
            store_dir.mkdir(parents=True)

            src_dir = store_dir / "source"
            src_dir.mkdir()

            # Create symlink to outside /nix/store
            link = src_dir / "external_link"
            os.symlink("/etc/passwd", link)

            dst = Path(tmpdir) / "output"
            await deref_hardlink_tree(src_dir, dst)

            result_link = dst / "external_link"
            assert result_link.is_symlink()
            assert os.readlink(result_link) == "/etc/passwd"

    @pytest.mark.asyncio
    async def test_regular_file_hardlinked(self):
        """Regular files should be hardlinked."""
        with tempfile.TemporaryDirectory() as tmpdir:
            src_file = Path(tmpdir) / "src_file.txt"
            src_file.write_text("content")

            dst_file = Path(tmpdir) / "dst_file.txt"
            await deref_hardlink_tree(src_file, dst_file)

            assert dst_file.read_text() == "content"
            # Should be hardlinks (same inode)
            assert os.stat(src_file).st_ino == os.stat(dst_file).st_ino

    @pytest.mark.asyncio
    async def test_nested_structure_with_symlinks(self):
        """Complex nested structure with mixed symlinks and files."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store_dir = Path(tmpdir) / "nix" / "store"
            store_dir.mkdir(parents=True)

            src = store_dir / "app"
            src.mkdir()

            # Regular file
            (src / "main").write_text("executable")

            # Subdir with content
            subdir = src / "lib"
            subdir.mkdir()
            (subdir / "lib.so").write_text("library")

            # Symlink to in-store target
            target = store_dir / "lib-target"
            target.mkdir()
            (target / "real.so").write_text("real library")
            os.symlink(str(target), src / "link_to_target")

            dst = Path(tmpdir) / "output"
            await deref_hardlink_tree(src, dst)

            assert (dst / "main").read_text() == "executable"
            assert (dst / "lib" / "lib.so").read_text() == "library"
            # Symlink to target should be dereferenced
            assert (dst / "link_to_target" / "real.so").read_text() == "real library"

    @pytest.mark.asyncio
    async def test_empty_directory(self):
        """Empty directory should be created at destination."""
        with tempfile.TemporaryDirectory() as tmpdir:
            src_dir = Path(tmpdir) / "empty"
            src_dir.mkdir()

            dst = Path(tmpdir) / "output"
            await deref_hardlink_tree(src_dir, dst)

            assert dst.exists()
            assert dst.is_dir()


class TestEventLoopStaysAlive:
    """The reason these functions are async at all.

    `prepare_volume` runs on the loop that serves the NRI heartbeat and the
    ZeroMQ REP socket, and `nri-wait` gives up after 30s of silence. A walk
    that never yields kills the container it is linking a store into.
    Issue #45.
    """

    @pytest.mark.asyncio
    async def test_a_walk_lets_another_task_run(self):
        ticks = 0

        async def ticker():
            nonlocal ticks
            while True:
                await asyncio.sleep(0)
                ticks += 1

        with tempfile.TemporaryDirectory() as tmpdir:
            src_dir = Path(tmpdir) / "src"
            src_dir.mkdir()
            # More than one checkpoint's worth of entries.
            for i in range(_YIELD_EVERY * 3):
                (src_dir / f"f{i:05d}").write_text("x")

            pump = asyncio.create_task(ticker())
            await hardlink_tree(src_dir, Path(tmpdir) / "dst")
            pump.cancel()

        assert ticks > 0, "the walk never gave the loop a turn"
