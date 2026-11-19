from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from . import ble_wake, file_transfer
from .net_thread import NetThread
from .pc_control import PcControl
from .transport import ControlChannelServer, DeviceConnection
from .protocol import Envelope
from .ui.main_window import MainWindow
from .ui.files_page import DOWNLOADS_DIR
from .ui.photos_page import PHOTOS_DIR

APP_ID = "dev.phonebridge.Linux"


class PhoneBridgeApp(Adw.Application):
    def __init__(self) -> None:
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.FLAGS_NONE)
        self.net_thread = NetThread()
        self.server = ControlChannelServer()
        self.connect("activate", self._on_activate)

    def _on_activate(self, app: Adw.Application) -> None:
        # Launching again while already running re-activates this instance: just raise the
        # existing window instead of re-starting the net thread / re-binding the port.
        if self.get_active_window() is not None:
            self.get_active_window().present()
            self.get_active_window()._sync.user_opened_viewer()   # noqa: SLF001 - launcher clicked again: still one session
            return
        self._load_css()
        self._register_icons()
        self._start_security_engine()
        self.net_thread.start()
        self.net_thread.run_coro(self.server.start()).result(timeout=10)
        self.net_thread.run_coro(self.server.serve_forever())

        win = MainWindow(app, self.server, self.net_thread)
        self._wire_server_events(win)
        # The phone app's "control this PC" tabs (mouse, keyboard, screen, files, ports…).
        self.pc_control = PcControl(self.server, self.net_thread.loop, clipboard_text=win.current_clipboard_text)
        self.pc_control.approval_callback = self._ask_pc_control

        show_page = Gio.SimpleAction.new("show-page", GLib.VariantType.new("s"))
        show_page.connect("activate", lambda _a, key: win.show_page_from_notification(key.get_string()))
        self.add_action(show_page)
        win.present()
        # Opening PhoneBridge on this PC is an explicit user launch (unless started by autostart: PHONEBRIDGE_ORIGIN=auto):
        # bring the phone side up and start mirroring. Re-activation by the launcher counts too (a no-op if already active).
        from .screensync.commands import Origin
        self._user_launch_origin = Origin.AUTO if os.environ.get("PHONEBRIDGE_ORIGIN") == "auto" else Origin.USER
        GLib.idle_add(lambda: (win._sync.user_opened_viewer(self._user_launch_origin), False)[1])  # noqa: SLF001
        ble_wake.start()  # BLE beacon: lets a scanning phone reconnect the moment this PC is up
        self.connect("shutdown", lambda *_: ble_wake.stop())

    def _start_security_engine(self) -> None:
        """Signed audit ledger + permission-change auditing. A ledger failure never stops the app."""
        from .security import keystore, ledger, permissions

        try:
            cfg = keystore.DEFAULT_CONFIG_DIR
            local = ledger.LocalLedger(cfg / "audit.jsonl", keystore.for_config_dir(cfg))
            self.server.ledger = self._maybe_anchor(local, cfg)
            verdict = self.server.ledger.verify()
            if not verdict.ok:
                logging.getLogger("phonebridge.security").error("audit ledger FAILED verification: %s (record %s)",
                                                                verdict.error, verdict.bad_index)
                self.server.audit(ledger.SECURITY_ERROR, None, error="ledger_verification_failed", at=verdict.bad_index)
        except Exception:  # noqa: BLE001
            logging.getLogger("phonebridge.security").exception("audit ledger unavailable; continuing without it")
        permissions.on_change.append(lambda dev, feat, mode: self.server.audit(
            ledger.PERMISSION, dev.fingerprint, feature=feat, mode=mode))
        self.server.ask_callback = self._ask_permission
        GLib.timeout_add_seconds(15, self._usb_tick)

    @staticmethod
    def _maybe_anchor(local, cfg):
        """Optional: anchor the ledger on a private chain you host. Configure with ~/.config/phonebridge/anchor.json, e.g.
        {"url": "http://nas.local:8480", "service_pub": "<key printed by the anchor service>"} or {"file": "/mnt/usb/anchors.jsonl"}.
        Absent or unreadable config means the plain local ledger; an unreachable service never affects the app."""
        import json

        try:
            conf = json.loads((cfg / "anchor.json").read_text())
        except (OSError, ValueError):
            return local
        from .security import anchor

        if conf.get("url"):
            return anchor.PrivateBlockchain(local, anchor.HttpAnchor(conf["url"], conf.get("service_pub")))
        if conf.get("file"):
            return anchor.PrivateBlockchain(local, anchor.FileAnchor(conf["file"]))
        return local

    def _usb_tick(self) -> bool:
        """Every 15 s: if adb sees an authorised phone, (re)create the USB tunnel to the control port."""
        import threading

        from . import usb_link

        def work() -> None:
            self.server.usb_serials = usb_link.ensure_reverse(self.server.port) if usb_link.adb_path() else []
        threading.Thread(target=work, daemon=True).start()
        return True

    async def _ask_permission(self, device, feature: str) -> bool:
        """'Ask' permissions: show a dialog on the GTK thread and wait for the answer (net loop side)."""
        import asyncio

        from .security.permissions import FEATURES

        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()

        def show() -> bool:
            win = self.get_active_window()
            dialog = Adw.MessageDialog(transient_for=win, heading=f"Allow {FEATURES[feature]}?",
                                       body=f"{device.name} wants to use “{FEATURES[feature]}” on this PC. "
                                            "Your answer lasts until it disconnects.")
            dialog.add_response("deny", "Deny")
            dialog.add_response("allow", "Allow")
            dialog.set_response_appearance("allow", Adw.ResponseAppearance.SUGGESTED)
            dialog.connect("response", lambda _d, r: loop.call_soon_threadsafe(
                lambda: fut.done() or fut.set_result(r == "allow")))
            dialog.present()
            return False

        GLib.idle_add(show)
        return await fut

    async def _ask_pc_control(self, device) -> int:
        """A phone asked for PC control while it is off: ask the person at the PC. Returns minutes granted (0 = refused).
        Never longer than one hour; the dialog defaults to Deny."""
        import asyncio

        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()

        def show() -> bool:
            dialog = Adw.MessageDialog(
                transient_for=self.get_active_window(), heading="Allow this phone to control your PC?",
                body=f"“{device.name}” is asking for remote control of this PC: a shell, your files, mouse and keyboard, "
                     "and a view of the screen.\n\nOnly allow it if you just asked for this. Control switches itself off "
                     "after the time you choose and can be revoked at any moment from the banner at the top of the window.")
            dialog.add_response("deny", "Deny")
            dialog.add_response("15", "Allow 15 minutes")
            dialog.add_response("60", "Allow 1 hour")
            dialog.set_default_response("deny")
            dialog.set_close_response("deny")
            dialog.set_response_appearance("60", Adw.ResponseAppearance.DESTRUCTIVE)
            dialog.connect("response", lambda _d, r: loop.call_soon_threadsafe(
                lambda: fut.done() or fut.set_result(int(r) if r in ("15", "60") else 0)))
            dialog.present()
            return False

        GLib.idle_add(show)
        return await fut

    def _load_css(self) -> None:
        # Dark like the phone app; style.css retunes Adwaita's dark palette to match it.
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.PREFER_DARK)
        css_path = Path(__file__).parent / "ui" / "style.css"
        provider = Gtk.CssProvider()
        provider.load_from_path(str(css_path))
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

    def _register_icons(self) -> None:
        """Some sidebar icon names (notification/chat-bubble) aren't in every system icon
        theme — bundle our own so they always render instead of showing nothing."""
        icons_dir = Path(__file__).parent / "ui" / "icons"
        Gtk.IconTheme.get_for_display(Gdk.Display.get_default()).add_search_path(str(icons_dir))

    def _wire_server_events(self, win: MainWindow) -> None:
        def on_connection(conn: DeviceConnection, connected: bool) -> None:
            peer = conn._writer.get_extra_info("peername")
            GLib.idle_add(win.on_connection_changed, conn.fingerprint, connected, peer[0] if peer else "", getattr(conn, "link", "lan"))

        self.server.on_connection_change(on_connection)

        def forward(type_: str):
            async def handler(conn: DeviceConnection, envelope: Envelope) -> None:
                GLib.idle_add(win.on_phone_message, conn.fingerprint, type_, envelope.payload)
            return handler

        for type_ in PHONE_MESSAGE_TYPES:
            self.server.on(type_, forward(type_))

        def dest_dir_for(payload: dict) -> Path:
            return PHOTOS_DIR if payload.get("photo_id") else DOWNLOADS_DIR

        def progress_for(payload: dict):
            last_pct = -1

            def on_progress(received: int, total: int) -> None:
                nonlocal last_pct
                pct = int(received * 100 / total) if total else 100
                if pct != last_pct:  # don't flood the GTK thread with one update per chunk
                    last_pct = pct
                    GLib.idle_add(win.on_receive_progress, payload, received, total)

            return on_progress

        def on_done(dest_path: Path, _size: int, payload: dict) -> None:
            GLib.idle_add(win.on_file_received, payload, dest_path)

        file_transfer.register(self.server, dest_dir_for, progress_for, on_done)


# Phone -> PC message types routed to MainWindow.on_phone_message (see PROTOCOL.md section 4).
PHONE_MESSAGE_TYPES = (
    "device.info",
    "notifications.all", "notifications.unavailable", "notification.posted", "notification.removed",
    "clipboard.update",
    "sms.thread_list", "sms.thread_messages", "sms.new", "sms.sent", "sms.mms_status",
    "calls.history", "contacts.list", "call.ringing", "call.state",
    "photos.list_result", "files.list_result", "file.saved",
    "apps.list",
    "hotspot.state",
    "video.started", "video.stopped", "mirror.status", "webcam.status",
    "screen.state", "volume.state",
    "load.progress", "device.unpair",
    "error",
)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")
    return PhoneBridgeApp().run(sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
