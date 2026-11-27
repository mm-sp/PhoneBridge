from __future__ import annotations

import base64
import mimetypes
import time
import uuid

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from .loading import LoadingBar, LoadingMixin
from .notifications_page import friendly_time


class MessagesPage(Gtk.Box, LoadingMixin):
    """Two-pane SMS view: every conversation stored on the phone on the left, the full
    conversation + composer on the right.

    Doesn't own any networking itself — main_window.py wires its callbacks to the real
    ControlChannelServer, same pattern as FilesPage.
    """

    def __init__(self, on_request_threads, on_request_thread, on_send, on_send_mms=None) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self._on_request_threads = on_request_threads
        self._on_request_thread = on_request_thread
        self._on_send = on_send
        self._on_send_mms = on_send_mms
        self._attachments: list[dict] = []   # picked for the next message: {mime, name, data(b64)}
        self._local_mms: list[tuple[str, dict]] = []  # sent from here; the phone's DB doesn't list them
        self._threads: dict[str, dict] = {}
        self._rows: dict[str, Gtk.ListBoxRow] = {}
        self._active_thread_id: str | None = None
        self._active_address = ""
        self._loaded = False
        self._thread_msgs: list[dict] = []  # loaded messages of the open conversation, oldest first
        self._thread_has_more = False

        # --- left: thread list ---
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, width_request=300)
        tools = Gtk.Box(spacing=6, margin_top=8, margin_bottom=8, margin_start=8, margin_end=8)
        self._search = Gtk.SearchEntry(placeholder_text="Search conversations", hexpand=True)
        self._search.connect("search-changed", lambda _e: self._thread_list.invalidate_filter())
        tools.append(self._search)
        new_btn = Gtk.Button(icon_name="list-add-symbolic", tooltip_text="New message")
        new_btn.connect("clicked", lambda _b: self.open_compose("", ""))
        tools.append(new_btn)
        refresh_btn = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Refresh")
        refresh_btn.connect("clicked", lambda _b: self._request_threads(0))
        tools.append(refresh_btn)
        left.append(tools)
        self._loading_bars = {
            "sms.threads": LoadingBar("sms.threads", margin_start=10, margin_end=10),
            "sms.thread": LoadingBar("sms.thread", margin_start=16, margin_end=16),
        }
        left.append(self._loading_bars["sms.threads"])

        self._thread_list = Gtk.ListBox(css_classes=["navigation-sidebar"])
        self._thread_list.set_filter_func(self._filter)
        self._thread_list.connect("row-activated", self._on_thread_activated)
        self._thread_list.set_placeholder(Gtk.Label(label="No conversations loaded yet", margin_top=24,
                                                    css_classes=["dim-label"]))
        left_inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        left_inner.append(self._thread_list)
        self._more_threads_btn = Gtk.Button(label="Load more conversations", visible=False, margin_top=6,
                                            margin_bottom=6, margin_start=10, margin_end=10)
        self._more_threads_btn.connect("clicked", lambda _b: self._request_threads(len(self._rows)))
        left_inner.append(self._more_threads_btn)
        left_scroll = Gtk.ScrolledWindow(vexpand=True)
        left_scroll.set_child(left_inner)
        left.append(left_scroll)
        self._count_label = Gtk.Label(css_classes=["caption", "dim-label"], margin_bottom=6)
        left.append(self._count_label)
        self.append(left)

        self.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))

        # --- right: conversation + composer ---
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        head = Gtk.Box(spacing=8, margin_top=10, margin_bottom=10, margin_start=16, margin_end=16)
        head_text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        self._title = Gtk.Label(label="Select a conversation", xalign=0, css_classes=["title-4"])
        self._subtitle = Gtk.Label(xalign=0, css_classes=["caption", "dim-label"])
        head_text.append(self._title)
        head_text.append(self._subtitle)
        head.append(head_text)
        self._to_entry = Gtk.Entry(placeholder_text="Phone number (several: separate with commas — picture messages only)",
                                   visible=False, width_chars=30)
        head.append(self._to_entry)
        right.append(head)
        right.append(Gtk.Separator())
        right.append(self._loading_bars["sms.thread"])

        self._messages_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4,
                                     margin_top=12, margin_bottom=12, margin_start=16, margin_end=16)
        self._messages_scroll = Gtk.ScrolledWindow(vexpand=True)
        self._messages_scroll.set_child(self._messages_box)
        right.append(self._messages_scroll)

        self._attach_row = Gtk.Box(spacing=6, margin_start=12, margin_end=12, margin_top=6, visible=False)
        self._attach_label = Gtk.Label(xalign=0, hexpand=True, ellipsize=3, css_classes=["caption"])
        clear_btn = Gtk.Button(icon_name="window-close-symbolic", css_classes=["flat"], tooltip_text="Remove attachments")
        clear_btn.connect("clicked", lambda _b: self._set_attachments([]))
        self._attach_row.append(self._attach_label)
        self._attach_row.append(clear_btn)
        right.append(self._attach_row)

        composer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6,
                           margin_start=12, margin_end=12, margin_bottom=12, margin_top=6)
        if on_send_mms is not None:
            attach_btn = Gtk.Button(icon_name="mail-attachment-symbolic", tooltip_text="Attach pictures (sent as MMS)")
            attach_btn.connect("clicked", self._on_attach_clicked)
            composer.append(attach_btn)
        self._entry = Gtk.Entry(hexpand=True, placeholder_text="Type a text message…")
        self._entry.connect("activate", self._on_send_clicked)
        send_btn = Gtk.Button(icon_name="mail-send-symbolic", css_classes=["suggested-action"], tooltip_text="Send")
        send_btn.connect("clicked", self._on_send_clicked)
        composer.append(self._entry)
        composer.append(send_btn)
        right.append(composer)
        self.append(right)

    # --- lifecycle ---

    def on_shown(self) -> None:
        if not self._loaded:
            self._request_threads(0)

    def _request_threads(self, offset: int) -> None:
        self._more_threads_btn.set_sensitive(False)
        self._on_request_threads(offset)

    def reset(self) -> None:
        self._loaded = False

    def mark_requested(self) -> None:
        self._loaded = True

    def after_refusal(self, what: str) -> None:
        if what == "sms.threads":
            self._loaded = bool(self._rows)   # nothing to show: ask again the next time the tab is opened
            self._more_threads_btn.set_sensitive(True)
        elif self._thread_msgs:
            self._render_thread()   # brings the "Load older messages" button back

    def send_refused(self, mms: bool, request_id: str | None, reason: str) -> None:
        """The phone didn't send a message typed here: its bubble must not stay "sending…"."""
        if mms:
            pending = [m for _addr, m in self._local_mms if m.get("status") == "sending"
                       and (not request_id or m.get("request_id") == request_id)]
        else:   # bubbles added here have no id, unlike messages listed by the phone
            pending = [m for m in self._thread_msgs
                       if m.get("status") == "sending" and not m.get("mms") and not m.get("id")]
        if not pending:
            return
        pending[0].update(status="failed", error=reason)   # answers arrive in the order the messages were sent
        self._render_thread()
        self._scroll_to_end()

    # --- data from phone ---

    def set_threads(self, payload: dict) -> None:
        """A page of conversations: offset 0 replaces the list, later pages append."""
        self._loaded = True
        if not payload.get("offset"):
            for row in list(self._rows.values()):
                self._thread_list.remove(row)
            self._rows.clear()
            self._threads.clear()
        for thread in payload.get("threads", []):
            if str(thread.get("id")) not in self._rows:
                self._add_thread_row(thread)
        total = payload.get("total")
        shown = len(self._rows)
        self._count_label.set_label(f"{shown} of {total} conversations" if total else f"{shown} conversations")
        self._more_threads_btn.set_visible(bool(payload.get("has_more")))
        self._more_threads_btn.set_sensitive(True)

    def _add_thread_row(self, thread: dict, at_top: bool = False) -> None:
        thread_id = str(thread.get("id"))
        self._threads[thread_id] = thread
        name = thread.get("name") or thread.get("address") or "(unknown)"
        subtitle = (thread.get("last_text") or "").replace("\n", " ")
        row = Adw.ActionRow(title=GLib.markup_escape_text(name), subtitle=GLib.markup_escape_text(subtitle[:90]))
        row.set_subtitle_lines(1)
        row.set_activatable(True)
        row.set_name(thread_id)
        suffix = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER)
        suffix.append(Gtk.Label(label=friendly_time(thread.get("ts")), css_classes=["caption", "dim-label"]))
        unread = int(thread.get("unread") or 0)
        if unread:
            suffix.append(Gtk.Label(label=str(unread), css_classes=["caption", "accent"]))
        row.add_suffix(suffix)
        if at_top:
            self._thread_list.prepend(row)
        else:
            self._thread_list.append(row)
        self._rows[thread_id] = row

    def set_messages(self, payload: dict) -> None:
        thread_id = str(payload.get("thread_id"))
        if self._active_thread_id is None and _same_number(payload.get("address") or "", self._active_address):
            self._active_thread_id = thread_id  # a compose window just got its thread id
        if thread_id != self._active_thread_id:
            return
        self._set_header(payload.get("name") or "", payload.get("address") or self._active_address)
        page = payload.get("messages", [])
        older = bool(payload.get("offset"))
        self._thread_msgs = page + self._thread_msgs if older else page
        if not older:
            self._thread_msgs += [m for addr, m in self._local_mms if _same_number(addr, self._active_address)]
        self._thread_has_more = bool(payload.get("has_more"))
        self._render_thread()
        if older:
            GLib.timeout_add(50, lambda: (self._messages_scroll.get_vadjustment().set_value(0), False)[1])
        else:
            self._scroll_to_end()

    def _render_thread(self) -> None:
        for child in list(self._messages_box):
            self._messages_box.remove(child)
        if self._thread_has_more:
            older = Gtk.Button(label="Load older messages", halign=Gtk.Align.CENTER, css_classes=["pill"])
            older.connect("clicked", self._on_load_older)
            self._messages_box.append(older)
        last_day = None
        for message in self._thread_msgs:
            day = time.strftime("%A, %d %B %Y", time.localtime((message.get("ts") or 0) / 1000))
            if day != last_day:
                self._messages_box.append(Gtk.Label(label=day, css_classes=["caption", "dim-label"], margin_top=8))
                last_day = day
            self._append_bubble(message.get("from_me", False), message.get("text", ""), message.get("ts"),
                                message.get("status"), message.get("attachments"), message.get("error"))
        if not self._thread_msgs:
            self._messages_box.append(Gtk.Label(label="No messages yet", css_classes=["dim-label"]))

    def _on_load_older(self, button: Gtk.Button) -> None:
        if self._active_thread_id is not None:
            button.set_sensitive(False)
            button.set_label("Loading…")
            # Paging counts SMS rows only (MMS are merged in by time on the phone).
            self._on_request_thread(self._active_thread_id, sum(1 for m in self._thread_msgs if not m.get("mms")))

    def add_incoming_message(self, payload: dict) -> None:
        thread_id = str(payload.get("thread_id"))
        message = payload.get("message", {})
        if thread_id == self._active_thread_id:
            self._thread_msgs.append({"from_me": False, "text": message.get("text", ""), "ts": message.get("ts")})
            self._append_bubble(False, message.get("text", ""), message.get("ts"), None)
            self._scroll_to_end()
        thread = dict(self._threads.get(thread_id, {"id": thread_id}))
        thread.update(address=payload.get("address", thread.get("address", "")),
                      name=payload.get("name") or thread.get("name", ""),
                      last_text=message.get("text", ""), ts=message.get("ts"),
                      unread=0 if thread_id == self._active_thread_id else int(thread.get("unread") or 0) + 1)
        if thread_id in self._rows:
            self._thread_list.remove(self._rows.pop(thread_id))
        self._add_thread_row(thread, at_top=True)

    # --- UI helpers ---

    def open_compose(self, number: str, name: str) -> None:
        """Start a new conversation (also used by the Calls page's "Message" buttons)."""
        existing = next((tid for tid, t in self._threads.items()
                         if _same_number(t.get("address", ""), number)), None) if number else None
        if existing:
            self._select_thread(existing)
            return
        self._active_thread_id = None
        self._active_address = number
        self._thread_list.unselect_all()
        for child in list(self._messages_box):
            self._messages_box.remove(child)
        self._title.set_label(name or "New message")
        self._subtitle.set_label("")
        self._to_entry.set_visible(True)
        self._to_entry.set_text(number)
        (self._entry if number else self._to_entry).grab_focus()

    def _set_header(self, name: str, address: str) -> None:
        self._to_entry.set_visible(False)
        self._title.set_label(name or address or "Conversation")
        self._subtitle.set_label(address if name else "")

    def _append_bubble(self, from_me: bool, text: str, ts, status: str | None, attachments=None, error: str | None = None) -> None:
        bubble = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2,
                         halign=Gtk.Align.END if from_me else Gtk.Align.START,
                         css_classes=["bubble", "bubble-out" if from_me else "bubble-in"])
        for att in attachments or []:  # MMS pictures / media
            thumb = att.get("thumb")
            if thumb:
                try:
                    texture = Gdk.Texture.new_from_bytes(GLib.Bytes.new(base64.b64decode(thumb)))
                    pic = Gtk.Picture.new_for_paintable(texture)
                    pic.set_can_shrink(True)
                    pic.set_size_request(min(texture.get_width(), 360), min(texture.get_height(), 360))
                    bubble.append(pic)
                    continue
                except (ValueError, GLib.Error):
                    pass
            bubble.append(Gtk.Label(label=f"📎 {att.get('name') or att.get('mime', 'attachment')}", xalign=0))
        if text or not attachments:
            bubble.append(Gtk.Label(label=text, wrap=True, xalign=0, selectable=True, max_width_chars=60))
        meta = time.strftime("%H:%M", time.localtime((ts or time.time() * 1000) / 1000))
        if status == "failed":
            meta += f" · failed: {error}" if error else " · failed"
        elif status == "sending":
            meta += " · sending…"
        bubble.append(Gtk.Label(label=meta, xalign=1, css_classes=["caption", "dim-label"]))
        self._messages_box.append(bubble)

    def _scroll_to_end(self) -> None:
        def go() -> bool:
            adj = self._messages_scroll.get_vadjustment()
            adj.set_value(adj.get_upper())
            return False
        GLib.timeout_add(50, go)

    def _filter(self, row: Gtk.ListBoxRow) -> bool:
        query = self._search.get_text().strip().lower()
        if not query:
            return True
        t = self._threads.get(row.get_name(), {})
        return any(query in (t.get(k) or "").lower() for k in ("name", "address", "last_text"))

    def _select_thread(self, thread_id: str) -> None:
        row = self._rows.get(thread_id)
        if row is not None:
            self._thread_list.select_row(row)
        self._open_thread(thread_id)

    def _on_thread_activated(self, _listbox: Gtk.ListBox, row: Gtk.ListBoxRow) -> None:
        self._open_thread(row.get_name())

    def _open_thread(self, thread_id: str) -> None:
        thread = self._threads.get(thread_id, {})
        self._active_thread_id = thread_id
        self._active_address = thread.get("address", "")
        self._set_header(thread.get("name", ""), self._active_address)
        for child in list(self._messages_box):
            self._messages_box.remove(child)
        self._thread_msgs = []
        self._thread_has_more = False
        self._on_request_thread(thread_id, 0)

    # --- MMS ---

    MAX_ATTACH_BYTES = 4 * 1024 * 1024  # raw; base64 inflates it and a control frame is capped at 8 MiB

    def _on_attach_clicked(self, _btn) -> None:
        dialog = Gtk.FileDialog(title="Attach pictures")
        images = Gtk.FileFilter(name="Pictures")
        images.add_mime_type("image/*")
        everything = Gtk.FileFilter(name="All files")
        everything.add_pattern("*")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(images)
        filters.append(everything)
        dialog.set_filters(filters)
        dialog.open_multiple(self.get_root(), None, self._on_attach_chosen)

    def _on_attach_chosen(self, dialog, result) -> None:
        try:
            files = dialog.open_multiple_finish(result)
        except GLib.Error:
            return  # cancelled
        picked, total, skipped = list(self._attachments), sum(len(a["data"]) * 3 // 4 for a in self._attachments), []
        for gfile in files:
            try:
                ok, data, _etag = gfile.load_contents(None)
            except GLib.Error:
                skipped.append(gfile.get_basename())
                continue
            if total + len(data) > self.MAX_ATTACH_BYTES:
                skipped.append(gfile.get_basename())
                continue
            total += len(data)
            name = gfile.get_basename()
            picked.append({"mime": mimetypes.guess_type(name)[0] or "application/octet-stream", "name": name,
                           "data": base64.b64encode(data).decode("ascii")})
        self._set_attachments(picked, "Skipped (over 4 MB): " + ", ".join(skipped) if skipped else "")

    def _set_attachments(self, attachments: list[dict], note: str = "") -> None:
        self._attachments = attachments
        names = ", ".join(a["name"] for a in attachments)
        label = f"📎 {names} — sent as MMS (pictures are shrunk to fit the carrier's limit)" if attachments else ""
        self._attach_label.set_label((label + ("  ·  " if label and note else "") + note).strip())
        self._attach_row.set_visible(bool(attachments or note))

    def mms_status(self, payload: dict) -> None:
        """sms.mms_status from the phone: sending -> sent | failed."""
        request_id = payload.get("request_id")
        for _addr, msg in self._local_mms:
            if msg.get("request_id") == request_id:
                state = payload.get("state")
                msg["status"] = {"sending": "sending", "failed": "failed"}.get(state)
                msg["error"] = payload.get("message") if state == "failed" else None
                self._render_thread()
                self._scroll_to_end()
                return

    def _on_send_clicked(self, _widget) -> None:
        text = self._entry.get_text().strip()
        address = self._to_entry.get_text().strip() if self._to_entry.get_visible() else self._active_address
        if not (text or self._attachments) or not address:
            return
        self._active_address = address
        if self._attachments:
            request_id = uuid.uuid4().hex
            attachments = self._attachments
            if self._on_send_mms(address, text, attachments, request_id) is False:
                return   # not connected: keep the text and the pictures so they can be sent later
            self._set_attachments([])
            msg = {"from_me": True, "text": text, "ts": int(time.time() * 1000), "status": "sending",
                   "request_id": request_id, "mms": True,
                   "attachments": [{"thumb": a["data"], "name": a["name"], "mime": a["mime"]} for a in attachments]}
            self._thread_msgs.append(msg)
            self._local_mms.append((address, msg))
            self._render_thread()
            self._scroll_to_end()
            self._entry.set_text("")
            return
        if "," in address or ";" in address:   # a plain text has one recipient; the phone can't send to a list
            self._set_attachments([], "A text goes to one number. To message several people, attach a picture (sent as MMS).")
            return
        self._set_attachments([])
        if self._on_send(address, text) is False:
            return   # not connected: the text stays in the box
        now = int(time.time() * 1000)   # without a time, a redraw would file the bubble under 1970
        self._thread_msgs.append({"from_me": True, "text": text, "ts": now, "status": "sending"})
        self._append_bubble(True, text, now, "sending")
        self._scroll_to_end()
        self._entry.set_text("")


def _same_number(a: str, b: str) -> bool:
    da = "".join(ch for ch in a if ch.isdigit())
    db = "".join(ch for ch in b if ch.isdigit())
    return bool(da) and bool(db) and (da[-10:] == db[-10:])
