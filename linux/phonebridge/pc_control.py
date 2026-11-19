"""Answers the phone app's "control this PC" tabs (protocol/PROTOCOL.md section 4c):
PC info, open ports/network, apps, home-folder files, clipboard, and remote mouse /
keyboard / screen view through the RemoteDesktop portal (pc_remote.py).
"""
from __future__ import annotations

import asyncio
import logging

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

from .netutil import spawn
from . import file_transfer, pc_info
from .pc_remote import BTN_LEFT, BTN_MIDDLE, BTN_RIGHT, KEYSYMS, RemoteDesktop, ScreenStreamer
from . import pairing
from .pairing import PairedDevice
from .terminal import TerminalSession
from .transport import ControlChannelServer, DeviceConnection

log = logging.getLogger("phonebridge.pc_control")

_BUTTONS = {"left": BTN_LEFT, "right": BTN_RIGHT, "middle": BTN_MIDDLE}


class PcControl:
    def __init__(self, server: ControlChannelServer, loop: asyncio.AbstractEventLoop, clipboard_text) -> None:
        """`clipboard_text()` returns the PC's current clipboard text (GTK thread)."""
        self._server = server
        self._loop = loop
        self._clipboard_text = clipboard_text
        self._remote: RemoteDesktop | None = None
        self._streamer: ScreenStreamer | None = None
        self._stream_conn: DeviceConnection | None = None
        self._frame_in_flight = False
        self._needs_reask = False
        self._terms: dict[str, TerminalSession] = {}  # one shell per phone
        self._gate_cache: dict[str, tuple[tuple[int, int], bool]] = {}

        for type_, handler in {
            "pc.info_request": self._on_info,
            "pc.ports_request": self._on_ports,
            "pc.servers_request": self._on_servers,
            "pc.server_action": self._on_server_action,
            "pc.apps_request": self._on_apps,
            "pc.app_launch": self._on_app_launch,
            "pcfiles.list": self._on_files_list,
            "pcfiles.get": self._on_files_get,
            "pc.clipboard_get": self._on_clipboard_get,
            "pc.input": self._on_input,
            "pc.screen_start": self._on_screen_start,
            "pc.screen_stop": self._on_screen_stop,
            "pc.term_open": self._on_term_open,
            "pc.term_input": self._on_term_input,
            "pc.term_resize": self._on_term_resize,
            "pc.term_close": self._on_term_close,
        }.items():
            server.on(type_, self._gated(handler))
        server.on_connection_change(self._on_connection_change)

    # ------------------------------------------------------------- helpers

    def _pc_control_allowed(self, fingerprint: str) -> bool:
        """The per-phone switch, re-read only when the device file changed (one stat() per message instead of a
        read + JSON parse + key unwrap), so turning it off takes effect on the very next message."""
        try:
            st = (pairing.DEVICES_DIR / f"{fingerprint}.json").stat()
            stamp = (st.st_mtime_ns, st.st_size)
        except OSError:
            self._gate_cache.pop(fingerprint, None)
            return False
        hit = self._gate_cache.get(fingerprint)
        if hit and hit[0] == stamp:
            return hit[1]
        device = PairedDevice.load(fingerprint)
        allowed = bool(device and device.allow_pc_control)
        self._gate_cache[fingerprint] = (stamp, allowed)
        return allowed

    def _gated(self, handler):
        """Shell, files, input and screen view of this PC are off until the user enables
        "Allow this phone to control this PC" on the phone's card in the Device page."""

        async def wrapper(conn: DeviceConnection, env) -> None:
            if not self._pc_control_allowed(conn.fingerprint):
                await conn.send("control", "pc.status", {"denied": True, "message": "PC control is turned off for this "
                                                         "phone. On the PC: Device page → Allow PC control."})
                return
            await handler(conn, env)

        return wrapper

    async def _off_thread(self, fn, *args):
        return await self._loop.run_in_executor(None, fn, *args)

    def _send_from_gtk(self, conn: DeviceConnection, type_: str, payload: dict) -> None:
        asyncio.run_coroutine_threadsafe(conn.send("control", type_, payload), self._loop)

    def _on_connection_change(self, conn: DeviceConnection, connected: bool) -> None:
        if not connected:
            GLib.idle_add(self._stop_screen)
            term = self._terms.pop(conn.fingerprint, None)
            if term is not None:
                term.close()

    # ------------------------------------------------------------- terminal

    async def _on_term_open(self, conn: DeviceConnection, env) -> None:
        cols, rows = int(env.payload.get("cols", 80)), int(env.payload.get("rows", 24))
        term = self._terms.get(conn.fingerprint)
        if term is not None and term.alive and not env.payload.get("new"):
            term.resize(cols, rows)
            # Reopening the tab: show what happened meanwhile.
            await conn.send("control", "pc.term_output", {"data": term.scrollback.decode("utf-8", "replace"),
                                                         "replay": True})
            return
        if term is not None:
            term.close()

        async def send(type_: str, payload: dict) -> None:
            current = self._server.get_connection(conn.fingerprint)
            if current is not None:
                await current.send("control", type_, payload)

        self._terms[conn.fingerprint] = TerminalSession(self._loop, send, cols, rows)

    async def _on_term_input(self, conn: DeviceConnection, env) -> None:
        term = self._terms.get(conn.fingerprint)
        if term is not None:
            term.write(str(env.payload.get("data", "")))

    async def _on_term_resize(self, conn: DeviceConnection, env) -> None:
        term = self._terms.get(conn.fingerprint)
        if term is not None:
            term.resize(int(env.payload.get("cols", 80)), int(env.payload.get("rows", 24)))

    async def _on_term_close(self, conn: DeviceConnection, _env) -> None:
        term = self._terms.pop(conn.fingerprint, None)
        if term is not None:
            term.close()

    # ------------------------------------------------------------- info / ports / apps / files

    async def _on_info(self, conn: DeviceConnection, _env) -> None:
        await conn.send("control", "pc.info", await self._off_thread(pc_info.pc_info))

    async def _on_ports(self, conn: DeviceConnection, _env) -> None:
        ports, ifaces = await asyncio.gather(self._off_thread(pc_info.listening_ports),
                                             self._off_thread(pc_info.interfaces))
        await conn.send("control", "pc.ports", {"ports": ports, "interfaces": ifaces})

    async def _on_servers(self, conn: DeviceConnection, _env) -> None:
        await conn.send("control", "pc.servers", await self._off_thread(pc_info.servers))

    async def _on_server_action(self, conn: DeviceConnection, env) -> None:
        p = env.payload
        if p.get("kind") != "docker":
            await conn.send("control", "pc.status", {"message": "Only Docker containers can be started/stopped from here"})
            return
        ok, message = await self._off_thread(pc_info.docker_action, str(p.get("id", "")), str(p.get("action", "")))
        await conn.send("control", "pc.status", {"message": message, "ok": ok})
        await conn.send("control", "pc.servers", await self._off_thread(pc_info.servers))

    async def _on_apps(self, conn: DeviceConnection, _env) -> None:
        apps = await self._off_thread(pc_info.list_apps)
        await conn.send("control", "pc.apps", {"apps": apps})

    async def _on_app_launch(self, conn: DeviceConnection, env) -> None:
        path = pc_info.desktop_file_path(str(env.payload.get("id", "")))

        def launch() -> bool:
            info = Gio.DesktopAppInfo.new_from_filename(str(path)) if path else None
            try:
                ok = bool(info and info.launch([], None))
            except GLib.Error as exc:
                log.warning("launch failed: %s", exc)
                ok = False
            name = info.get_display_name() if info else env.payload.get("id")
            self._send_from_gtk(conn, "pc.status", {"message": f"Opened {name} on the PC" if ok
                                                    else f"Couldn't open {name}"})
            return False

        GLib.idle_add(launch)

    async def _on_files_list(self, conn: DeviceConnection, env) -> None:
        listing = await self._off_thread(pc_info.list_dir, str(env.payload.get("path", "/")))
        await conn.send("control", "pc.files", listing)

    async def _on_files_get(self, conn: DeviceConnection, env) -> None:
        path = pc_info.resolve_home_file(str(env.payload.get("path", "")))
        if path is None:
            await conn.send("control", "pc.status", {"message": "That file can't be sent"})
            return
        # Lands in the phone's Download/PhoneBridge (Files.receive on the phone).
        # In the background: handlers run inline on the connection's read loop, which must
        # stay free to read the phone's file.accept / file.reject.
        async def go() -> None:
            try:
                await file_transfer.send_file(conn, path, extra_offer_fields={"from_pc": True})
            except Exception as exc:  # noqa: BLE001
                await conn.send("control", "pc.status", {"message": f"Couldn't send {path.name}: {exc}"})

        spawn(go())

    async def _on_clipboard_get(self, conn: DeviceConnection, _env) -> None:
        def read() -> bool:
            self._send_from_gtk(conn, "clipboard.update", {"text": self._clipboard_text() or "", "requested": True})
            return False

        GLib.idle_add(read)

    # ------------------------------------------------------------- remote input / screen

    def _with_remote(self, conn: DeviceConnection, then) -> None:
        """Runs then(remote) on the GTK thread once the portal session is ready."""
        def go() -> bool:
            if self._remote is None:
                self._remote = RemoteDesktop()
            remote = self._remote
            if not remote.ready:
                self._send_from_gtk(conn, "pc.remote_status", {
                    "state": "waiting", "message": "On the laptop: turn ON \"Allow remote interaction\", then click "
                               "\"Share\" (first time only)"})

            def ready(error: str | None) -> None:
                if error:
                    self._send_from_gtk(conn, "pc.remote_status", {"state": "denied", "message": error})
                    return
                if remote.can_control:
                    self._send_from_gtk(conn, "pc.remote_status", {"state": "ready", "message": "Controlling the PC"})
                else:
                    # Viewing works, but touch/typing was not granted: forget this choice so
                    # the next Start shows the dialog again.
                    self._send_from_gtk(conn, "pc.remote_status", {
                        "state": "no_input",
                        "message": "Touch and typing are off: tap Stop, then Start, and in the laptop's dialog turn ON "
                                   "\"Allow remote interaction\" before clicking Share."})
                    self._needs_reask = True
                then(remote)

            remote.ensure(ready)
            return False

        GLib.idle_add(go)

    async def _on_input(self, conn: DeviceConnection, env) -> None:
        p = env.payload
        kind = p.get("kind")
        if self._remote is not None and self._remote.ready:
            GLib.idle_add(self._apply_input, self._remote, kind, p)
        else:
            self._with_remote(conn, lambda r: self._apply_input(r, kind, p))

    @staticmethod
    def _apply_input(remote: RemoteDesktop, kind: str, p: dict) -> bool:
        if kind == "move":
            remote.move(float(p.get("dx", 0)), float(p.get("dy", 0)))
        elif kind == "click":
            remote.click(_BUTTONS.get(p.get("button", "left"), BTN_LEFT))
        elif kind == "button":
            remote.button(_BUTTONS.get(p.get("button", "left"), BTN_LEFT), bool(p.get("down")))
        elif kind == "scroll":
            remote.scroll(int(p.get("dy", 0)), int(p.get("dx", 0)))
        elif kind == "tap_abs":
            remote.move_abs(float(p.get("x", 0)), float(p.get("y", 0)))
            remote.click(_BUTTONS.get(p.get("button", "left"), BTN_LEFT))
        elif kind == "abs":
            # Absolute pointer on the viewed window; optional press/release for drags.
            remote.move_abs(float(p.get("x", 0)), float(p.get("y", 0)))
            if "down" in p:
                remote.button(_BUTTONS.get(p.get("button", "left"), BTN_LEFT), bool(p["down"]))
        elif kind == "text":
            remote.type_text(str(p.get("text", "")))
        elif kind == "key":
            sym = KEYSYMS.get(str(p.get("key")))
            if sym is None and len(str(p.get("key", ""))) == 1:
                from gi.repository import Gdk
                sym = Gdk.unicode_to_keyval(ord(str(p["key"]).lower()))
            mods = [KEYSYMS[m] for m in p.get("mods", []) if m in KEYSYMS]
            if sym:
                remote.tap_key(sym, mods)
        return False

    async def _on_screen_start(self, conn: DeviceConnection, _env) -> None:
        def go() -> bool:
            if self._needs_reask and self._remote is not None:
                self._needs_reask = False
                self._stop_screen()
                self._remote.forget_choice()  # ask again (with remote interaction this time)
            self._with_remote(conn, lambda remote: self._show_stream(conn, remote, 0))
            return False

        GLib.idle_add(go)

    def _show_stream(self, conn: DeviceConnection, remote: RemoteDesktop, index: int) -> None:
        self._stop_screen()
        if not remote.streams:
            self._send_from_gtk(conn, "pc.remote_status", {"state": "denied", "message": "No window or screen was shared"})
            return
        remote.current = max(0, min(index, len(remote.streams) - 1))
        try:
            self._stream_conn = conn
            self._streamer = ScreenStreamer(remote, self._on_frame)
        except Exception as exc:  # noqa: BLE001
            log.warning("screen stream failed: %s", exc)
            self._send_from_gtk(conn, "pc.remote_status", {"state": "denied", "message": f"Screen capture failed: {exc}"})
            return
        stream = remote.stream
        self._send_from_gtk(conn, "pc.screen_started", {
            "index": remote.current, "count": len(remote.streams), "kind": stream["kind"],
            "width": self._streamer.size[0], "height": self._streamer.size[1]})

    async def _on_screen_stop(self, _conn: DeviceConnection, _env) -> None:
        GLib.idle_add(self._stop_screen)

    def _stop_screen(self) -> bool:
        if self._streamer is not None:
            self._streamer.stop()
            self._streamer = None
        self._stream_conn = None
        return False

    def _on_frame(self, jpeg: bytes) -> None:
        """GStreamer thread. Drops frames while the previous one is still being sent so a
        slow phone/Wi-Fi never builds up a backlog (it just gets a lower frame rate)."""
        conn = self._stream_conn
        if conn is None or self._frame_in_flight:
            return
        self._frame_in_flight = True

        async def send() -> None:
            try:
                async with conn.write_lock:
                    await conn.send_unlocked("control", "pc.screen_frame", {"size": len(jpeg)})
                    await conn.send_raw(jpeg)
            except Exception:  # noqa: BLE001 - connection gone; the disconnect stops the stream
                pass
            finally:
                self._frame_in_flight = False

        asyncio.run_coroutine_threadsafe(send(), self._loop)
