"""Shared test fixtures and helpers for grpclib_ttrpc tests."""

import asyncio
import struct

import grpclib.const
import pytest_asyncio
from grpclib_ttrpc.protocol import (
    _HEADER_FMT,
    FLAG_REMOTE_CLOSED,
    FLAG_REMOTE_OPEN,
    HEADER_SIZE,
    MSG_TYPE_DATA,
    MSG_TYPE_REQUEST,
    MSG_TYPE_RESPONSE,
    AbstractTtrpcHandler,
)
from ttrpc.ttrpc_pb2 import Request

from .dummy_pb2 import (
    DummyReply,  # type: ignore[import-not-found]
    DummyRequest,  # type: ignore[import-not-found]
)

# ---------------------------------------------------------------------------
# Frame helpers (used by both unit tests and the fake client)
# ---------------------------------------------------------------------------


def encode_frame(stream_id: int, msg_type: int, flags: int, payload: bytes) -> bytes:
    """Pack one ttrpc wire frame."""
    header = struct.pack(_HEADER_FMT, len(payload), stream_id, msg_type, flags)
    return header + payload


def decode_frame(data: bytes) -> tuple:
    """Unpack one ttrpc frame from the front of *data*.

    Returns ``(length, stream_id, msg_type, flags, payload, remainder)``.
    """
    length, stream_id, msg_type, flags = struct.unpack_from(_HEADER_FMT, data, 0)
    payload = data[HEADER_SIZE : HEADER_SIZE + length]
    remainder = data[HEADER_SIZE + length :]
    return length, stream_id, msg_type, flags, payload, remainder


# ---------------------------------------------------------------------------
# FakeTransport
# ---------------------------------------------------------------------------


class FakeTransport(asyncio.Transport):
    """Stub transport that records written bytes."""

    def __init__(self) -> None:
        super().__init__()
        self.written = bytearray()
        self._closing = False

    def write(self, data: bytes) -> None:  # type: ignore[override]
        self.written.extend(data)

    def is_closing(self) -> bool:
        return self._closing

    def close(self) -> None:
        self._closing = True

    # asyncio.BaseTransport requires this
    def get_extra_info(self, name: str, default=None):
        return default

    def pop_frames(self):
        """Return a list of (stream_id, msg_type, flags, payload) tuples."""
        frames = []
        data = bytes(self.written)
        self.written.clear()
        while len(data) >= HEADER_SIZE:
            _length, stream_id, msg_type, flags, payload, data = decode_frame(data)
            frames.append((stream_id, msg_type, flags, payload))
        return frames


# ---------------------------------------------------------------------------
# FakeHandler
# ---------------------------------------------------------------------------


class FakeHandler(AbstractTtrpcHandler):
    """Minimal handler for protocol-layer unit tests."""

    def __init__(self) -> None:
        self.accepted = []  # list of (raw_stream, initial_payload, flags)
        self.closed = False

    def accept(self, raw_stream, initial_payload, flags, release):
        self.accepted.append((raw_stream, initial_payload, flags))
        # Don't release — tests inspect the stream.

    def close(self):
        self.closed = True


# ---------------------------------------------------------------------------
# Dummy service for handler / functional tests
# ---------------------------------------------------------------------------


