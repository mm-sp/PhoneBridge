"""Remote control of this PC from the phone: mouse, keyboard and screen view.

On Wayland an app can neither inject input nor capture the screen by itself, so this
goes through xdg-desktop-portal's RemoteDesktop portal with a ScreenCast stream in the
same session. The first time, GNOME asks the user on the PC to allow it; the portal's
restore token is saved so later sessions start without asking again.

Everything here runs on the GTK/GLib main thread (portal replies arrive as D-Bus
signals there); app.py hops onto it with GLib.idle_add.
"""
from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path

import gi

gi.require_version("Gst", "1.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gio, GLib, Gst  # noqa: E402

log = logging.getLogger("phonebridge.remote")

PORTAL = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
RD = "org.freedesktop.portal.RemoteDesktop"
SC = "org.freedesktop.portal.ScreenCast"
TOKEN_PATH = Path.home() / ".config" / "phonebridge" / "remote_desktop_token"

BTN_LEFT, BTN_RIGHT, BTN_MIDDLE = 0x110, 0x111, 0x112

# Named keys the phone's Keyboard tab can send.
KEYSYMS = {
    "Escape": Gdk.KEY_Escape, "Tab": Gdk.KEY_Tab, "Return": Gdk.KEY_Return, "BackSpace": Gdk.KEY_BackSpace,
    "Delete": Gdk.KEY_Delete, "Left": Gdk.KEY_Left, "Right": Gdk.KEY_Right, "Up": Gdk.KEY_Up, "Down": Gdk.KEY_Down,
    "Home": Gdk.KEY_Home, "End": Gdk.KEY_End, "Page_Up": Gdk.KEY_Page_Up, "Page_Down": Gdk.KEY_Page_Down,
    "Control_L": Gdk.KEY_Control_L, "Alt_L": Gdk.KEY_Alt_L, "Shift_L": Gdk.KEY_Shift_L, "Super_L": Gdk.KEY_Super_L,
    "space": Gdk.KEY_space, "Print": Gdk.KEY_Print,
    **{f"F{i}": getattr(Gdk, f"KEY_F{i}") for i in range(1, 13)},
    "AudioRaiseVolume": Gdk.KEY_AudioRaiseVolume, "AudioLowerVolume": Gdk.KEY_AudioLowerVolume,
    "AudioMute": Gdk.KEY_AudioMute,
}


class RemoteDesktop:
    """One portal session (pointer + keyboard + one monitor stream)."""

    def __init__(self) -> None:
        self._bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self._sender = self._bus.get_unique_name()[1:].replace(".", "_")
        self.session: str | None = None
        # One entry per window/monitor the user shared: {"node", "size", "kind"}.
        self.streams: list[dict] = []
        self.current = 0  # which stream the phone is looking at / tapping on
        # What GNOME actually granted: bit 1 keyboard, bit 2 pointer. The user can leave
        # "Allow remote interaction" off in the dialog, which grants viewing only.
        self.devices = 0
        self._starting = False
        self._attempt = 0  # portal replies belonging to an abandoned attempt are ignored
        self._waiters: list = []
        self._closed_sub = 0

    @property
    def ready(self) -> bool:
        """True only once the session has actually started. A session handle exists from the first step, while the
        PC's owner may still be looking at (or about to refuse) the dialog."""
        return self.session is not None and not self._starting

    @property
    def starting(self) -> bool:
        return self._starting

    # ------------------------------------------------------------- session setup

    def ensure(self, done) -> None:
        """done(error_or_None) once the session has started (asks the user on the PC the first time)."""
        if self.ready:
            done(None)
            return
        self._waiters.append(done)
        if self._starting:
            return
        self._starting = True
        self._attempt += 1
        token = secrets.token_hex(8)
        self._request(RD, "CreateSession", lambda h: GLib.Variant("(a{sv})", ({
            "handle_token": GLib.Variant("s", h), "session_handle_token": GLib.Variant("s", "pb" + token)},)),
            self._on_created)

    def _finish(self, error: str | None) -> None:
        self._starting = False
        if error:
            self.close()  # a refused or half-made session must not look usable; the next request starts afresh
        waiters, self._waiters = self._waiters, []
        for w in waiters:
            w(error)

    def _request(self, iface: str, method: str, make_args, on_result) -> None:
        """Calls a portal method that answers via a Request object's Response signal."""
        handle = "h" + secrets.token_hex(6)
        path = f"/org/freedesktop/portal/desktop/request/{self._sender}/{handle}"
        sub = 0
        attempt = self._attempt

        def on_response(_conn, _sender, _path, _iface, _signal, params) -> None:
            self._bus.signal_unsubscribe(sub)
            if attempt != self._attempt:
                return
            code, results = params.unpack()
            if code != 0:
                self._finish("Remote control was not allowed on the PC" if code == 1 else f"portal error {code}")
                return
            on_result(results)

        sub = self._bus.signal_subscribe(PORTAL, "org.freedesktop.portal.Request", "Response", path, None,
                                         Gio.DBusSignalFlags.NO_MATCH_RULE, on_response)

        def on_called(bus, res) -> None:
            try:
                bus.call_finish(res)
            except GLib.Error as exc:
                self._bus.signal_unsubscribe(sub)
                if attempt == self._attempt:
                    self._finish(f"portal call {method} failed: {exc.message}")

        self._bus.call(PORTAL, PORTAL_PATH, iface, method, make_args(handle), None,
                       Gio.DBusCallFlags.NONE, -1, None, on_called)

    def _on_created(self, results: dict) -> None:
        self.session = results["session_handle"]
        self._closed_sub = self._bus.signal_subscribe(
            PORTAL, "org.freedesktop.portal.Session", "Closed", self.session, None,
            Gio.DBusSignalFlags.NONE, lambda *a: self._on_closed())
        opts = {"types": GLib.Variant("u", 3), "persist_mode": GLib.Variant("u", 2)}
        try:
            saved = TOKEN_PATH.read_text().strip()
            if saved:
                opts["restore_token"] = GLib.Variant("s", saved)
        except OSError:
            pass
        session = self.session
        self._request(RD, "SelectDevices", lambda h: GLib.Variant("(oa{sv})", (session, {
            "handle_token": GLib.Variant("s", h), **opts})), self._on_devices)

    def _on_devices(self, _results: dict) -> None:
        session = self.session
        self._request(SC, "SelectSources", lambda h: GLib.Variant("(oa{sv})", (session, {
            # The whole screen (one monitor).
            "handle_token": GLib.Variant("s", h), "types": GLib.Variant("u", 1),
            "multiple": GLib.Variant("b", False), "cursor_mode": GLib.Variant("u", 2)})), self._on_sources)

    def _on_sources(self, _results: dict) -> None:
        session = self.session
        self._request(RD, "Start", lambda h: GLib.Variant("(osa{sv})", (session, "", {
            "handle_token": GLib.Variant("s", h)})), self._on_started)

    def _on_started(self, results: dict) -> None:
        token = results.get("restore_token")
        if token:
            try:
                TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
                with open(os.open(TOKEN_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
                    os.fchmod(f.fileno(), 0o600)  # a file left by an older version may be more open
                    f.write(token)
            except OSError as exc:  # the dialog will simply be shown again next time
                log.warning("could not save the remote desktop permission: %s", exc)
        self.streams = [{"node": node, "size": tuple(props.get("size", (0, 0))),
                         "kind": "window" if props.get("source_type") == 2 else "screen"}
                        for node, props in (results.get("streams") or [])]
        self.current = 0
        self.devices = int(results.get("devices", 0))
        log.info("remote desktop session ready (devices %d, %d streams: %s)", self.devices, len(self.streams), self.streams)
        self._finish(None)

    def _on_closed(self) -> None:
        log.info("remote desktop session closed")
        if self._closed_sub:
            self._bus.signal_unsubscribe(self._closed_sub)
            self._closed_sub = 0
        self.session = None
        self.streams = []
        self.devices = 0
        self._attempt += 1
        if self._starting:  # closed before it started: no further reply will come, so answer the waiters now
            self._finish("Remote control was closed on the PC")

    def forget_choice(self) -> None:
        """Drop the remembered selection so the next session asks which windows to share."""
        self.close()
        TOKEN_PATH.unlink(missing_ok=True)

    @property
    def can_control(self) -> bool:
        return bool(self.devices & 2) and bool(self.devices & 1)

    @property
    def stream(self) -> dict | None:
        return self.streams[self.current] if 0 <= self.current < len(self.streams) else None

    def close(self) -> None:
        if self.session:
            self._bus.call(PORTAL, self.session, "org.freedesktop.portal.Session", "Close", None, None,
                           Gio.DBusCallFlags.NONE, -1, None, None)
            self._on_closed()

    def pipewire_fd(self) -> int:
        reply, fds = self._bus.call_with_unix_fd_list_sync(
            PORTAL, PORTAL_PATH, SC, "OpenPipeWireRemote", GLib.Variant("(oa{sv})", (self.session, {})),
            GLib.VariantType("(h)"), Gio.DBusCallFlags.NONE, -1, None, None)
        return fds.get(reply.unpack()[0])

    # ------------------------------------------------------------- input

    def _notify(self, method: str, args: GLib.Variant) -> None:
        if not self.session:
            log.info("%s dropped: no remote desktop session", method)
            return

        def done(bus, res) -> None:
            try:
                bus.call_finish(res)
            except GLib.Error as exc:
                log.warning("%s failed: %s", method, exc.message)

        self._bus.call(PORTAL, PORTAL_PATH, RD, method, args, None, Gio.DBusCallFlags.NONE, -1, None, done)

    def move(self, dx: float, dy: float) -> None:
        self._notify("NotifyPointerMotion", GLib.Variant("(oa{sv}dd)", (self.session, {}, dx, dy)))

    def move_abs(self, fx: float, fy: float, size: tuple[int, int] | None = None) -> None:
        """Pointer to a fraction of the currently viewed window/monitor."""
        s = self.stream
        if s is None:
            return
        w, h = size or s["size"]
        if not w:
            return
        self._notify("NotifyPointerMotionAbsolute", GLib.Variant(
            "(oa{sv}udd)", (self.session, {}, s["node"], fx * w, fy * h)))

    def button(self, button: int, pressed: bool) -> None:
        self._notify("NotifyPointerButton", GLib.Variant("(oa{sv}iu)", (self.session, {}, button, 1 if pressed else 0)))

    def click(self, button: int) -> None:
        self.button(button, True)
        self.button(button, False)

    def scroll(self, steps_y: int, steps_x: int = 0) -> None:
        if steps_y:
            self._notify("NotifyPointerAxisDiscrete", GLib.Variant("(oa{sv}ui)", (self.session, {}, 0, steps_y)))
        if steps_x:
            self._notify("NotifyPointerAxisDiscrete", GLib.Variant("(oa{sv}ui)", (self.session, {}, 1, steps_x)))

    def keysym(self, sym: int, pressed: bool) -> None:
        self._notify("NotifyKeyboardKeysym", GLib.Variant("(oa{sv}iu)", (self.session, {}, sym, 1 if pressed else 0)))

    def tap_key(self, sym: int, modifiers: list[int] | None = None) -> None:
        for m in modifiers or []:
            self.keysym(m, True)
        self.keysym(sym, True)
        self.keysym(sym, False)
        for m in reversed(modifiers or []):
            self.keysym(m, False)

    def type_text(self, text: str) -> None:
        for ch in text:
            if ch == "\n":
                self.tap_key(Gdk.KEY_Return)
                continue
            sym = Gdk.unicode_to_keyval(ord(ch))
            if sym:
                self.tap_key(sym)


class ScreenStreamer:
    """PipeWire monitor stream -> scaled JPEG frames at a few fps (cheap to decode on the
    phone, no H.264 decoder needed there)."""

    def __init__(self, remote: RemoteDesktop, on_frame, max_width: int = 1280, fps: int = 8, quality: int = 55) -> None:
        if not Gst.is_initialized():
            Gst.init(None)
        stream = remote.stream
        fd = remote.pipewire_fd()  # a fresh PipeWire connection per pipeline
        self._on_frame = on_frame
        # Windows change size, so keep the aspect ratio and only cap the width.
        self._pipeline = Gst.parse_launch(
            f"pipewiresrc fd={fd} path={stream['node']} do-timestamp=true keepalive-time=500 ! "
            f"videoconvert ! videoscale ! video/x-raw,width=[16,{max_width}],pixel-aspect-ratio=1/1 ! "
            f"videorate drop-only=true ! video/x-raw,framerate={fps}/1 ! jpegenc quality={quality} ! "
            "appsink name=sink emit-signals=true max-buffers=1 drop=true sync=false"
        )
        w, h = stream["size"] or (1920, 1080)
        scale = min(1.0, max_width / w) if w else 1.0
        self.size = (int(w * scale), int(h * scale))
        self._pipeline.get_by_name("sink").connect("new-sample", self._on_sample)
        self._pipeline.set_state(Gst.State.PLAYING)

    def _on_sample(self, sink) -> Gst.FlowReturn:
        sample = sink.emit("pull-sample")
        if sample is not None:
            buf = sample.get_buffer()
            ok, info = buf.map(Gst.MapFlags.READ)
            if ok:
                try:
                    self._on_frame(bytes(info.data))
                finally:
                    buf.unmap(info)
        return Gst.FlowReturn.OK

    def stop(self) -> None:
        if self._pipeline is not None:
            self._pipeline.set_state(Gst.State.NULL)
            self._pipeline = None
