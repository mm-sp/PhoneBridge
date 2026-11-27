"""Main window: left nav rail (Phone Link-style sections) + content pane.

All phone traffic arrives in on_phone_message() (posted to the GTK thread by app.py)
and is routed to the page that shows it; pages call back into the _request_*/_on_*
methods here, which send to the connected phone.
"""
from __future__ import annotations

import re
import time
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from .. import pairing
from ..screensync import glue as sync_glue, settings as sync_settings
from ..security import permissions
from .apps_page import AppsPage
from .calls_page import CallsPage
from .clipboard_page import ClipboardBridge, ClipboardPage
from .device_page import DevicePage
from .files_page import FilesPage
from .messages_page import MessagesPage
from .mirror_page import MirrorPage
from .notifications_page import NotificationsPage
from .pairing_window import PairingWindow
from .photos_page import PhotosPage
from .security_page import SecurityPage
from .sync_page import SyncPage
from .webcam_page import WebcamPage

SECTIONS = [
    ("device", "Device", "phone-symbolic"),
    ("notifications", "Notifications", "preferences-system-notifications-symbolic|notification-symbolic"),
    ("messages", "Messages", "chat-symbolic|chat-message-new-symbolic|chat-bubble-text-symbolic"),
    ("calls", "Calls", "call-start-symbolic"),
    ("photos", "Photos", "image-x-generic-symbolic"),
    ("files", "Files", "folder-symbolic"),
    ("mirror", "Screen mirror", "video-display-symbolic"),
    ("sync", "Mirror dashboard", "view-reveal-symbolic|preferences-system-symbolic"),
    ("camera", "Camera", "camera-web-symbolic"),
    ("apps", "Apps", "view-grid-symbolic"),
    ("security", "Security", "security-high-symbolic|channel-secure-symbolic"),
]


# The phone's own names for its permissions (PcAccess.FEATURES in the Android app), so the user can find the switch.
PHONE_PERMISSIONS = {
    "files": "Files", "photos": "Photos and videos", "notifications": "Notifications", "sms": "Messages (read)",
    "sms_send": "Sending messages", "calls": "Calls and contacts", "call_dial": "Placing calls",
    "clipboard": "Clipboard sync", "screen": "Screen mirroring and camera",
    "remote_control": "Remote control of this phone", "apps": "Apps",
}
# Refused requests the PC makes on its own: nothing to tell the user.
_BACKGROUND_REQUESTS = {"screen.state_request"}
# One reminder per permission in this time: clipboard updates and touch input are sent far more often.
DENIAL_TOAST_SECONDS = 30

# Page sizes: tabs load this many first, then "Load more" fetches the next page.
SMS_PAGE = 20
CALLS_PAGE = 20
CONTACTS_PAGE = 50

# Requests whose answer can take a while, and the progress bar ("what") each one drives.
_LOAD_REQUESTS = {
    "sms.list_threads": ("messages", "sms.threads"),
    "sms.get_thread": ("messages", "sms.thread"),
    "calls.history_request": ("calls", "calls.history"),
    "contacts.list_request": ("calls", "contacts"),
    "files.list": ("files", "files"),
    "photos.list": ("photos", "photos"),
    "apps.list_request": ("apps", "apps"),
}
_LOAD_PAGES = {what: page for page, what in _LOAD_REQUESTS.values()}
_LOAD_RESPONSES = {
    "sms.thread_list": "sms.threads",
    "sms.thread_messages": "sms.thread",
    "calls.history": "calls.history",
    "contacts.list": "contacts",
    "files.list_result": "files",
    "photos.list_result": "photos",
    "apps.list": "apps",
}
# Request -> the message (type, telling payload fields) the phone answers with, as this PC's permissions judge it.
_ANSWER_TO = {request: (answer, {}) for request, (_page, what) in _LOAD_REQUESTS.items()
              for answer, answered in _LOAD_RESPONSES.items() if answered == what}
