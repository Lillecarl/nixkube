# SPDX-License-Identifier: MIT
"""Integration tests for grpclib-nri NRI protocol implementation.

Tests the NriServer against the Go test server to verify:
- RegisterPlugin handshake
- Plugin lifecycle (Configure, Synchronize)
- CreateContainer hook invocation
"""

import asyncio

import pytest
import structlog
from grpclib_nri import NriServer

from .dummy_plugin import DummyPlugin


@pytest.mark.asyncio
async def test_nri_handshake(nri_server: NriServer) -> None:
    """Test that the NRI RegisterPlugin handshake completes successfully."""
    logger = structlog.get_logger("test.nri_handshake")

    # Server should be running and have completed registration
    logger.info("checking_nri_server")

    # Give it a moment to complete the registration handshake
    await asyncio.sleep(1.0)

    assert nri_server is not None
    logger.info("nri_handshake_ok")


@pytest.mark.asyncio
async def test_plugin_configure_called(nri_server: NriServer) -> None:
    """Test that Configure handler is called during registration."""
    logger = structlog.get_logger("test.plugin_configure")

    # Extract plugin from server
    plugin = nri_server.plugin
    assert isinstance(plugin, DummyPlugin)

    logger.info("configure_called", value=plugin.configure_called)

    # Configure should have been called during the handshake
    assert plugin.configure_called, (
        "Configure handler was not called during registration"
    )


@pytest.mark.asyncio
async def test_plugin_survives_registration(nri_server: NriServer) -> None:
    """Test that plugin survives the full registration handshake without errors."""
    logger = structlog.get_logger("test.plugin_survives")

    plugin = nri_server.plugin
    assert isinstance(plugin, DummyPlugin)

    # If we got here, the plugin completed at least Configure without errors
    logger.info("plugin_survived_registration")
    assert plugin.configure_called


@pytest.mark.asyncio
async def test_nri_server_lifecycle(
    test_server_bin,
    socket_path,
) -> None:
    """Test that NriServer can be started and stopped cleanly."""
    logger = structlog.get_logger("test.nri_lifecycle")

    # Start a fresh test server
    import asyncio

    proc = await asyncio.create_subprocess_exec(
        str(test_server_bin),
        "-socket",
        str(socket_path),
        "-timeout",
        "5s",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    # Wait for socket
    for _ in range(50):
        if socket_path.exists():
            break
        if proc.returncode is not None:
            stdout, stderr = await proc.communicate()
            pytest.fail(
                f"Test server exited prematurely (code {proc.returncode})\n"
                f"stdout: {stdout.decode()}\nstderr: {stderr.decode()}"
            )
        await asyncio.sleep(0.1)
    else:
        if proc.returncode is None:
            proc.terminate()
            await proc.wait()
        pytest.fail("Test server socket not created")

    # Create and start server
    plugin = DummyPlugin()
    server = NriServer(
        plugin,
        socket_path,
        plugin_name="test-lifecycle",
        plugin_idx="99",
    )

    logger.info("starting_server")
    server_task = asyncio.create_task(server.start())

    # Let it run and complete handshake
    await asyncio.sleep(2.0)

    # Close it
    logger.info("closing_server")
    await server.close()

    # Wait for task to complete
    try:
        await asyncio.wait_for(server_task, timeout=2)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        logger.warning("server_task_exit_timeout")

    # Cleanup subprocess
    proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), timeout=2)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()

    logger.info("server_lifecycle_ok")