class DummyServiceImpl:
    """Test implementation of all four cardinalities."""

    async def UnaryUnary(self, stream):
        request: DummyRequest = await stream.recv_message()
        await stream.send_message(DummyReply(value=f"echo:{request.value}"))

    async def UnaryStream(self, stream):
        request: DummyRequest = await stream.recv_message()
        for i in range(3):
            await stream.send_message(DummyReply(value=f"{request.value}:{i}"))

    async def StreamUnary(self, stream):
        values = []
        async for msg in stream:
            values.append(msg.value)
        await stream.send_message(DummyReply(value=",".join(values)))

    async def StreamStream(self, stream):
        async for msg in stream:
            await stream.send_message(DummyReply(value=f"echo:{msg.value}"))

    def __mapping__(self):
        return {
            "/dummy.DummyService/UnaryUnary": grpclib.const.Handler(
                self.UnaryUnary,
                grpclib.const.Cardinality.UNARY_UNARY,
                DummyRequest,
                DummyReply,
            ),
            "/dummy.DummyService/UnaryStream": grpclib.const.Handler(
                self.UnaryStream,
                grpclib.const.Cardinality.UNARY_STREAM,
                DummyRequest,
                DummyReply,
            ),
            "/dummy.DummyService/StreamUnary": grpclib.const.Handler(
                self.StreamUnary,
                grpclib.const.Cardinality.STREAM_UNARY,
                DummyRequest,
                DummyReply,
            ),
            "/dummy.DummyService/StreamStream": grpclib.const.Handler(
                self.StreamStream,
                grpclib.const.Cardinality.STREAM_STREAM,
                DummyRequest,
                DummyReply,
            ),
        }


# ---------------------------------------------------------------------------
# Minimal ttRPC raw client (used in functional tests)
# ---------------------------------------------------------------------------


class TtrpcClient:
    """Very thin ttrpc client for functional tests — no framing buffering."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._next_stream_id = 1  # odd, client-initiated

    def _next_id(self) -> int:
        sid = self._next_stream_id
        self._next_stream_id += 2
        return sid

    async def send_unary_request(
        self,
        service: str,
        method: str,
        payload: bytes,
        *,
        timeout_nano: int = 0,
        stream_id: int | None = None,
    ) -> bytes:
        """Send a unary ttrpc request and await the response payload."""
        sid = stream_id if stream_id is not None else self._next_id()
        req = Request(
            service=service,
            method=method,
            payload=payload,
            timeout_nano=timeout_nano,
        )
        req_bytes = req.SerializeToString()
        frame = encode_frame(sid, MSG_TYPE_REQUEST, FLAG_REMOTE_CLOSED, req_bytes)
        self._writer.write(frame)
        await self._writer.drain()
        return await self._read_response()

    async def send_data_frame(
        self,
        stream_id: int,
        payload: bytes,
        *,
        close: bool = False,
    ) -> None:
        """Send a DATA frame (for client-streaming tests)."""
        flags = FLAG_REMOTE_CLOSED if close else 0
        frame = encode_frame(stream_id, MSG_TYPE_DATA, flags, payload)
        self._writer.write(frame)
        await self._writer.drain()

    async def open_request(
        self,
        service: str,
        method: str,
        first_payload: bytes = b"",
    ) -> int:
        """Send the initial Request frame (without FLAG_REMOTE_CLOSED)."""
        sid = self._next_id()
        req = Request(
            service=service,
            method=method,
            payload=first_payload,
        )
        req_bytes = req.SerializeToString()
        frame = encode_frame(sid, MSG_TYPE_REQUEST, FLAG_REMOTE_OPEN, req_bytes)
        self._writer.write(frame)
        await self._writer.drain()
        return sid

    async def _read_frame(self) -> tuple:
        header = await self._reader.readexactly(HEADER_SIZE)
        length, stream_id, msg_type, flags = struct.unpack(_HEADER_FMT, header)
        payload = await self._reader.readexactly(length) if length else b""
        return stream_id, msg_type, flags, payload

    async def _read_response(self) -> bytes:
        """Read one RESPONSE frame and return its raw payload."""
        _, msg_type, _, payload = await self._read_frame()
        assert msg_type == MSG_TYPE_RESPONSE, f"expected RESPONSE, got {msg_type}"
        return payload

    async def read_data_frame(self) -> tuple:
        """Read one DATA frame. Returns (flags, payload)."""
        _, msg_type, flags, payload = await self._read_frame()
        assert msg_type == MSG_TYPE_DATA, f"expected DATA, got {msg_type}"
        return flags, payload

    def close(self) -> None:
        self._writer.close()


# ---------------------------------------------------------------------------
# pytest fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def loop():
    return asyncio.get_running_loop()
