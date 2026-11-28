from __future__ import annotations

import posixpath
import time
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from .. import file_transfer
from .device_page import human_bytes
from .loading import LoadingBar, LoadingMixin

# File types that are safe to open without asking. HTML, SVG, scripts, desktop entries, archives, office macros etc. are not.
SAFE_AUTO_OPEN = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".heic", ".txt", ".pdf", ".mp3", ".m4a", ".wav",
                            ".ogg", ".flac", ".mp4", ".mkv", ".mov", ".webm"})
DOWNLOADS_DIR = Path.home() / "Downloads" / "PhoneBridge"
REQUESTING = "Requesting from phone…"

_ICON_BY_EXT = {
    **dict.fromkeys(("jpg", "jpeg", "png", "gif", "webp", "heic", "bmp"), "image-x-generic-symbolic"),
    **dict.fromkeys(("mp4", "mkv", "3gp", "webm", "mov", "avi"), "video-x-generic-symbolic"),
    **dict.fromkeys(("mp3", "m4a", "aac", "ogg", "opus", "wav", "flac", "amr"), "audio-x-generic-symbolic"),
    **dict.fromkeys(("pdf", "doc", "docx", "odt", "txt", "xls", "xlsx", "ppt", "pptx"), "x-office-document-symbolic"),
    **dict.fromkeys(("zip", "rar", "7z", "tar", "gz"), "package-x-generic-symbolic"),
    "apk": "application-x-executable-symbolic",
}


