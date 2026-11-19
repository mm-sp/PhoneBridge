"""Optional self-hosted relay (protocol/PROTOCOL.md section 1g) so a phone can reach its PC when the two are not
on the same network. Run it on any machine both can reach:

    python3 -m phonebridge.relay_server --host 0.0.0.0 --port 28200

It has no accounts and stores nothing. The PC keeps a few idle outbound connections open per pairing ("slots");
when the phone connects to the same room the relay tells the PC, then copies bytes between the two sockets. Those
bytes are the normal TLS + AES-GCM PhoneBridge traffic (the phone still pins the PC's certificate), so the relay
can see who connected when and how much data moved, never what it says.

Wire:  first line  `PBRELAY pc <room>`  or  `PBRELAY phone <room>`  (room = 32 hex, crypto.relay_room()).
  pc    -> parked until a phone arrives; the relay then sends `GO`, the PC answers `READY` and starts TLS.
  phone -> `OK` once the PC is ready (then bytes flow both ways), or `NOPC` if no PC is waiting.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import re
import socket
from collections import deque

log = logging.getLogger("phonebridge.relay")

MAX_SLOTS_PER_ROOM = 8
MAX_ROOMS = 10_000
FIRST_LINE_TIMEOUT = 10
READY_TIMEOUT = 8
_LINE = re.compile(rb"^PBRELAY (pc|phone) ([0-9a-f]{32})$")


class Slot:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader, self.writer = reader, writer
        self.finished: asyncio.Future = asyncio.get_running_loop().create_future()

    def alive(self) -> bool:
        return not self.writer.is_closing() and not self.reader.at_eof()


class RelayServer:
    def __init__(self) -> None:
        self._rooms: dict[str, deque[Slot]] = {}
        self.server: asyncio.AbstractServer | None = None

    async def start(self, host: str, port: int) -> int:
        self.server = await asyncio.start_server(self._handle, host, port)
        return self.server.sockets[0].getsockname()[1]

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        sock = writer.get_extra_info("socket")
        if sock is not None:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        try:
            line = await asyncio.wait_for(reader.readline(), FIRST_LINE_TIMEOUT)
        except (asyncio.TimeoutError, ConnectionError, ValueError):
            writer.close()
            return
        m = _LINE.match(line.rstrip(b"\r\n"))
        if not m:
            writer.close()
            return
        role, room = m.group(1).decode(), m.group(2).decode()
        try:
            if role == "pc":
                await self._park(room, Slot(reader, writer))
            else:
                await self._connect_phone(room, reader, writer)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            if role == "phone":
                writer.close()

    async def _park(self, room: str, slot: Slot) -> None:
        if room not in self._rooms and len(self._rooms) >= MAX_ROOMS:
            slot.writer.close()
            return
        slots = self._rooms.setdefault(room, deque())
        while len(slots) >= MAX_SLOTS_PER_ROOM:
            slots.popleft().writer.close()
        slots.append(slot)
        try:
            await slot.finished          # stays parked until a phone takes it or it dies
        finally:
            if slot in slots:
                slots.remove(slot)
            if not slots:
                self._rooms.pop(room, None)
            slot.writer.close()

    def _take(self, room: str) -> Slot | None:
        slots = self._rooms.get(room)
        while slots:
            slot = slots.popleft()
            if slot.alive():
                return slot
            slot.writer.close()
            if not slot.finished.done():
                slot.finished.set_result(None)
        return None

    async def _connect_phone(self, room: str, p_reader: asyncio.StreamReader, p_writer: asyncio.StreamWriter) -> None:
        while True:
            slot = self._take(room)
            if slot is None:
                p_writer.write(b"NOPC\n")
                await p_writer.drain()
                return
            try:
                slot.writer.write(b"GO\n")
                await slot.writer.drain()
                ready = await asyncio.wait_for(slot.reader.readline(), READY_TIMEOUT)
            except (asyncio.TimeoutError, ConnectionError):
                ready = b""
            if ready.strip() == b"READY":
                break
            slot.writer.close()
            if not slot.finished.done():
                slot.finished.set_result(None)
        p_writer.write(b"OK\n")
        await p_writer.drain()
        log.info("relayed a connection in room %s…", room[:6])
        try:
            await asyncio.gather(_pipe(p_reader, slot.writer), _pipe(slot.reader, p_writer))
        finally:
            slot.writer.close()
            if not slot.finished.done():
                slot.finished.set_result(None)


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        writer.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="PhoneBridge relay (no accounts, forwards encrypted bytes only)")
    parser.add_argument("--host", default="0.0.0.0")  # nosec B104 - the relay is meant to be reachable
    parser.add_argument("--port", type=int, default=28200)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")

    async def run() -> None:
        relay = RelayServer()
        port = await relay.start(args.host, args.port)
        log.info("relay listening on %s:%d", args.host, port)
        await asyncio.Event().wait()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
