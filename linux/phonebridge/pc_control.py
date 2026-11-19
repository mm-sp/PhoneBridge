"""Answers the phone app's "control this PC" tabs (protocol/PROTOCOL.md section 4c):
PC info, open ports/network, apps, home-folder files, clipboard, and remote mouse /
keyboard / screen view through the RemoteDesktop portal (pc_remote.py).
"""
from __future__ import annotations

import asyncio
import logging
import time

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

from .netutil import spawn
from . import file_transfer, pc_info
from .pc_remote import BTN_LEFT, BTN_MIDDLE, BTN_RIGHT, KEYSYMS, RemoteDesktop, ScreenStreamer
from . import pairing
from .security import ledger as sec_ledger, permissions
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
        self._gate_cache: dict[str, tuple[tuple[int, int], float | None]] = {}
        # PC-side approval: an async callable (device) -> minutes granted (0 = refused). The GUI shows the prompt.
        # Without one (tests, headless) a phone's request simply stays refused.
        self.approval_callback = None
        self._approval_pending: set[str] = set()
        self._approval_cooldown: dict[str, float] = {}   # fingerprint -> time before which no new prompt is raised
        permissions.on_change.append(self._on_permission_change)

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
        loop.call_soon_threadsafe(loop.call_later, 5.0, self._sweep_expired)

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
            return hit[1] is not None and time.time() < hit[1]
        device = PairedDevice.load(fingerprint)
        until = device.pc_control_until if device and device.allow_pc_control else None
        self._gate_cache[fingerprint] = (stamp, until)
        return until is not None and time.time() < until

    # ------------------------------------------------------------- approval on the PC

    APPROVAL_TIMEOUT = 60       # an unanswered prompt counts as "refused"
    APPROVAL_COOLDOWN = 120     # after a refusal/timeout the same phone cannot raise another prompt for this long

    def _maybe_request_approval(self, conn: DeviceConnection) -> bool:
        """A phone asked for something while PC control is off: raise ONE prompt on the PC (if a GUI is attached).
        The phone can never switch control on itself; only the answer given on the PC does."""
        fp = conn.fingerprint
        if self.approval_callback is None or fp in self._approval_pending:
            return fp in self._approval_pending
        if time.monotonic() < self._approval_cooldown.get(fp, 0.0):
            return False
        self._approval_pending.add(fp)
        self._server.audit(sec_ledger.PERMISSION, fp, feature="remote_control", action="pc_control.requested")
        spawn(self._run_approval(conn))
        return True

    async def _run_approval(self, conn: DeviceConnection) -> None:
        fp = conn.fingerprint
        minutes = 0
        try:
            device = PairedDevice.load(fp)
            if device is not None:
                minutes = int(await asyncio.wait_for(self.approval_callback(device), self.APPROVAL_TIMEOUT) or 0)
        except Exception:  # noqa: BLE001 - timeout, dialog failure, bad answer: all mean "refused"
            minutes = 0
        finally:
            self._approval_pending.discard(fp)
        minutes = max(0, min(minutes, pairing.PC_CONTROL_MAX_SECONDS // 60))
        device = PairedDevice.load(fp)
        if minutes and device is not None:
            permissions.set_mode(device, "remote_control", permissions.ALLOW, duration=minutes * 60)
            self._gate_cache.pop(fp, None)
            self._server.audit(sec_ledger.PERMISSION, fp, feature="remote_control", action="pc_control.approved", minutes=minutes)
            message = {"message": f"PC control approved on the PC for {minutes} min", "ok": True}
        else:
            self._approval_cooldown[fp] = time.monotonic() + self.APPROVAL_COOLDOWN
            self._server.audit(sec_ledger.PERMISSION, fp, feature="remote_control", action="pc_control.refused")
            message = {"denied": True, "message": "PC control was not approved on the PC"}
        try:
            await conn.send("control", "pc.status", message)
        except Exception:  # noqa: BLE001 - the phone may have gone away
            pass

    def _on_permission_change(self, device, feature: str, mode: str) -> None:
        """Turning remote control off (Device page, Security page, the banner's Revoke) ends live sessions at once."""
        if feature == "remote_control" and mode == permissions.DENY:
            self._gate_cache.pop(device.fingerprint, None)
            # The owner just switched it off: the phone may not immediately raise a new prompt.
            self._approval_cooldown[device.fingerprint] = time.monotonic() + self.APPROVAL_COOLDOWN
            self._server.audit(sec_ledger.PERMISSION, device.fingerprint, feature="remote_control", action="pc_control.revoked")
            self._loop.call_soon_threadsafe(self._revoke_live_sessions, device.fingerprint)

    def _revoke_live_sessions(self, fingerprint: str) -> None:
        """Control was switched off or timed out: end the shell now (its output would otherwise keep flowing to the
        phone with no further gated message) and the screen view."""
        term = self._terms.pop(fingerprint, None)
        if term is not None:
            log.info("PC control no longer allowed: closing the phone's terminal")
            term.close()
        if self._stream_conn is not None and self._stream_conn.fingerprint == fingerprint:
            GLib.idle_add(self._stop_screen)

    def _sweep_expired(self) -> None:
        """Every few seconds: (1) any phone whose time limit ran out gets its switch cleared and an `expired` audit record,
        (2) shells/screen views of phones that are no longer allowed are closed, even if the phone is silent."""
        try:
            for d in PairedDevice.list_all():
                if d.allow_pc_control and not d.pc_control_active():
                    PairedDevice.update(d.fingerprint, allow_pc_control=False, pc_control_until=None)
                    self._gate_cache.pop(d.fingerprint, None)
                    self._server.audit(sec_ledger.PERMISSION, d.fingerprint, feature="remote_control", action="pc_control.expired")
        except Exception:  # noqa: BLE001 - housekeeping must never stop the sweep
            log.exception("expiry sweep failed")
        live = list(self._terms) + ([self._stream_conn.fingerprint] if self._stream_conn else [])
        for fp in live:
            if not self._pc_control_allowed(fp):
                self._revoke_live_sessions(fp)
        self._loop.call_later(5.0, self._sweep_expired)

    def _gated(self, handler):
        """Shell, files, input and screen view of this PC are off until the user enables
        "Allow this phone to control this PC" on the phone's card in the Device page."""

        async def wrapper(conn: DeviceConnection, env) -> None:
            if not self._pc_control_allowed(conn.fingerprint):
                self._revoke_live_sessions(conn.fingerprint)
                asking = self._maybe_request_approval(conn)
                await conn.send("control", "pc.status", {
                    "denied": True,
                    "message": ("PC control needs approval. Waiting for the PC's owner to approve on the PC…" if asking else
                                "PC control is turned off for this phone. It can only be enabled on the PC "
                                "(Device page → Allow PC control, or approve the prompt a request raises).")})
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
