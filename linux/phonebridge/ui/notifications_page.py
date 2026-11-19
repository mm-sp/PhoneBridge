from __future__ import annotations

import base64
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
from gi.repository import Adw, Gdk, GLib, Gtk  # noqa: E402


def texture_from_b64(data: str) -> Gdk.Texture | None:
    if not data:
        return None
    try:
        return Gdk.Texture.new_from_bytes(GLib.Bytes.new(base64.b64decode(data)))
    except (GLib.Error, ValueError):
        return None


def friendly_time(ts_ms: int | float | None) -> str:
    if not ts_ms:
        return ""
    ts = ts_ms / 1000
    delta = time.time() - ts
    if delta < 60:
        return "now"
    if delta < 3600:
        return f"{int(delta // 60)} min ago"
    if time.localtime(ts)[:3] == time.localtime()[:3]:
        return time.strftime("%H:%M", time.localtime(ts))
    if delta < 6 * 86400:
        return time.strftime("%a %H:%M", time.localtime(ts))
    return time.strftime("%d %b %Y", time.localtime(ts))


class NotificationsPage(Gtk.Box):
    """The phone's active notifications. Dismissing one here dismisses it on the phone;
    action buttons and inline reply run the notification's own actions there."""

    def __init__(self, on_action, on_refresh) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self._on_action = on_action
        self._rows: dict[str, Gtk.Widget] = {}
        self._data: dict[str, dict] = {}

        header = Gtk.Box(spacing=8, margin_top=12, margin_bottom=6, margin_start=16, margin_end=16)
        self._count = Gtk.Label(xalign=0, hexpand=True, css_classes=["title-3"])
        header.append(self._count)
        refresh = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Refresh from phone")
        refresh.connect("clicked", lambda _b: on_refresh())
        header.append(refresh)
        clear = Gtk.Button(label="Clear all")
        clear.connect("clicked", self._on_clear_all)
        header.append(clear)
        self.append(header)

        self._list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"])
        self._list.set_placeholder(Adw.StatusPage(
            title="No notifications", icon_name="preferences-system-notifications-symbolic",
            description="Notifications on your phone appear here.\nIf nothing shows up, allow "
                        "\"Notification access\" in the PhoneBridge app on the phone.",
        ))
        clamp = Adw.Clamp(maximum_size=760, margin_start=16, margin_end=16, margin_bottom=16)
        clamp.set_child(self._list)
        scroll = Gtk.ScrolledWindow(vexpand=True)
        scroll.set_child(clamp)
        self.append(scroll)
        self._update_count()

    # --- data from phone ---

    def set_all(self, notifications: list[dict]) -> None:
        for row in list(self._rows.values()):
            self._list.remove(row)
        self._rows.clear()
        self._data.clear()
        for n in notifications:
            self.add_notification(n, at_top=False)
        self._update_count()

    def add_notification(self, n: dict, at_top: bool = True) -> None:
        notif_id = n.get("id", "")
        if notif_id in self._rows:
            self._list.remove(self._rows.pop(notif_id))
        row = self._build_row(n)
        if at_top:
            self._list.prepend(row)
        else:
            self._list.append(row)
        self._rows[notif_id] = row
        self._data[notif_id] = n
        self._update_count()

    def remove_notification(self, notif_id: str) -> None:
        row = self._rows.pop(notif_id, None)
        self._data.pop(notif_id, None)
        if row is not None:
            self._list.remove(row)
        self._update_count()

    def clear(self) -> None:
        self.set_all([])

    # --- UI ---

    def _update_count(self) -> None:
        n = len(self._rows)
        self._count.set_label(f"{n} notification{'s' if n != 1 else ''}" if n else "Notifications")

    def _build_row(self, n: dict) -> Gtk.Widget:
        notif_id = n.get("id", "")
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_top=10, margin_bottom=10,
                        margin_start=12, margin_end=8)
        top = Gtk.Box(spacing=10)
        texture = texture_from_b64(n.get("icon", ""))
        icon = Gtk.Image.new_from_paintable(texture) if texture else Gtk.Image.new_from_icon_name("application-x-executable-symbolic")
        icon.set_pixel_size(32)
        icon.set_valign(Gtk.Align.START)
        top.append(icon)

        text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
        meta = Gtk.Label(xalign=0, css_classes=["caption", "dim-label"])
        meta.set_label(" · ".join(x for x in (n.get("app", ""), friendly_time(n.get("ts"))) if x))
        text_box.append(meta)
        if n.get("title"):
            text_box.append(Gtk.Label(label=n["title"], xalign=0, wrap=True, css_classes=["heading"]))
        if n.get("text"):
            body = Gtk.Label(label=n["text"], xalign=0, wrap=True, selectable=True)
            body.set_max_width_chars(80)
            text_box.append(body)
        top.append(text_box)

        open_btn = Gtk.Button(icon_name="go-next-symbolic", tooltip_text="Open on phone",
                              valign=Gtk.Align.START, css_classes=["flat"])
        open_btn.connect("clicked", lambda _b: self._on_action(notif_id, "open", None))
        top.append(open_btn)
        if n.get("clearable", True):
            dismiss = Gtk.Button(icon_name="window-close-symbolic", tooltip_text="Dismiss",
                                 valign=Gtk.Align.START, css_classes=["flat"])
            dismiss.connect("clicked", lambda _b: self._dismiss(notif_id))
            top.append(dismiss)
        outer.append(top)

        actions = n.get("actions") or []
        plain = [a for a in actions if not a.get("is_reply")]
        if plain:
            bar = Gtk.Box(spacing=6, margin_start=42)
            for a in plain[:3]:
                b = Gtk.Button(label=a.get("label", ""), css_classes=["flat"])
                b.connect("clicked", lambda _b, aid=a.get("id"): self._on_action(notif_id, aid, None))
                bar.append(b)
            outer.append(bar)
        if any(a.get("is_reply") for a in actions):
            reply = Gtk.Box(spacing=6, margin_start=42)
            entry = Gtk.Entry(placeholder_text="Reply…", hexpand=True)
            send = Gtk.Button(icon_name="mail-send-symbolic", css_classes=["suggested-action"])

            def do_reply(*_a, e=entry):
                text = e.get_text().strip()
                if text:
                    self._on_action(notif_id, "reply", text)
                    e.set_text("")
                    e.set_placeholder_text("Sent")

            entry.connect("activate", do_reply)
            send.connect("clicked", do_reply)
            reply.append(entry)
            reply.append(send)
            outer.append(reply)

        row = Gtk.ListBoxRow(activatable=False)
        row.set_child(outer)
        return row

    def _dismiss(self, notif_id: str) -> None:
        self.remove_notification(notif_id)
        self._on_action(notif_id, "dismiss", None)

    def _on_clear_all(self, _button) -> None:
        for notif_id, n in list(self._data.items()):
            if n.get("clearable", True):
                self._dismiss(notif_id)