class FilesPage(Gtk.Box, LoadingMixin):
    """Browse the phone's storage, download any file, and send PC files to the phone
    (button or drag-and-drop) into the folder currently shown."""

    def __init__(self, server, net_thread, on_list, on_get) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self._server = server
        self._net_thread = net_thread
        self._on_list = on_list
        self._on_get = on_get
        self._path = "/"
        self._loaded = False
        self._open_when_saved: set[str] = set()
        self._transfer_rows: dict[str, tuple[Adw.ActionRow, Gtk.ProgressBar]] = {}
        self._cancel_buttons: dict[str, Gtk.Button] = {}
        self._cancel_handlers: dict[str, int] = {}
        self._sent_rows: dict[str, str] = {}   # PC->phone transfer id -> row key

        self._stack = Adw.ViewStack(vexpand=True)
        self._stack.add_titled_with_icon(self._build_browser(), "phone", "Phone storage", "phone-symbolic")
        self._stack.add_titled_with_icon(self._build_transfers(), "transfers", "Transfers", "folder-download-symbolic")
        switcher = Adw.ViewSwitcher(stack=self._stack, policy=Adw.ViewSwitcherPolicy.WIDE, halign=Gtk.Align.CENTER,
                                    margin_top=12, margin_bottom=6)
        self.append(switcher)
        self.append(self._stack)

        drop_target = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        drop_target.connect("drop", self._on_drop)
        self.add_controller(drop_target)

    # ---------------------------------------------------------------- browser

    def _build_browser(self) -> Gtk.Widget:
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        bar = Gtk.Box(spacing=6, margin_start=16, margin_end=16)
        up = Gtk.Button(icon_name="go-up-symbolic", tooltip_text="Up one folder")
        up.connect("clicked", lambda _b: self.navigate(posixpath.dirname(self._path.rstrip("/")) or "/"))
        home = Gtk.Button(icon_name="go-home-symbolic", tooltip_text="Phone storage root")
        home.connect("clicked", lambda _b: self.navigate("/"))
        self._path_label = Gtk.Label(xalign=0, hexpand=True, ellipsize=3, css_classes=["heading"])
        refresh = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Refresh")
        refresh.connect("clicked", lambda _b: self.navigate(self._path))
        send = Gtk.Button(label="Send files here…", css_classes=["suggested-action"])
        send.connect("clicked", self._on_send_clicked)
        for w in (up, home, self._path_label, refresh, send):
            bar.append(w)
        outer.append(bar)

        quick = Gtk.Box(spacing=6, margin_start=16, margin_end=16)
        for label, path in (("Downloads", "/Download"), ("Camera", "/DCIM/Camera"), ("Pictures", "/Pictures"),
                            ("Documents", "/Documents"), ("WhatsApp", "/WhatsApp/Media"), ("Music", "/Music")):
            b = Gtk.Button(label=label, css_classes=["flat", "caption"])
            b.connect("clicked", lambda _b, p=path: self.navigate(p))
            quick.append(b)
        outer.append(quick)
        self._loading_bars = {"files": LoadingBar("files", margin_start=16, margin_end=16)}
        outer.append(self._loading_bars["files"])

        self._file_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"])
        self._file_list.connect("row-activated", self._on_row_activated)
        self._placeholder = Gtk.Label(label="Connect your phone to browse its files", margin_top=24, margin_bottom=24,
                                      css_classes=["dim-label"], wrap=True)
        self._file_list.set_placeholder(self._placeholder)
        clamp = Adw.Clamp(maximum_size=900, margin_start=16, margin_end=16, margin_bottom=8)
        clamp.set_child(self._file_list)
        scroll = Gtk.ScrolledWindow(vexpand=True)
        scroll.set_child(clamp)
        outer.append(scroll)
        self._free_label = Gtk.Label(css_classes=["caption", "dim-label"], margin_bottom=8)
        outer.append(self._free_label)
        return outer

    def on_shown(self) -> None:
        if not self._loaded:
            self.navigate(self._path)

    def reset(self) -> None:
        self._loaded = False

    def forget_phone(self) -> None:
        """Another phone takes over: back to the top of its storage. The transfer list stays (it is this PC's history)."""
        self._path = "/"
        self._loaded = False
        self._open_when_saved.clear()
        self._clear_listing()
        self._path_label.set_label("Phone")
        self._free_label.set_label("")
        self._placeholder.set_label("Nothing loaded from this phone yet")

    def _clear_listing(self) -> None:
        # Rows only: get_first_child() also returns the placeholder, and removing that loses the "empty" and error texts.
        while (row := self._file_list.get_row_at_index(0)) is not None:
            self._file_list.remove(row)

    def navigate(self, path: str) -> None:
        self._path = path or "/"
        self._path_label.set_label(f"Phone{self._path if self._path != '/' else ''}")
        self._placeholder.set_label("Loading…")
        self._on_list(self._path)

    def after_refusal(self, _what: str) -> None:
        self._loaded = False
        self._clear_listing()   # the entries shown belong to another folder than the path above them
        self._placeholder.set_label("Browsing the phone's files isn't allowed (see the note above)")

    def download_refused(self, phone_path: str | None, message: str) -> None:
        """files.get was refused. Older phone apps don't say which file: then every request still unanswered failed."""
        if phone_path:
            keys = [f"get:{phone_path}"]
        else:
            keys = [k for k, (row, _bar) in self._transfer_rows.items()
                    if k.startswith("get:") and row.get_subtitle() == REQUESTING]
        for key in keys:
            self._open_when_saved.discard(key[len("get:"):])
            self._update_transfer(key, f"Failed: {message}", None)

    def request_failed(self, phone_path: str, message: str) -> None:
        """The phone couldn't send a file asked for here. A transfer that has started reports itself (and a cancelled one
        stays cancelled), so only a row still waiting changes."""
        key = f"get:{phone_path}"
        row = self._transfer_rows.get(key)
        if row is not None and row[0].get_subtitle() == REQUESTING:
            self.download_refused(phone_path, message)

    def set_listing(self, payload: dict) -> None:
        # Only the folder this page asked for: the mounted phone folder (phone_fs.py) receives listings too, and a late
        # answer for a folder already left must not pull the view back.
        path = payload.get("path") or "/"
        if posixpath.normpath("/" + path.lstrip("/")) != posixpath.normpath("/" + self._path.lstrip("/")):
            return
        self._loaded = True
        self._path = path
        self._path_label.set_label(f"Phone{self._path if self._path != '/' else ''}")
        self._clear_listing()
        self._placeholder.set_label(payload.get("error") or "This folder is empty")
        for e in payload.get("entries", []):
            name = e.get("name", "")
            full = posixpath.join(self._path, name)
            if e.get("is_dir"):
                row = Adw.ActionRow(title=GLib.markup_escape_text(name), subtitle=f"{e.get('size', 0)} items",
                                    activatable=True)
                row.add_prefix(Gtk.Image.new_from_icon_name("folder-symbolic"))
                row.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
            else:
                ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
                when = time.strftime("%d %b %Y %H:%M", time.localtime((e.get("mtime") or 0) / 1000))
                row = Adw.ActionRow(title=GLib.markup_escape_text(name),
                                    subtitle=f"{human_bytes(e.get('size'))} · {when}", activatable=True)
                row.add_prefix(Gtk.Image.new_from_icon_name(_ICON_BY_EXT.get(ext, "text-x-generic-symbolic")))
                dl = Gtk.Button(icon_name="folder-download-symbolic", tooltip_text="Download to PC",
                                valign=Gtk.Align.CENTER, css_classes=["flat"])
                dl.connect("clicked", lambda _b, p=full: self._download(p, open_after=False))
                row.add_suffix(dl)
            row._entry = (full, bool(e.get("is_dir")))
            self._file_list.append(row)
        if payload.get("total"):
            self._free_label.set_label(f"{human_bytes(payload.get('free'))} free of {human_bytes(payload.get('total'))}")

    def _on_row_activated(self, _list, row) -> None:
        full, is_dir = getattr(row, "_entry", ("", False))
        if is_dir:
            self.navigate(full)
        elif full:
            self._download(full, open_after=True)

    def _download(self, phone_path: str, open_after: bool) -> None:
        name = posixpath.basename(phone_path)
        if open_after:
            self._open_when_saved.add(phone_path)
        self._transfer_row(f"get:{phone_path}", f"{name}", REQUESTING, incoming=True)
        self._on_get(phone_path)

    # ---------------------------------------------------------------- transfers

    def _build_transfers(self) -> Gtk.Widget:
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        bar = Gtk.Box(spacing=6, margin_start=16, margin_end=16)
        bar.append(Gtk.Label(label="Drop files anywhere on this tab to send them to the phone.", xalign=0,
                             hexpand=True, css_classes=["dim-label"], wrap=True))
        open_dir = Gtk.Button(label="Open downloads folder", tooltip_text=str(DOWNLOADS_DIR))
        open_dir.connect("clicked", lambda _b: (DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True),
                                                 Gio.AppInfo.launch_default_for_uri(DOWNLOADS_DIR.as_uri(), None)))
        bar.append(open_dir)
        outer.append(bar)
        self._transfers = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"])
        self._transfers.set_placeholder(Gtk.Label(label="No transfers yet", margin_top=24, margin_bottom=24,
                                                  css_classes=["dim-label"]))
        clamp = Adw.Clamp(maximum_size=900, margin_start=16, margin_end=16, margin_bottom=16)
        clamp.set_child(self._transfers)
        scroll = Gtk.ScrolledWindow(vexpand=True)
        scroll.set_child(clamp)
        outer.append(scroll)
        return outer

    def _transfer_row(self, key: str, title: str, subtitle: str, incoming: bool) -> None:
        if key in self._transfer_rows:
            self._transfer_rows[key][0].set_subtitle(subtitle)
            return
        row = Adw.ActionRow(title=GLib.markup_escape_text(title), subtitle=subtitle)
        row.add_prefix(Gtk.Image.new_from_icon_name("go-down-symbolic" if incoming else "go-up-symbolic"))
        bar = Gtk.ProgressBar(valign=Gtk.Align.CENTER, width_request=140)
        row.add_suffix(bar)
        self._transfers.prepend(row)
        self._transfer_rows[key] = (row, bar)
        cancel = Gtk.Button(label="Cancel", valign=Gtk.Align.CENTER, css_classes=["flat"])
        cancel.set_visible(False)   # shown once we know which transfer this row is (see _bind_cancel)
        row.add_suffix(cancel)
        self._cancel_buttons[key] = cancel

    def _bind_cancel(self, key: str, fingerprint: str, transfer_id: str) -> None:
        """Gives a row a working Cancel button: stops the stream, closes the data link, deletes the partial download and
        tells the phone (file_transfer.cancel_transfer)."""
        btn = self._cancel_buttons.get(key)
        if btn is None:
            return

        def clicked(_b) -> None:
            async def go() -> None:
                conn = self._server.get_connection(fingerprint)
                if conn is not None:
                    await file_transfer.cancel_transfer(conn, transfer_id)
            self._net_thread.run_coro(go())
            btn.set_visible(False)
            self._update_transfer(key, "Cancelled", None)

        previous = self._cancel_handlers.pop(key, None)
        if previous is not None:
            btn.disconnect(previous)   # the row is used again: Cancel is for the new transfer only
        self._cancel_handlers[key] = btn.connect("clicked", clicked)
        btn.set_visible(True)

    def _update_transfer(self, key: str, subtitle: str | None = None, fraction: float | None = None) -> bool:
        entry = self._transfer_rows.get(key)
        if entry is not None:
            if subtitle is not None:
                entry[0].set_subtitle(subtitle)
                if subtitle.startswith(("Saved", "Failed", "Sent", "Cancelled")) and key in self._cancel_buttons:
                    self._cancel_buttons[key].set_visible(False)   # finished: nothing left to cancel
            if fraction is not None:
                entry[1].set_fraction(fraction)
        return False

    def on_receive_progress(self, payload: dict, received: int, total: int) -> None:
        key = self._key_for_incoming(payload)
        self._transfer_row(key, payload.get("filename", "file"), "Receiving…", incoming=True)
        pct = received / total if total else 1.0
        self._update_transfer(key, f"Receiving… {int(pct * 100)}% of {human_bytes(total)}", pct)
        fp = payload.get("_fingerprint")
        if fp and payload.get("transfer_id") and not self._cancel_buttons[key].get_visible():
            self._bind_cancel(key, fp, str(payload["transfer_id"]))

    def on_file_received(self, payload: dict, dest_path: Path) -> None:
        key = self._key_for_incoming(payload)
        self._transfer_row(key, payload.get("filename", dest_path.name), "", incoming=True)
        self._update_transfer(key, f"Saved to {dest_path}", 1.0)
        row = self._transfer_rows[key][0]
        if getattr(row, "_saved_path", None) is None:
            open_btn = Gtk.Button(label="Open", valign=Gtk.Align.CENTER)
            open_btn.connect("clicked", lambda _b, r=row: Gio.AppInfo.launch_default_for_uri(r._saved_path.as_uri(), None))
            row.add_suffix(open_btn)
        row._saved_path = dest_path   # downloading a file again saves a new copy: "Open" means the latest one
        src = payload.get("source_path")
        if src in self._open_when_saved:
            self._open_when_saved.discard(src)
            # Only open by itself what is inert (a picture, document or media file). The file name came from the phone, so
            # an HTML/SVG/script/desktop file must wait for an explicit click on "Open".
            if dest_path.suffix.lower() in SAFE_AUTO_OPEN:
                Gio.AppInfo.launch_default_for_uri(dest_path.as_uri(), None)
            else:
                self._update_transfer(key, f"Saved to {dest_path} (not opened automatically: click Open)", 1.0)

    def on_receive_failed(self, payload: dict, reason: str) -> None:
        """A file from the phone won't arrive: its row must not stay "Requesting" or "Receiving"."""
        key = self._key_for_incoming(payload)
        self._open_when_saved.discard(payload.get("source_path"))
        self._transfer_row(key, payload.get("filename", "file"), "", incoming=True)
        self._update_transfer(key, "Cancelled" if reason == "cancelled" else f"Failed: {reason}", None)

    @staticmethod
    def _key_for_incoming(payload: dict) -> str:
        src = payload.get("source_path")
        return f"get:{src}" if src else f"in:{payload.get('transfer_id')}"

    def on_phone_saved(self, payload: dict) -> None:
        """Phone confirmed a PC->phone transfer landed (file.saved). Matched by transfer id: the phone renames a file
        whose name is taken ("name (1).ext"). Without a known id, by the exact file name."""
        key = self._sent_rows.get(str(payload.get("transfer_id", "")))
        keys = [key] if key else [k for k in self._transfer_rows
                                  if k.startswith("out:") and k.split(":", 2)[2] == payload.get("filename")]
        for key in keys:
            self._update_transfer(key, f"Saved on phone: {payload.get('path', '')}", 1.0)
        if self._loaded:
            self.navigate(self._path)

    def on_phone_failed(self, transfer_id: str, message: str) -> None:
        """The phone couldn't store a file sent from here (it may have received all of it already)."""
        key = self._sent_rows.get(transfer_id)
        if key is not None:
            self._update_transfer(key, f"Failed: {message}", None)

    def _on_offered(self, key: str, fingerprint: str, transfer_id: str) -> None:
        """A PC->phone transfer got its id."""
        self._sent_rows[transfer_id] = key
        self._bind_cancel(key, fingerprint, transfer_id)

    def _on_send_clicked(self, _button) -> None:
        dialog = Gtk.FileDialog(title="Send files to phone")
        dialog.open_multiple(self.get_root(), None, self._on_files_chosen)

    def _on_files_chosen(self, dialog: Gtk.FileDialog, result) -> None:
        try:
            files = dialog.open_multiple_finish(result)
        except GLib.Error:
            return
        self._send_paths([Path(f.get_path()) for f in files if f.get_path()])

    def _on_drop(self, _target: Gtk.DropTarget, value: Gdk.FileList, _x: float, _y: float) -> bool:
        paths = [Path(f.get_path()) for f in value.get_files() if f.get_path()]
        self._send_paths(paths)
        return bool(paths)

    def _send_paths(self, paths: list[Path]) -> None:
        fingerprints = self._server.connected_fingerprints()
        if not fingerprints:
            self._transfer_row("no-device", "No phone connected", "Open PhoneBridge on the phone first", incoming=False)
            self._stack.set_visible_child_name("transfers")
            return
        dest = self._path if self._path != "/" else "/Download/PhoneBridge"
        for path in paths:
            if path.is_file():
                self._send_one(fingerprints[0], path, dest)
        self._stack.set_visible_child_name("transfers")

    def _mark_sent(self, key: str) -> bool:
        entry = self._transfer_rows.get(key)
        # The phone's "saved" (or "failed") notice travels separately and may already have arrived.
        if entry is not None and not (entry[0].get_subtitle() or "").startswith(("Saved", "Failed")):
            self._update_transfer(key, "Sent — waiting for phone to save…", 1.0)
        return False

    def _send_one(self, fingerprint: str, path: Path, dest: str) -> None:
        row_key = f"out:{time.time()}:{path.name}"
        self._transfer_row(row_key, path.name, f"Sending to phone {dest}…", incoming=False)

        def on_progress(sent: int, total: int) -> None:
            pct = sent / total if total else 1.0
            GLib.idle_add(self._update_transfer, row_key, f"Sending… {int(pct * 100)}% of {human_bytes(total)}", pct)

        async def do_send() -> None:
            conn = self._server.get_connection(fingerprint)
            if conn is None:
                GLib.idle_add(self._update_transfer, row_key, "Failed: phone disconnected", None)
                return
            try:
                await file_transfer.send_file(
                    conn, path, on_progress=on_progress, extra_offer_fields={"dest_path": dest},
                    on_offer=lambda tid: GLib.idle_add(self._on_offered, row_key, fingerprint, tid))
                GLib.idle_add(self._mark_sent, row_key)
            except Exception as exc:  # noqa: BLE001
                GLib.idle_add(self._update_transfer, row_key, f"Failed: {exc}", None)

        self._net_thread.run_coro(do_send())
