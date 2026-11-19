"""PC side of the optional relay (protocol/PROTOCOL.md section 1g; server in relay_server.py).

For every paired phone the PC keeps a few idle outbound connections ("slots") parked at the relay in that
pairing's room. When the phone connects through the relay, the relay says GO; we answer READY, upgrade the
socket to TLS as the *server* (the phone pins our certificate exactly as on the LAN) and hand it to the
normal connection handler, then park a replacement slot.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
from pathlib import Path
from typing import TYPE_CHECKING

from . import crypto
from .pairing import PairedDevice

if TYPE_CHECKING:
    from .transport import ControlChannelServer

log = logging.getLogger("phonebridge.relay_client")

CONFIG_PATH = Path.home() / ".config" / "phonebridge" / "relay.json"
SLOTS_PER_DEVICE = 3
DEVICE_REFRESH_SECONDS = 20
DEFAULT_PORT = 28200


def load_config() -> tuple[str, int] | None:
    """(host, port) from $PHONEBRIDGE_RELAY ("host[:port]") or ~/.config/phonebridge/relay.json; None = relay off."""
    env = os.environ.get("PHONEBRIDGE_RELAY", "").strip()
    if env:
        host, _, port = env.partition(":")
        return host, int(port or DEFAULT_PORT)
    try:
        obj = json.loads(CONFIG_PATH.read_text())
        if obj.get("host"):
            return str(obj["host"]), int(obj.get("port") or DEFAULT_PORT)
    except (OSError, ValueError):
        pass
    return None


def save_config(host: str | None, port: int = DEFAULT_PORT) -> None:
    if not host:
        CONFIG_PATH.unlink(missing_ok=True)
        return
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps({"host": host, "port": port}))


class RelayClient:
    def __init__(self, server: "ControlChannelServer", host: str, port: int, slots: int = SLOTS_PER_DEVICE) -> None:
        self._server = server
        self.host, self.port, self._slots = host, port, slots
        self._tasks: dict[str, set[asyncio.Task]] = {}    # room -> slot tasks (parked or serving)
        self._idle: dict[str, int] = {}                   # room -> slots currently parked/connecting
        self._runner: asyncio.Task | None = None

    def start(self) -> None:
        self._runner = asyncio.ensure_future(self._run())

    def stop(self) -> None:
        if self._runner:
            self._runner.cancel()
        for tasks in self._tasks.values():
            for task in tasks:
                task.cancel()
        self._tasks.clear()
        self._idle.clear()

    async def _run(self) -> None:
        """Keeps slots parked for every currently paired phone (new pairings are picked up within seconds)."""
        while True:
            rooms = {crypto.relay_room(d.session_key) for d in PairedDevice.list_all()}
            for room in rooms:
                self._topup(room)
            for room in set(self._tasks) - rooms:          # unpaired phone
                for task in self._tasks.pop(room):
                    task.cancel()
                self._idle.pop(room, None)
            await asyncio.sleep(DEVICE_REFRESH_SECONDS)

    def _topup(self, room: str) -> None:
        """Make sure `slots` connections are parked for this room (called on a timer and right after a phone
        takes a slot, since a phone opens extra connections for file transfers and video)."""
        tasks = self._tasks.setdefault(room, set())
        while self._idle.get(room, 0) < self._slots:
            self._idle[room] = self._idle.get(room, 0) + 1
            task = asyncio.ensure_future(self._slot(room))
            tasks.add(task)
            task.add_done_callback(tasks.discard)

    async def _slot(self, room: str) -> None:
        """One parked connection: wait for a phone, serve it, finish."""
        state = {"idle": True}
        backoff = 5.0
        try:
            while True:
                try:
                    await self._park_and_serve(room, state)
                    return
                except asyncio.CancelledError:
                    raise
                except (OSError, asyncio.IncompleteReadError, ConnectionError) as exc:
                    log.warning("relay slot failed: %r", exc)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
        finally:
            if state["idle"]:                    # died while still parked
                self._idle[room] = max(0, self._idle.get(room, 1) - 1)

    async def _park_and_serve(self, room: str, state: dict) -> None:
        if not state["idle"]:                              # retrying after a failure that happened past GO
            state["idle"] = True
            self._idle[room] = self._idle.get(room, 0) + 1
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader()
        transport, protocol = await asyncio.wait_for(
            loop.create_connection(lambda: asyncio.StreamReaderProtocol(reader), self.host, self.port), 10)
        sock = transport.get_extra_info("socket")
        if sock is not None:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        writer = asyncio.StreamWriter(transport, protocol, reader, loop)
        try:
            await self._serve(room, state, loop, reader, writer, transport, protocol)
        finally:
            writer.close()          # also when cancelled (relay turned off), so the relay drops the parked slot

    async def _serve(self, room, state, loop, reader, writer, transport, protocol) -> None:
        writer.write(f"PBRELAY pc {room}\n".encode())
        line = await reader.readline()                     # blocks until a phone arrives
        if line.strip() != b"GO":
            raise ConnectionError("relay closed the slot")
        log.info("a phone is connecting through the relay")
        state["idle"] = False
        self._idle[room] = max(0, self._idle.get(room, 1) - 1)
        self._topup(room)                                  # park a replacement right away
        writer.write(b"READY\n")
        # No await between READY and start_tls: loop.start_tls pauses reading first, so the phone's TLS
        # ClientHello (which the relay only forwards after READY) can't be eaten by the plain reader.
        tls = await loop.start_tls(transport, protocol, self._server.ssl_context, server_side=True,
                                   ssl_handshake_timeout=15)
        tls_writer = asyncio.StreamWriter(tls, protocol, reader, loop)
        await self._server.handle_stream(reader, tls_writer, relayed=True)
