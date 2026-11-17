"""Transport layer for the cTrader Open API (JSON mode).

CTraderTransport is the seam the unit tests drive: everything the bridge
does goes through send()/messages(), so a fake transport yields a full
in-process server without any network. TcpNewlineTransport is the real
asyncio TLS client for connection day (demo first — see ctrader/README.md).
It is NOT started by any test, script or service in this repo.
"""

import asyncio
import logging
from typing import AsyncIterator, Protocol

from .protocol import FrameDecoder, encode_frame

logger = logging.getLogger(__name__)


class CTraderTransport(Protocol):
    """Byte-frame transport contract used by CTraderBridge."""

    async def connect(self) -> None:
        """Establish the connection (raises on failure)."""
        ...

    async def close(self) -> None:
        """Tear down the connection (idempotent)."""
        ...

    async def send(self, data: bytes) -> None:
        """Send one complete frame (newline included)."""
        ...

    def messages(self) -> AsyncIterator[bytes]:
        """Yield received frames; ends when the connection closes."""
        ...


class TcpNewlineTransport:
    """Real transport: TLS TCP to ctraderapi.com:5036, newline-JSON frames."""

    def __init__(self, host: str, port: int, connect_timeout: float = 10.0) -> None:
        self.host = host
        self.port = port
        self.connect_timeout = connect_timeout
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._decoder = FrameDecoder()
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._pump_task: asyncio.Task | None = None

    async def connect(self) -> None:
        """Open the TLS connection and start the background read pump.

        Reconnect-safe: state from a previous session is reset first — a
        stale partial frame, a stale end-of-stream sentinel left by the old
        read pump, or a still-running old pump would corrupt/kill the new
        session otherwise.
        """
        if self._pump_task is not None and not self._pump_task.done():
            self._pump_task.cancel()
        self._pump_task = None
        self._decoder = FrameDecoder()
        while True:  # drain stale frames + end-of-stream sentinel
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        self._reader, self._writer = await asyncio.wait_for(
            self._open_streams(), timeout=self.connect_timeout,
        )
        self._pump_task = asyncio.create_task(self._read_pump())
        self._pump_task.add_done_callback(self._on_pump_done)

    async def _open_streams(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """Open the TLS socket (separate method = test seam)."""
        return await asyncio.open_connection(self.host, self.port, ssl=True)

    async def close(self) -> None:
        if self._pump_task is not None and not self._pump_task.done():
            self._pump_task.cancel()
        self._pump_task = None
        if self._writer is not None:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:  # noqa: BLE001 — best-effort teardown
                pass
        self._writer = None
        self._reader = None

    async def send(self, data: bytes) -> None:
        if self._writer is None:
            raise ConnectionError(f"not connected to {self.host}:{self.port}")
        self._writer.write(data)
        await self._writer.drain()

    def messages(self) -> AsyncIterator[bytes]:
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[bytes]:
        while True:
            item = await self._queue.get()
            if item is None:  # closed — end iteration
                break
            yield item

    async def _read_pump(self) -> None:
        """Feed the socket into the frame decoder, queueing complete frames."""
        assert self._reader is not None
        try:
            while True:
                chunk = await self._reader.read(4096)
                if not chunk:
                    logger.warning("cTrader %s:%s closed by peer", self.host, self.port)
                    break
                for frame in self._decoder.feed(chunk):
                    await self._queue.put(frame)
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001 — pump must never leak
            logger.error("cTrader read pump failed: %s", exc)
        finally:
            await self._queue.put(None)

    def _on_pump_done(self, task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error("cTrader read pump task errored: %s", exc)


def build_transport(host: str, port: int) -> CTraderTransport:
    """Factory used in production wiring (kept separate for test seam)."""
    return TcpNewlineTransport(host, port)


__all__ = ["CTraderTransport", "TcpNewlineTransport", "build_transport", "encode_frame"]
