# SPDX-License-Identifier: MIT

"""What nri-wait does while it waits.

A build is slower than any timeout somebody picks, so the hook does not
time the build. It times the daemon's silence. These tests hold it to that,
because the bug they follow made the heartbeat decoration: a second,
absolute deadline of the same length ran beside the resettable one and
nothing reset it, so no build could last longer than NRI_TIMEOUT.
"""

import json
import threading
import time

import pytest
import zmq
from nri_wait import connect_updates, wait_for_completion

CONTAINER = "0123456789abcdef"

# Shorter than the pauses below, so a test that hangs fails in a second
# rather than sitting there.
TIMEOUT = 1


@pytest.fixture
def pub(tmp_path):
    """A PUB socket standing in for the build daemon, and its path."""
    context = zmq.Context()
    socket = context.socket(zmq.PUB)
    path = tmp_path / "wait-pub.sock"
    socket.bind(f"ipc://{path}")
    yield socket, str(path)
    socket.close()
    context.term()


def message(status: str, container_id: str = CONTAINER) -> bytes:
    return json.dumps({"container_id": container_id, "status": status}).encode()


def never() -> bool:
    """A recheck that says the build is not done."""
    return False


class TestHeartbeat:
    """The daemon says it is alive. The hook keeps waiting."""

    def test_progress_outlives_the_timeout(self, pub):
        """A build three times longer than TIMEOUT still completes.

        The publisher sends progress from the start, so the test never has
        to guess when the subscription arrives -- ZeroMQ drops what nobody
        has joined for yet, and a later heartbeat lands either way.
        """
        socket, path = pub
        stop = threading.Event()

        def daemon():
            deadline = time.time() + 3 * TIMEOUT
            while time.time() < deadline and not stop.is_set():
                socket.send(message("progress"))
                time.sleep(TIMEOUT / 10)
            socket.send(message("done"))

        thread = threading.Thread(target=daemon)
        thread.start()
        try:
            context = zmq.Context()
            sub = connect_updates(context, path)
            started = time.time()
            wait_for_completion(sub, CONTAINER, TIMEOUT, never)
            waited = time.time() - started
            context.term()
        finally:
            stop.set()
            thread.join()

        assert waited > TIMEOUT, "the heartbeat did not extend the wait"

    def test_another_container_does_not_count(self, pub):
        """Heartbeats for a different container are not this one's."""
        socket, path = pub
        stop = threading.Event()

        def daemon():
            while not stop.is_set():
                socket.send(message("progress", "some-other-container"))
                time.sleep(TIMEOUT / 10)

        thread = threading.Thread(target=daemon)
        thread.start()
        try:
            context = zmq.Context()
            sub = connect_updates(context, path)
            with pytest.raises(SystemExit) as exit_info:
                wait_for_completion(sub, CONTAINER, TIMEOUT, never)
            context.term()
        finally:
            stop.set()
            thread.join()

        assert exit_info.value.code == 1


class TestSilence:
    """The daemon says nothing. That is the only failure."""

    def test_silence_fails(self, pub):
        _, path = pub
        context = zmq.Context()
        sub = connect_updates(context, path)
        with pytest.raises(SystemExit) as exit_info:
            wait_for_completion(sub, CONTAINER, TIMEOUT, never)
        context.term()
        assert exit_info.value.code == 1

    def test_the_query_socket_has_the_last_word(self, pub):
        """A "done" the hook never heard still counts.

        PUB is the fast path and REP is the truth, so silence asks once
        more before it gives up.
        """
        _, path = pub
        context = zmq.Context()
        sub = connect_updates(context, path)
        wait_for_completion(sub, CONTAINER, TIMEOUT, lambda: True)
        context.term()
