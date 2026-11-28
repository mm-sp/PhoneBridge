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
        self._screen_request = 0  # only the newest screen request may start a stream; "stop" cancels a pending one
        self._portal_refused_at = float("-inf")
        self._gate_cache: dict[str, tuple[bytes, float | None]] = {}
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
        }.items():
            server.on(type_, self._gated(handler))
        # The terminal is unavailable. These types are deliberately NOT wrapped by `_gated`: `_gated` would raise a PC
        # approval prompt for a phone without PC control before any refusal could happen.
        for type_ in ("pc.term_open", "pc.term_input", "pc.term_resize", "pc.term_close"):
            server.on(type_, self._refuse_terminal)
        server.on_connection_change(self._on_connection_change)
        loop.call_soon_threadsafe(loop.call_later, 5.0, self._sweep_expired)

    # ------------------------------------------------------------- helpers

    def _pc_control_allowed(self, fingerprint: str) -> bool:
        """The per-phone switch, parsed again only when the device file's contents changed (one small read per message
        instead of a read + JSON parse + key unwrap), so turning it off takes effect on the very next message.
        The contents are compared, not the file's time and size: two writes within one filesystem timestamp tick can
        leave both identical."""
        try:
            stamp = (pairing.DEVICES_DIR / f"{fingerprint}.json").read_bytes()
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
            message = {"message": f"PC control approved on the PC for {minutes} min", "ok": True, "approved": True}
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
        """Control was switched off or timed out: end the screen view now."""
        if self._stream_conn is not None and self._stream_conn.fingerprint == fingerprint:
            GLib.idle_add(self._stop_screen)

    def _sweep_expired(self) -> None:
        """Every few seconds: (1) any phone whose time limit ran out gets its switch cleared and an `expired` audit record,
        (2) screen views of phones that are no longer allowed are closed, even if the phone is silent."""
        try:
            for d in PairedDevice.list_all():
                if d.allow_pc_control and not d.pc_control_active():
                    PairedDevice.update(d.fingerprint, allow_pc_control=False, pc_control_until=None)
                    self._gate_cache.pop(d.fingerprint, None)
                    self._server.audit(sec_ledger.PERMISSION, d.fingerprint, feature="remote_control", action="pc_control.expired")
        except Exception:  # noqa: BLE001 - housekeeping must never stop the sweep
            log.exception("expiry sweep failed")
        live = [self._stream_conn.fingerprint] if self._stream_conn else []
        for fp in live:
            if not self._pc_control_allowed(fp):
                self._revoke_live_sessions(fp)
        self._loop.call_later(5.0, self._sweep_expired)

    def _gated(self, handler):
        """Files, input and screen view of this PC are off until the user enables
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
        # A phone that reconnects before the PC noticed the old link was dead is only announced as "connected": the
        # screen capture feeding the old link must end then too.
        if not connected or (self._stream_conn is not None and self._stream_conn is not conn):
            GLib.idle_add(self._stop_screen)

    # ------------------------------------------------------------- terminal (unavailable)

    async def _refuse_terminal(self, conn: DeviceConnection, env) -> None:
        """Every terminal request is refused, in every grant state, before any approval workflow. Reads only `env.type`
        (never the payload), writes one `access.denied` record and sends one `pc.status`."""
        self._server.audit(sec_ledger.DENIED, conn.fingerprint, feature="terminal", message=env.type, mode="unavailable")
        try:
            await conn.send("control", "pc.status", {"denied": True, "message": "The terminal is not available"})
        except Exception:  # noqa: BLE001 - the phone may have gone away
            pass

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

    INPUT_MAX_AGE = 5.0         # input that waited longer than this for the PC's dialog is dropped, never replayed
    INPUT_REASK_COOLDOWN = 30   # after the dialog is refused, touch/typing alone cannot raise it again this soon

    def _still_wanted(self, conn: DeviceConnection) -> bool:
        """The PC's dialog can stay open for minutes: by the time it is answered the phone may have disconnected, or
        PC control may have been revoked or have expired."""
        return self._server.get_connection(conn.fingerprint) is conn and self._pc_control_allowed(conn.fingerprint)

    def _with_remote(self, conn: DeviceConnection, then, from_input: bool = False) -> None:
        """Runs then(remote) on the GTK thread once the portal session is ready, if the phone is still allowed then."""
        def go() -> bool:
            if self._remote is None:
                self._remote = RemoteDesktop()
            remote = self._remote
            quiet = from_input and remote.starting  # a stream of touch events must not repeat every status message
            if not remote.ready and not quiet:
                self._send_from_gtk(conn, "pc.remote_status", {
                    "state": "waiting", "message": "On the laptop: turn ON \"Allow remote interaction\", then click "
                               "\"Share\" (first time only)"})

            def ready(error: str | None) -> None:
                if error:
                    self._portal_refused_at = time.monotonic()
                    if not quiet:
                        self._send_from_gtk(conn, "pc.remote_status", {"state": "denied", "message": error})
                    return
                if not self._still_wanted(conn):
                    return
                if quiet:
                    then(remote)
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
            return
        asked = time.monotonic()
        if asked - self._portal_refused_at < self.INPUT_REASK_COOLDOWN:
            return

        def apply_if_fresh(remote) -> None:
            if time.monotonic() - asked <= self.INPUT_MAX_AGE:
                self._apply_input(remote, kind, p)

        self._with_remote(conn, apply_if_fresh, from_input=True)

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
            self._screen_request += 1
            request = self._screen_request

            def show(remote) -> None:
                if request == self._screen_request:
                    self._show_stream(conn, remote, 0)

            self._with_remote(conn, show)
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
            self._stream_conn = None
            log.warning("screen stream failed: %s", exc)
            self._send_from_gtk(conn, "pc.remote_status", {"state": "denied", "message": f"Screen capture failed: {exc}"})
            return
        stream = remote.stream
        self._send_from_gtk(conn, "pc.screen_started", {
            "index": remote.current, "count": len(remote.streams), "kind": stream["kind"],
            "width": self._streamer.size[0], "height": self._streamer.size[1]})

    async def _on_screen_stop(self, _conn: DeviceConnection, _env) -> None:
        GLib.idle_add(self._cancel_screen)

    def _cancel_screen(self) -> bool:
        """The phone asked to stop: end the stream, and don't start one that is still waiting for the PC's dialog."""
        self._screen_request += 1
        return self._stop_screen()

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