_ANSWER_TO |= {"notifications.sync": ("notifications.all", {}), "files.get": ("file.offer", {}),
               "photos.get": ("file.offer", {"photo_id": "any"})}


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application, server, net_thread) -> None:
        super().__init__(application=app, default_width=1100, default_height=720, title="PhoneBridge")
        self._app = app
        self._server = server
        self._net_thread = net_thread
        self._last_clip_text: str | None = None
        self._connected: dict[str, str] = {}  # fingerprint -> phone address
        self._active_fp: str | None = None  # phone the tabs show/control
        self._updating_dropdown = False
        self._current_key = "device"
        self._denial_toasted: dict[str, float] = {}  # phone permission -> when its refusal was last shown

        self._device_page = DevicePage(on_pair=self._open_pairing, on_forget=self._forget_device,
                                       on_control=self._set_active_phone,
                                       on_disconnect=lambda fp: self._disconnect_phone(fp),
                                       on_hotspot=lambda action: self._send("hotspot.request", {"action": action}),
                                       on_mount=self._mount_phone, on_relay=self._configure_relay)
        self._mounts: dict[str, object] = {}
        self._notifications_page = NotificationsPage(
            on_action=self._on_notification_action,
            on_refresh=lambda: self._send("notifications.sync", {}),
        )
        self._messages_page = MessagesPage(
            on_request_threads=lambda offset: self._send("sms.list_threads", {"offset": offset, "limit": SMS_PAGE}),
            on_request_thread=lambda tid, offset: self._send(
                "sms.get_thread", {"thread_id": tid, "offset": offset, "limit": SMS_PAGE}),
            on_send=lambda number, text: self._send("sms.send", {"number": number, "text": text}),
            on_send_mms=lambda number, text, attachments, request_id: self._send(
                "sms.send_mms", {"request_id": request_id,
                                 "numbers": [n.strip() for n in re.split(r"[,;]", number) if n.strip()], "text": text,
                                 "attachments": attachments}),
        )
        self._calls_page = CallsPage(
            on_action=lambda action: self._send("call.action", {"action": action}),
            on_dial=lambda number: self._send("call.dial", {"number": number}),
            on_message=self._compose_message,
            on_request_history=lambda offset: self._send("calls.history_request", {"offset": offset, "limit": CALLS_PAGE}),
            on_request_contacts=lambda offset: self._send("contacts.list_request",
                                                          {"offset": offset, "limit": CONTACTS_PAGE}),
        )
        self._photos_page = PhotosPage(
            on_request_list=lambda offset, limit: self._send("photos.list", {"offset": offset, "limit": limit}),
            on_request_photo=lambda photo_id: self._send("photos.get", {"id": photo_id}),
        )
        self._files_page = FilesPage(
            server, net_thread,
            on_list=lambda path: self._send("files.list", {"path": path}),
            on_get=lambda path: self._send("files.get", {"path": path}),
        )
        self._mirror_page = MirrorPage(on_start=self._on_mirror_start, on_stop=self._on_mirror_stop,
                                       on_input=lambda p: self._send("mirror.input", p),
                                       on_send_clipboard=self._send_clipboard_to_phone,
                                       on_copy_to_pc=self.copy_to_pc,
                                       pc_clipboard=lambda: self._last_clip_text,
                                       transport_of=self._video_transport)
        self._webcam_page = WebcamPage(on_start=self._on_webcam_start, on_stop=self._on_webcam_stop,
                                       on_switch=lambda facing: self._send("webcam.switch", {"facing": facing}),
                                       transport_of=self._video_transport)
        # No Clipboard tab any more: this stays as the background sync + history keeper
        # (the Screen mirror tab shows the latest clip and paste/send buttons).
        self._clipboard_page = ClipboardPage(on_send_to_phone=self._send_clipboard_to_phone)
        self._apps_page = AppsPage(
            on_request_list=lambda: self._send("apps.list_request", {}),
            on_launch=lambda pkg: self._send("apps.launch", {"pkg": pkg}),
            on_launch_and_mirror=self._launch_and_mirror,
            on_stream_app=self._stream_app,
        )

        # Bidirectional app-launch sync + smart display-off (screensync package; docs/SCREEN_SYNC.md).
        self._sync = sync_glue.build(self, server, net_thread)
        self._sync_page = SyncPage(self._sync, on_save_settings=sync_settings.save,
                                   probe_display=self._sync.probe_display)
        self._sync_refresh_queued = False

        self._security_page = SecurityPage(server, net_thread, on_toast=self.toast,
                                           on_revoked=lambda fp: self._device_page.remove_device(fp))
        self._pages: dict[str, Gtk.Widget] = {
            "device": self._device_page,
            "notifications": self._notifications_page,
            "messages": self._messages_page,
            "calls": self._calls_page,
            "photos": self._photos_page,
            "files": self._files_page,
            "mirror": self._mirror_page,
            "sync": self._sync_page,
            "camera": self._webcam_page,
            "apps": self._apps_page,
            "security": self._security_page,
        }

        self._stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        sidebar_list = Gtk.ListBox(css_classes=["navigation-sidebar"])
        for key, title, icon_name in SECTIONS:
            row = Adw.ActionRow(title=title)
            # Themed symbolic icons (with fallbacks) so they follow the light/dark text colour.
            row.add_prefix(Gtk.Image.new_from_gicon(Gio.ThemedIcon.new_from_names(icon_name.split("|"))))
            row.set_activatable(True)
            sidebar_list.append(row)
            self._stack.add_named(self._pages[key], key)
        sidebar_list.connect("row-activated", self._on_row_activated)
        sidebar_list.select_row(sidebar_list.get_row_at_index(0))
        self._sidebar_list = sidebar_list
        self._stack.set_visible_child_name("device")

        split_view = Adw.NavigationSplitView()
        sidebar_toolbar = Adw.ToolbarView()
        sidebar_header = Adw.HeaderBar(title_widget=Gtk.Label(label="PhoneBridge", css_classes=["heading"]))
        sidebar_toolbar.add_top_bar(sidebar_header)
        sidebar_toolbar.set_content(sidebar_list)
        bottom = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, margin_bottom=10, margin_start=10,
                         margin_end=10)
        status_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, css_classes=["status-card"])
        self._link_name = Gtk.Label(xalign=0, css_classes=["heading"], ellipsize=3)
        self._link_label = Gtk.Label(xalign=0, css_classes=["caption", "dim-label"], wrap=True)
        status_card.append(self._link_name)
        status_card.append(self._link_label)
        bottom.append(status_card)
        self._battery_text = ""
        # Which phone the tabs show/control when more than one is connected.
        self._phone_model = Gtk.StringList()
        self._phone_fps: list[str] = []
        self._phone_dropdown = Gtk.DropDown(model=self._phone_model, visible=False,
                                            tooltip_text="Phone the tabs show and control")
        self._phone_dropdown.connect("notify::selected", self._on_phone_selected)
        bottom.append(self._phone_dropdown)
        sidebar_toolbar.add_bottom_bar(bottom)
        split_view.set_sidebar(Adw.NavigationPage(title="PhoneBridge", child=sidebar_toolbar))

        content_toolbar = Adw.ToolbarView()
        self._content_title = Adw.WindowTitle(title="Device")
        content_toolbar.add_top_bar(Adw.HeaderBar(title_widget=self._content_title))
        self._banner = Adw.Banner(title="Phone not connected — open PhoneBridge on your phone (same Wi-Fi)",
                                  button_label="Pair a device", revealed=True)
        self._banner.connect("button-clicked", lambda _b: self._open_pairing())
        content_toolbar.add_top_bar(self._banner)
        # Shown whenever a phone currently has remote control of this PC; "Revoke now" ends it immediately.
        self._control_banner = Adw.Banner(title="", button_label="Revoke now", revealed=False)
        self._control_banner.add_css_class("error")
        self._control_banner.connect("button-clicked", lambda _b: self._revoke_pc_control())
        content_toolbar.add_top_bar(self._control_banner)
        GLib.timeout_add_seconds(2, self._refresh_control_banner)
        self._toasts = Adw.ToastOverlay()
        self._toasts.set_child(self._stack)
        content_toolbar.set_content(self._toasts)
        split_view.set_content(Adw.NavigationPage(title="Content", child=content_toolbar))

        self.set_content(split_view)
        self._clip_bridge = ClipboardBridge(self, self._on_local_clipboard_changed, self._on_local_image_changed)
        self.connect("close-request", self._on_close)
        self._update_link_label()

    # ---------------------------------------------------------------- PC control indicator

    def _refresh_control_banner(self) -> bool:
        import time

        devices = pairing.PairedDevice.list_all()
        page = getattr(self, "_device_page", None)
        if page is not None:
            page.sync_pc_control(devices)   # the cards' checkboxes follow the same stored state as the banner
        active = [d for d in devices if d.pc_control_active()]
        if active:
            left = max(1, int((max(d.pc_control_until for d in active) - time.time()) // 60) + 1)
            names = ", ".join(f"“{d.name}”" for d in active)
            self._control_banner.set_title(f"{names} can control this PC — switches off in {left} min")
        self._control_banner.set_revealed(bool(active))
        return True

    def _revoke_pc_control(self) -> None:
        for d in pairing.PairedDevice.list_all():
            if d.allow_pc_control:
                permissions.set_mode(d, "remote_control", permissions.DENY)
        self._refresh_control_banner()
        self.toast("Remote control revoked")

    # ---------------------------------------------------------------- navigation

    def _on_row_activated(self, _listbox: Gtk.ListBox, row: Adw.ActionRow) -> None:
        key, title, _icon = SECTIONS[row.get_index()]
        self._show_page(key)

    def _show_page(self, key: str) -> None:
        self._current_key = key
        self._stack.set_visible_child_name(key)
        title = next(t for k, t, _i in SECTIONS if k == key)
        self._content_title.set_title(title)
        page = self._pages[key]
        # Pages that show this PC's own state (works_offline) are brought up to date with or without a phone.
        if hasattr(page, "on_shown") and (self._connected or getattr(page, "works_offline", False)):
            page.on_shown()

    def _open_pairing(self) -> None:
        connected = self._fingerprint()
        if connected is not None:
            # One phone at a time: a new phone couldn't connect while this one is.
            name = self._phone_name(connected)
            dialog = Adw.MessageDialog(transient_for=self, heading=f"{name} is connected",
                                       body=f"Only one phone can be connected at a time. Disconnect {name} "
                                            "to pair another phone.")
            dialog.add_response("cancel", "Cancel")
            dialog.add_response("disconnect", f"Disconnect {name}")
            dialog.set_response_appearance("disconnect", Adw.ResponseAppearance.SUGGESTED)

            def on_response(_d, response: str) -> None:
                if response == "disconnect":
                    self._disconnect_phone(connected)
                    GLib.timeout_add(600, lambda: (self._open_pairing(), False)[1])

            dialog.connect("response", on_response)
            dialog.present()
            return
        dialog = PairingWindow(self, on_paired=self._on_device_paired, server=self._server)
        dialog.present()

    def _disconnect_phone(self, fingerprint: str) -> None:
        """Ask the phone to stay disconnected (until "Connect" is tapped on it), so another
        paired phone can take over."""
        conn = self._server.get_connection(fingerprint)
        if conn is None:
            return

        async def go() -> None:
            try:
                await conn.send("control", "link.disconnect", {})
            finally:
                conn.close()

        self._net_thread.run_coro(go())
        self.toast(f"Disconnected {self._phone_name(fingerprint)}")

    def _on_device_paired(self, device: pairing.PairedDevice) -> None:
        self._device_page.sync_with_disk()
        self._device_page.add_device(device)
        from ..security import ledger as sec_ledger

        self._server.audit(sec_ledger.PAIRED, device.fingerprint, platform=device.platform)
        self.toast(f"Paired with {device.name}")

    def _forget_device(self, device: pairing.PairedDevice) -> None:
        dialog = Adw.MessageDialog(transient_for=self, heading=f"Forget {device.name}?",
                                   body="It will have to be paired again to reconnect.")
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("forget", "Forget")
        dialog.set_response_appearance("forget", Adw.ResponseAppearance.DESTRUCTIVE)

        def on_response(_d, response: str) -> None:
            if response != "forget":
                return
            # revoke(): deletes the pairing + key, drops a live connection and writes the ledger entry.
            self._net_thread.run_coro(self._server.revoke(device.fingerprint, "forgotten_on_pc"))
            self._device_page.remove_device(device.fingerprint)

        dialog.connect("response", on_response)
        dialog.present()

    def toast(self, message: str) -> None:
        self._toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(message), timeout=4))

    def _mount_phone(self, fingerprint: str, action: str) -> None:
        """Mount / unmount the phone's storage as a read-only folder (phone_fs.py, needs fusepy)."""
        import threading

        from .. import phone_fs

        mount = self._mounts.get(fingerprint)
        if action == "unmount":
            if mount is not None:
                threading.Thread(target=mount.unmount, daemon=True).start()
                self.toast("Phone folder unmounted")
            return
        if not self._server.is_connected(fingerprint):
            self.toast("Connect the phone first")
            return
        if mount is None:
            mount = self._mounts[fingerprint] = phone_fs.Mount(
                self._server, self._net_thread.loop, fingerprint, self._phone_name(fingerprint))

        def work() -> None:
            error = mount.mount()
            if error:
                GLib.idle_add(self.toast, error)
            else:
                GLib.idle_add(lambda: (Gio.AppInfo.launch_default_for_uri(f"file://{mount.path}", None), False)[1])

        threading.Thread(target=work, daemon=True).start()

    def _on_close(self, *_args) -> bool:
        self._sync.shutdown()   # never leave the phone's display off
        for mount in self._mounts.values():
            mount.unmount()
        self._mirror_page.stop_if_running()
        self._webcam_page.stop_if_running()
        self._clip_bridge.stop()
        return False

    # ---------------------------------------------------------------- sending

    def _fingerprint(self) -> str | None:
        """The phone the tabs control (falls back to any connected phone)."""
        connected = self._server.connected_fingerprints()
        if self._active_fp in connected:
            return self._active_fp
        return connected[0] if connected else None

    def _phone_name(self, fingerprint: str) -> str:
        device = pairing.PairedDevice.load(fingerprint)
        return device.name if device else fingerprint

    def _send(self, type_: str, payload: dict) -> bool:
        fingerprint = self._fingerprint()
        if fingerprint is None:
            self.toast("Phone not connected")
            return False
        blocked = self._blocked_here(fingerprint, type_)
        if blocked:
            # Don't make the phone read (and maybe prompt for) data this PC would throw away on arrival.
            message = f"“{blocked}” is turned off for this phone on this PC (Security tab)."
            if type_ in _LOAD_REQUESTS:
                page_key, what = _LOAD_REQUESTS[type_]
                self._pages[page_key].loading_refused(what, message)
            elif type_ == "files.get":
                self._files_page.download_refused(payload.get("path"), message)
            elif type_ == "photos.get":
                self._photos_page.download_refused(message)
            return False
        self._net_thread.run_coro(self._server.send(fingerprint, "control", type_, payload))
        if type_ in _LOAD_REQUESTS:
            page_key, what = _LOAD_REQUESTS[type_]
            self._pages[page_key].loading_started(what)
        return True

    @staticmethod
    def _blocked_here(fingerprint: str, type_: str) -> str | None:
        """This PC's name for the permission that is set to Deny and would drop the answer to request `type_`, if any."""
        answer = _ANSWER_TO.get(type_)
        if answer is None:
            return None
        feature = permissions.feature_for(answer[0], answer[1])
        device = pairing.PairedDevice.load(fingerprint)
        if device is None or feature is None or permissions.mode_of(device, feature) != permissions.DENY:
            return None
        return permissions.FEATURES[feature]

    def _msg_load_progress(self, payload: dict) -> None:
        what = payload.get("what", "")
        page_key = _LOAD_PAGES.get(what)
        if page_key:
            self._pages[page_key].loading_progress(what, int(payload.get("done", 0)), int(payload.get("total", 0)))

    # ---------------------------------------------------------------- connection

    def on_connection_changed(self, fingerprint: str, connected: bool, address: str, link: str = "lan") -> None:
        if connected:
            self._connected[fingerprint] = address
        else:
            self._connected.pop(fingerprint, None)
        self._device_page.set_connected(fingerprint, connected, address, link)
        self._sync.link_kind = link if connected else ""
        self._banner.set_revealed(not self._connected)
        if connected:
            self._net_thread.run_coro(self._server.send(fingerprint, "control", "device.info_request", {}))
            if self._active_fp not in self._connected or self._active_fp == fingerprint:
                self._set_active_phone(fingerprint)  # first phone, or the active one came back
        elif fingerprint == self._active_fp:
            self._mirror_page.on_disconnected()
            self._webcam_page.on_disconnected()
            self._calls_page.on_disconnected()
            self._app.withdraw_notification("call")
            self._denial_toasted.clear()
            self._notifications_page.clear()
            others = list(self._connected)
            if others:
                self._set_active_phone(others[0])
        self._refresh_phone_selector()
        self._sync.on_link_change(self._fingerprint() is not None)
        if self._current_key == "security":
            self._security_page.refresh()   # it names who is connected: don't leave that on screen once it changes

    def _video_transport(self) -> str | None:
        return self._server.video_transport.get(self._fingerprint() or "")

    @staticmethod
    def _is_ios(fingerprint: str) -> bool:
        device = pairing.PairedDevice.load(fingerprint)
        return device is not None and device.platform == "ios"

    def _apply_platform(self, fingerprint: str) -> None:
        """Hide the tabs an iPhone can't back; show them all again for an Android phone."""
        hidden = pairing.IOS_HIDDEN_SECTIONS if self._is_ios(fingerprint) else frozenset()
        for index, (key, _title, _icon) in enumerate(SECTIONS):
            row = self._sidebar_list.get_row_at_index(index)
            if row is not None:
                row.set_visible(key not in hidden)
        if self._current_key in hidden:
            self._select_sidebar("device")

    def _set_active_phone(self, fingerprint: str) -> None:
        """Point every tab at this phone and reload their data from it."""
        if fingerprint not in self._connected:
            self.toast(f"{self._phone_name(fingerprint)} isn't connected")
            return
        switching = self._active_fp is not None and self._active_fp != fingerprint
        if switching:
            self._mirror_page.stop_if_running()
            self._webcam_page.stop_if_running()
        self._active_fp = fingerprint
        self._apply_platform(fingerprint)
        self._device_page.set_active(fingerprint)
        self._notifications_page.clear()
        for page in self._pages.values():
            if hasattr(page, "reset"):
                page.reset()
            if switching and hasattr(page, "forget_phone"):
                page.forget_phone()   # one phone's data must not be shown, or answered, as another's
        # Cheap things load straight away; heavy pages load when opened.
        if not self._is_ios(fingerprint):
            self._send("notifications.sync", {})
            self._messages_page.mark_requested()  # so opening the tab doesn't ask again
            self._calls_page.mark_requested()     # (before sending: a request refused at once undoes it)
            self._send("sms.list_threads", {"offset": 0, "limit": SMS_PAGE})
            self._send("calls.history_request", {"offset": 0, "limit": CALLS_PAGE})
            self._send("contacts.list_request", {"offset": 0, "limit": CONTACTS_PAGE})
            # No screen.state_request here: the phone reports its screen by itself on connect, and asking needs its
            # "Remote control" permission, which would prompt the phone's user on every connection.
        page = self._pages[self._current_key]
        if hasattr(page, "on_shown"):
            page.on_shown()
        self._refresh_phone_selector()
        if switching:
            self.toast(f"Now controlling {self._phone_name(fingerprint)}")

    def _refresh_phone_selector(self) -> None:
        self._update_link_label()
        fps = list(self._connected)
        self._updating_dropdown = True
        try:
            self._phone_model.splice(0, self._phone_model.get_n_items(), [self._phone_name(fp) for fp in fps])
            self._phone_fps = fps
            if self._active_fp in fps:
                self._phone_dropdown.set_selected(fps.index(self._active_fp))
        finally:
            self._updating_dropdown = False
        self._phone_dropdown.set_visible(len(fps) > 1)

    def _on_phone_selected(self, dropdown: Gtk.DropDown, _pspec) -> None:
        if self._updating_dropdown:
            return
        idx = dropdown.get_selected()
        if 0 <= idx < len(self._phone_fps) and self._phone_fps[idx] != self._active_fp:
            self._set_active_phone(self._phone_fps[idx])

    def _update_link_label(self) -> None:
        self._update_status_card()

    def _update_status_card(self) -> None:
        if self._connected:
            names = [self._phone_name(fp) for fp in self._connected]
            self._link_name.set_label("📱 " + ", ".join(names))
            self._link_label.set_label("● Connected" + self._battery_text)
            self._link_label.set_css_classes(["caption", "success"])
        else:
            self._link_name.set_label("📱 No phone")
            self._link_label.set_label("○ Open PhoneBridge on your phone")
            self._link_label.set_css_classes(["caption", "dim-label"])
            self._battery_text = ""

    # ---------------------------------------------------------------- incoming

    def on_phone_message(self, fingerprint: str, type_: str, payload: dict) -> None:
        if type_ == "device.info":
            self._device_page.update_info(fingerprint, payload)
            if payload.get("battery_pct") is not None:
                self._battery_text = f" · 🔋 {payload['battery_pct']}%" + (" ⚡" if payload.get("charging") else "")
                self._update_status_card()
            return
        if type_ == "device.unpair":
            # The phone's own Unpair button: forget it here too.
            name = self._phone_name(fingerprint)
            self._net_thread.run_coro(self._server.revoke(fingerprint, "unpaired_on_phone"))
            self._device_page.remove_device(fingerprint)
            self.toast(f"{name} was unpaired from the phone")
            return
        if fingerprint != self._fingerprint():
            # Another connected phone: tabs belong to the active one, but still surface
            # calls and texts so nothing is missed.
            name = self._phone_name(fingerprint)
            if type_ == "call.ringing":
                who = payload.get("caller_name") or payload.get("number") or "Unknown caller"
                self._desktop_notify(f"call-{fingerprint}", f"Incoming call on {name}: {who}",
                                     "Switch to this phone in PhoneBridge to answer", "calls", urgent=True)
            elif type_ == "sms.new":
                who = payload.get("name") or payload.get("address", "")
                self._desktop_notify(f"sms-{fingerprint}", f"{who} (on {name})",
                                     payload.get("message", {}).get("text", ""), "messages")
            return
        if type_ in _LOAD_RESPONSES:
            what = _LOAD_RESPONSES[type_]
            self._pages[_LOAD_PAGES[what]].loading_done(what)
        handler = getattr(self, "_msg_" + type_.replace(".", "_"), None)
        if handler is not None:
            handler(payload)

    def _msg_sms_mms_status(self, payload: dict) -> None:
        self._messages_page.mms_status(payload)
        if payload.get("state") == "failed":
            self.toast(payload.get("message") or "The picture message couldn't be sent")

    def _msg_hotspot_state(self, payload: dict) -> None:
        fp = self._fingerprint() or ""
        if payload.get("error"):
            self._device_page.set_hotspot_text(fp, payload["error"])
            self.toast(payload["error"])
        elif payload.get("on"):
            ssid, password = payload.get("ssid", ""), payload.get("password", "")
            self._device_page.set_hotspot_text(fp, f"On — {ssid}  /  {password}")
            dialog = Adw.MessageDialog(
                transient_for=self, heading="Phone hotspot is on",
                body=f"Network: {ssid}\nPassword: {password}\n\nThis is a private network with no internet. "
                     "Joining it disconnects this PC from its current Wi-Fi.")
            dialog.add_response("close", "Close")
            dialog.add_response("join", "Join from this PC")
            dialog.set_response_appearance("join", Adw.ResponseAppearance.SUGGESTED)

            def on_response(_d, response: str) -> None:
                if response == "join":
                    import threading

                    from .. import wifi_join

                    def join() -> None:   # off the GTK thread: it waits for NetworkManager; the password never reaches a command line
                        _ok, message = wifi_join.join(ssid, password)
                        GLib.idle_add(self.toast, message)

                    threading.Thread(target=join, daemon=True).start()
                    self.toast(f"Joining {ssid}…")

            dialog.connect("response", on_response)
            dialog.present()
        else:
            self._device_page.set_hotspot_text(fp, payload.get("message") or "Off")

    def _msg_notifications_all(self, payload: dict) -> None:
        self._notifications_page.set_all(payload.get("notifications", []))

    def _msg_notifications_unavailable(self, _payload: dict) -> None:
        self.toast("Allow \"Notification access\" for PhoneBridge on the phone")

    def _msg_notification_posted(self, payload: dict) -> None:
        self._notifications_page.add_notification(payload)
        if not payload.get("ongoing"):
            self._desktop_notify(f"phone-{payload.get('id')}", payload.get("title") or payload.get("app", ""),
                                 payload.get("text", ""), "notifications")

    def _msg_notification_removed(self, payload: dict) -> None:
        self._notifications_page.remove_notification(payload.get("id", ""))
        self._app.withdraw_notification(f"phone-{payload.get('id')}")

    def _msg_clipboard_update(self, payload: dict) -> None:
        image = payload.get("image")
        if image:
            if len(image) <= 6 * 1024 * 1024:  # base64 of the 4 MiB cap
                self._last_clip_image = image
                self._clip_bridge.set_image(image)
                self.toast("Copied image from phone")
            return
        text = payload.get("text", "")
        if not text:
            return
        self._clipboard_page.set_remote_text(text)
        self._mirror_page.set_clipboard(text, "phone")
        if self._clipboard_page.sync_switch.get_active() or payload.get("copied_on_phone"):
            self._last_clip_text = text  # suppress the echo our own clipboard write triggers
            self._clip_bridge.set_text(text, expire=True)   # synced text doesn't linger on the clipboard
            preview = " ".join(text.split())
            self.toast(f"Copied from phone: {preview[:60]}{'…' if len(preview) > 60 else ''}")

    def _msg_sms_thread_list(self, payload: dict) -> None:
        self._messages_page.set_threads(payload)

    def _msg_sms_thread_messages(self, payload: dict) -> None:
        self._messages_page.set_messages(payload)

    def _msg_sms_new(self, payload: dict) -> None:
        self._messages_page.add_incoming_message(payload)
        who = payload.get("name") or payload.get("address", "")
        self._desktop_notify(f"sms-{payload.get('thread_id')}", who, payload.get("message", {}).get("text", ""),
                             "messages")

    def _msg_sms_sent(self, _payload: dict) -> None:
        self._send("sms.list_threads", {"offset": 0, "limit": SMS_PAGE})

    def _msg_calls_history(self, payload: dict) -> None:
        self._calls_page.set_history(payload)

    def _msg_contacts_list(self, payload: dict) -> None:
        self._calls_page.set_contacts(payload)

    def _msg_call_ringing(self, payload: dict) -> None:
        self._calls_page.on_ringing(payload.get("caller_name", ""), payload.get("number", ""))
        who = payload.get("caller_name") or payload.get("number") or "Unknown caller"
        self._desktop_notify("call", f"Incoming call: {who}", "Open PhoneBridge › Calls to answer", "calls",
                             urgent=True)

    def _msg_call_state(self, payload: dict) -> None:
        self._calls_page.on_state(payload)
        if payload.get("state") == "ended":
            self._app.withdraw_notification("call")

    def _msg_photos_list_result(self, payload: dict) -> None:
        self._photos_page.set_photos(payload)

    def _msg_files_list_result(self, payload: dict) -> None:
        self._files_page.set_listing(payload)

    def _msg_file_saved(self, payload: dict) -> None:
        self._files_page.on_phone_saved(payload)
        self.toast(f"Saved on phone: {payload.get('filename', '')}")

    def _msg_apps_list(self, payload: dict) -> None:
        self._apps_page.set_apps(payload.get("apps", []))

    def _msg_video_started(self, payload: dict) -> None:
        page = self._mirror_page if payload.get("kind") == "mirror" else self._webcam_page
        page.on_video_started(payload)
        if payload.get("kind") == "mirror":
            self._sync.on_video_started(payload)

    def _msg_video_stopped(self, payload: dict) -> None:
        if payload.get("kind") == "mirror":
            self._sync.on_video_stopped()   # restores the display first; the page below then tidies up
        if payload.get("kind") == "mirror" and self._mirror_page.is_mirroring():
            self._mirror_page.stop_if_running()
            self._mirror_page.set_status("Mirroring stopped on the phone")

    def _msg_screen_state(self, payload: dict) -> None:
        self._mirror_page.on_screen_state(payload)

    def _msg_volume_state(self, payload: dict) -> None:
        self._mirror_page.on_volume_state(payload)

    def _msg_mirror_status(self, payload: dict) -> None:
        self._mirror_page.set_status(payload.get("message", ""))
        if payload.get("state") in ("denied", "error"):
            self._sync.on_mirror_error(payload.get("message", "") or payload.get("state"))
            self._mirror_page.stop_if_running()
            self.toast(payload.get("message", ""))

    def _msg_webcam_status(self, payload: dict) -> None:
        ended = payload.get("state") in ("error", "stopped")
        if ended:   # the phone's camera is not streaming (any more): don't keep waiting on a frozen or empty picture
            self._webcam_page.stop_if_running()
        self._webcam_page.set_status(payload.get("message", ""))
        if ended:
            self.toast(payload.get("message", ""))

    def _msg_error(self, payload: dict) -> None:
        message = payload.get("message", "Phone reported an error")
        if payload.get("transfer_id"):
            self._files_page.on_phone_failed(str(payload["transfer_id"]), message)
        self.toast(message)

    def _msg_permission_denied(self, payload: dict) -> None:
        """The phone's permission manager refused a request, or is still asking its user. Whatever was waiting for the
        answer must stop waiting, and the user needs to know where the switch is. (file.offer: file_transfer.py.)"""
        type_ = str(payload.get("type", ""))
        feature = str(payload.get("feature", ""))
        if type_ in _BACKGROUND_REQUESTS or type_ == "file.offer":
            return
        name = PHONE_PERMISSIONS.get(feature, feature)
        message = f"The phone didn't allow “{name}”. Approve it there, or change it in PhoneBridge › Security."
        if type_ in _LOAD_REQUESTS:
            page_key, what = _LOAD_REQUESTS[type_]
            self._pages[page_key].loading_refused(what, message)
        elif type_ in ("sms.send", "sms.send_mms"):
            self._messages_page.send_refused(type_ == "sms.send_mms", payload.get("request_id"),
                                             f"“{name}” isn't allowed on the phone")
            self.toast(f"Not sent. {message}")
        elif type_ == "call.dial":
            self._calls_page.dial_refused()
            self.toast(f"Not called. {message}")
        elif type_ == "mirror.start":
            self._msg_mirror_status({"state": "denied", "message": message})
        elif type_ in ("webcam.start", "webcam.switch"):
            if type_ == "webcam.start":
                self._webcam_page.stop_if_running()
            self._webcam_page.set_status(message)
            self.toast(message)
        else:
            if type_ == "files.get":
                self._files_page.download_refused(payload.get("path"), message)
            elif type_ == "photos.get":
                self._photos_page.download_refused(message)
            elif type_ == "mirror.input":
                self._mirror_page.set_status(message)
            if time.monotonic() - self._denial_toasted.get(feature, float("-inf")) >= DENIAL_TOAST_SECONDS:
                self._denial_toasted[feature] = time.monotonic()
                self.toast(message)

    # file transfers phone -> PC (called by app.py)

    def on_receive_progress(self, payload: dict, received: int, total: int) -> None:
        if not payload.get("photo_id"):
            self._files_page.on_receive_progress(payload, received, total)

    def on_file_received(self, payload: dict, dest_path: Path) -> None:
        if payload.get("photo_id"):
            self._photos_page.on_photo_saved(dest_path, str(payload["photo_id"]))
        else:
            self._files_page.on_file_received(payload, dest_path)
            if not payload.get("source_path"):
                self.toast(f"Received {dest_path.name} from phone")

    def on_file_failed(self, payload: dict, reason: str) -> None:
        if payload.get("photo_id"):
            self._photos_page.download_failed(str(payload["photo_id"]), reason)
        else:
            self._files_page.on_receive_failed(payload, reason)
            if reason != "cancelled" and not payload.get("source_path"):
                self.toast(f"A file from the phone didn't arrive: {reason}")

    # ---------------------------------------------------------------- actions

    def _on_notification_action(self, notif_id: str, action_id: str, reply_text: str | None) -> None:
        payload = {"id": notif_id, "action_id": action_id}
        if reply_text is not None:
            payload["reply_text"] = reply_text
        self._send("notification.action", payload)

    def _compose_message(self, number: str, name: str) -> None:
        self._select_sidebar("messages")
        self._messages_page.open_compose(number, name)

    def _select_sidebar(self, key: str) -> None:
        index = next(i for i, (k, _t, _ic) in enumerate(SECTIONS) if k == key)
        self._sidebar_list.select_row(self._sidebar_list.get_row_at_index(index))
        self._show_page(key)

    def _launch_and_mirror(self, pkg: str) -> None:
        if not self._send("apps.launch", {"pkg": pkg}):
            return
        self._select_sidebar("mirror")
        if not self._mirror_page.is_mirroring():
            self._mirror_page._on_toggle_clicked(None)

    def _configure_relay(self, text: str) -> None:
        """text = "host[:port]" or "" to switch the relay off."""
        from .. import relay_client

        host, _, port_text = text.partition(":")
        host = host.strip()
        if port_text and not port_text.isdigit():
            self.toast("The port must be a number, e.g. relay.example.com:28200")
            return
        port = int(port_text) if port_text else relay_client.DEFAULT_PORT
        relay_client.save_config(host or None, port)
        self._net_thread.run_coro(self._server.configure_relay(host or None, port))
        self.toast(f"Relay {host}:{port} turned on" if host else "Relay turned off")

    def _stream_app(self, pkg: str, label: str) -> None:
        if self._fingerprint() is None:
            self.toast("No phone connected")
            return
        self._select_sidebar("mirror")
        self._mirror_page.start_app(pkg, label)

    def _on_mirror_start(self) -> bool:
        fingerprint = self._fingerprint()
        if fingerprint is None:
            return False
        self._webcam_page.stop_if_running()
        self._sync.adopt_manual_start()   # no-op when the sync controller itself started this
        self._server.set_video_sink(fingerprint, self._on_mirror_frame)
        self._send("screen.state_request", {})
        app = self._mirror_page.app
        return self._send("mirror.start", {"app": app} if app else {})

    def _on_mirror_stop(self) -> None:
        fingerprint = self._fingerprint()
        if fingerprint is not None:
            self._server.set_video_sink(fingerprint, None)
            self._send("mirror.stop", {})
        self._sync.viewer_stopped()   # restores the phone display, ends the sync session

    def _on_mirror_frame(self, data: bytes) -> None:
        """Net thread: every access unit counts as a received frame for the sync controller, then goes to the decoder."""
        self._sync.on_frame(len(data))
        self._mirror_page.push_frame(data)

    # ---- hooks used by screensync.glue (GTK thread)
    def sync_refresh(self) -> bool:
        self._sync_page.refresh()
        return False

    def sync_present_viewer(self) -> bool:
        self.present()
        self._select_sidebar("mirror")
        return False

    def sync_start_mirror_page(self) -> bool:
        self.present()
        if not self._mirror_page.is_mirroring():
            self._select_sidebar("mirror")
            self._mirror_page._on_toggle_clicked(None)   # noqa: SLF001 - same path as the Start button
        return False

    def sync_stop_mirror_page(self) -> bool:
        self._mirror_page.stop_if_running()
        return False

    def sync_resolution(self):
        r = self._mirror_page._renderer   # noqa: SLF001
        return r.frame_size if r is not None and r.frame_size[0] else None

    def _on_webcam_start(self, facing: str) -> bool:
        fingerprint = self._fingerprint()
        if fingerprint is None:
            return False
        self._mirror_page.stop_if_running()
        self._server.set_video_sink(fingerprint, self._webcam_page.push_frame)
        return self._send("webcam.start", {"facing": facing})

    def _on_webcam_stop(self) -> None:
        fingerprint = self._fingerprint()
        if fingerprint is not None:
            self._server.set_video_sink(fingerprint, None)
            self._send("webcam.stop", {})

    # ---------------------------------------------------------------- clipboard

    def current_clipboard_text(self) -> str | None:
        """Last text seen on the PC clipboard (for the phone's "Get PC clipboard")."""
        return self._last_clip_text

    def copy_to_pc(self, text: str) -> None:
        self._last_clip_text = text
        self._clip_bridge.set_text(text)
        self.toast("Copied")

    @staticmethod
    def _clipboard_off(fingerprint: str) -> bool:
        """"Clipboard sync" set to Deny for this phone stops both directions, as the phone's own setting does."""
        device = pairing.PairedDevice.load(fingerprint)
        return device is not None and permissions.mode_of(device, "clipboard") == permissions.DENY

    def _send_clipboard_to_phone(self, text: str) -> None:
        fp = self._fingerprint()
        if fp is not None and self._clipboard_off(fp):
            self.toast("“Clipboard sync” is turned off for this phone on this PC (Security tab)")
            return
        if self._send("clipboard.update", {"text": text}):
            self.toast("Sent to phone clipboard")

    _last_clip_image: str | None = None

    def _on_local_image_changed(self, b64: str) -> None:
        """An image was copied on the PC: send it to the phone when clipboard sync is on."""
        if b64 == self._last_clip_image:
            return  # our own write coming back
        self._last_clip_image = b64
        fp = self._fingerprint()
        if fp and self._clipboard_page.sync_switch.get_active() and not self._clipboard_off(fp):
            self._net_thread.run_coro(
                self._server.send(fp, "control", "clipboard.update", {"image": b64, "mime": "image/png"})
            )

    def _on_local_clipboard_changed(self, text: str) -> None:
        if not text or text == self._last_clip_text:
            return
        self._last_clip_text = text
        self._clipboard_page.add(text, "pc")
        self._mirror_page.set_clipboard(text, "pc")
        fp = self._fingerprint()
        if fp and self._clipboard_page.sync_switch.get_active() and not self._clipboard_off(fp):
            self._net_thread.run_coro(self._server.send(fp, "control", "clipboard.update", {"text": text}))

    # ---------------------------------------------------------------- desktop notifications

    def _desktop_notify(self, notif_id: str, title: str, body: str, page: str, urgent: bool = False) -> None:
        if self.is_active() and self._current_key == page:
            return  # already looking at it
        n = Gio.Notification.new(title or "PhoneBridge")
        n.set_body(body[:300])
        n.set_default_action_and_target_value("app.show-page", GLib.Variant.new_string(page))
        if urgent:
            n.set_priority(Gio.NotificationPriority.URGENT)
        self._app.send_notification(notif_id, n)

    def show_page_from_notification(self, key: str) -> None:
        self._select_sidebar(key)
        self.present()
