from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

HISTORY_PATH = Path(GLib.get_user_data_dir()) / "phonebridge" / "clipboard_history.json"
MAX_HISTORY = 300


class ClipboardBridge:
    """Watches and sets the desktop clipboard even while PhoneBridge isn't focused, via
    the XWayland helper in clipboard_watcher.py. Falls back to GTK's own clipboard
    (which only reports changes while focused on Wayland) if the helper can't start."""

    def __init__(self, widget: Gtk.Widget, on_changed, on_image_changed=None) -> None:
        self._on_changed = on_changed
        self._on_image_changed = on_image_changed
        self._widget = widget
        self._proc: Gio.Subprocess | None = None
        self._stdin: Gio.OutputStream | None = None
        self._fallback_last: str | None = None
        self._current: str | None = None   # the text we believe is on the clipboard (to clear only our own synced text)
        if not self._start_helper():
            widget.get_clipboard().connect("changed", self._on_gtk_changed)

    def _start_helper(self) -> bool:
        if not os.environ.get("DISPLAY"):
            return False
        launcher = Gio.SubprocessLauncher.new(Gio.SubprocessFlags.STDIN_PIPE | Gio.SubprocessFlags.STDOUT_PIPE)
        launcher.setenv("GDK_BACKEND", "x11", True)
        pkg_parent = str(Path(__file__).resolve().parents[2])
        launcher.setenv("PYTHONPATH", pkg_parent + os.pathsep + os.environ.get("PYTHONPATH", ""), True)
        try:
            self._proc = launcher.spawnv([sys.executable, "-m", "phonebridge.clipboard_watcher"])
        except GLib.Error:
            return False
        self._stdin = self._proc.get_stdin_pipe()
        stdout = Gio.DataInputStream.new(self._proc.get_stdout_pipe())
        self._read(stdout)
        return True

    def _read(self, stream: Gio.DataInputStream) -> None:
        stream.read_line_async(GLib.PRIORITY_DEFAULT, None, self._on_line)

    def _on_line(self, stream: Gio.DataInputStream, result) -> None:
        try:
            line, _ = stream.read_line_finish_utf8(result)
        except GLib.Error:
            line = None
        if line is None:  # helper died: fall back to the in-process clipboard
            self._proc = None
            self._widget.get_clipboard().connect("changed", self._on_gtk_changed)
            return
        try:
            msg = json.loads(line)
        except ValueError:
            msg = {}
        text = msg.get("changed")
        if text:
            self._current = text
            self._on_changed(text)
        elif msg.get("changed_image") and self._on_image_changed:
            self._on_image_changed(msg["changed_image"])
        self._read(stream)

    def _on_gtk_changed(self, clipboard) -> None:
        from .. import privacy

        formats = clipboard.get_formats()
        if any(formats.contain_mime_type(m) for m in privacy.SENSITIVE_CLIPBOARD_MIMES):
            return   # a password manager's copy: not forwarded, not stored
        clipboard.read_text_async(None, self._on_gtk_read)

    def _on_gtk_read(self, clipboard, result) -> None:
        try:
            text = clipboard.read_text_finish(result)
        except GLib.Error:
            return
        if text and text != self._fallback_last:
            self._fallback_last = text
            self._on_changed(text)

    def set_text(self, text: str, expire: bool = False) -> None:
        """Puts text on the clipboard. With expire=True (text that came from the phone) it is removed again after the
        configured time, unless something else was copied in the meantime."""
        self._current = text
        if expire and text:
            from .. import privacy

            seconds = privacy.get("clipboard_expiry_s")
            if seconds > 0:
                GLib.timeout_add_seconds(seconds, self._expire, text)
        self._set_text_now(text)

    def _expire(self, text: str) -> bool:
        if self._current == text:   # still our synced text: nobody copied anything else
            self._current = ""
            self._set_text_now("")
        return False

    def _set_text_now(self, text: str) -> None:
        if self._proc is not None and self._stdin is not None:
            data = (json.dumps({"set": text}) + "\n").encode()
            self._stdin.write_bytes(GLib.Bytes.new(data), None)
            self._stdin.flush(None)
        else:
            self._fallback_last = text
            self._widget.get_clipboard().set(text)

    def set_image(self, b64: str) -> None:
        """Put an image (base64 PNG/JPEG) on the PC clipboard."""
        if self._proc is not None and self._stdin is not None:
            data = (json.dumps({"set_image": b64}) + "\n").encode()
            self._stdin.write_bytes(GLib.Bytes.new(data), None)
            self._stdin.flush(None)

    def stop(self) -> None:
        if self._proc is not None:
            self._proc.force_exit()


class ClipboardPage(Gtk.Box):
    """Everything copied on either device, newest first. Sync is automatic; each entry
    can be copied again on the PC or re-sent to the phone."""

    def __init__(self, on_send_to_phone) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self._on_send_to_phone = on_send_to_phone
        self._history: list[dict] = self._load()
        self._purge_old()
        GLib.timeout_add_seconds(300, lambda: (self._purge_old(), True)[1])   # keep forgetting old entries while the app runs

        header = Gtk.Box(spacing=8, margin_top=12, margin_start=16, margin_end=16)
        header.append(Gtk.Label(label="Clipboard history", xalign=0, hexpand=True, css_classes=["title-3"]))
        header.append(Gtk.Label(label="Sync automatically"))
        self.sync_switch = Gtk.Switch(active=True, valign=Gtk.Align.CENTER)
        header.append(self.sync_switch)
        clear = Gtk.Button(label="Clear history")
        clear.connect("clicked", self._on_clear)
        header.append(clear)
        self.append(header)

        hint = Gtk.Label(
            label="Text you copy on the PC is sent to the phone instantly. Android only lets apps read the "
                  "clipboard while they're on screen, so on the phone, copy then tap \"Send clipboard to PC\" "
                  "in the PhoneBridge notification (or just open the app).",
            wrap=True, xalign=0, css_classes=["dim-label", "caption"], margin_start=16, margin_end=16,
        )
        self.append(hint)

        compose = Gtk.Box(spacing=6, margin_start=16, margin_end=16)
        self._entry = Gtk.Entry(placeholder_text="Type text to put on the phone's clipboard…", hexpand=True)
        self._entry.connect("activate", self._on_compose)
        send = Gtk.Button(label="Send to phone", css_classes=["suggested-action"])
        send.connect("clicked", self._on_compose)
        compose.append(self._entry)
        compose.append(send)
        self.append(compose)

        self._list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"])
        self._list.set_placeholder(Gtk.Label(label="Nothing copied yet", margin_top=24, margin_bottom=24,
                                             css_classes=["dim-label"]))
        clamp = Adw.Clamp(maximum_size=900, margin_start=16, margin_end=16, margin_bottom=16)
        clamp.set_child(self._list)
        scroll = Gtk.ScrolledWindow(vexpand=True)
        scroll.set_child(clamp)
        self.append(scroll)
        for item in self._history:
            self._list.append(self._row(item))

    # --- public ---

    def _purge_old(self) -> None:
        """Forgets history entries older than the configured retention (privacy setting, default 24 h)."""
        from .. import privacy

        hours = privacy.get("clipboard_history_hours")
        if hours <= 0:
            return
        cutoff = time.time() - hours * 3600
        kept = [h for h in self._history if h.get("ts", 0) >= cutoff]
        if len(kept) != len(self._history):
            self._history = kept
            if hasattr(self, "_list"):
                self._rebuild()
            self._save()

    def add(self, text: str, source: str) -> None:
        """source: "phone" or "pc"."""
        if self._history and self._history[0]["text"] == text:
            return
        self._history = [h for h in self._history if h["text"] != text]
        item = {"text": text, "source": source, "ts": time.time()}
        self._history.insert(0, item)
        del self._history[MAX_HISTORY:]
        self._rebuild()
        self._save()

    def set_remote_text(self, text: str) -> None:
        self.add(text, "phone")

    # --- UI ---

    def _rebuild(self) -> None:
        while (child := self._list.get_first_child()) is not None:
            self._list.remove(child)
        for item in self._history:
            self._list.append(self._row(item))

    def _row(self, item: dict) -> Gtk.Widget:
        text = item["text"]
        preview = " ".join(text.split())
        preview = preview if len(preview) <= 160 else preview[:157] + "…"
        when = time.strftime("%d %b %H:%M", time.localtime(item.get("ts", 0)))
        src = "From phone" if item.get("source") == "phone" else "From PC"
        row = Adw.ActionRow(title=GLib.markup_escape_text(preview), subtitle=f"{src} · {when} · {len(text)} chars")
        row.set_title_lines(3)
        row.add_prefix(Gtk.Image.new_from_icon_name("phone-symbolic" if item.get("source") == "phone" else "computer-symbolic"))
        copy = Gtk.Button(icon_name="edit-copy-symbolic", tooltip_text="Copy on PC", valign=Gtk.Align.CENTER,
                          css_classes=["flat"])
        copy.connect("clicked", lambda _b: self.get_root().copy_to_pc(text))
        send = Gtk.Button(icon_name="send-to-symbolic", tooltip_text="Send to phone", valign=Gtk.Align.CENTER,
                          css_classes=["flat"])
        send.connect("clicked", lambda _b: self._on_send_to_phone(text))
        delete = Gtk.Button(icon_name="user-trash-symbolic", tooltip_text="Remove", valign=Gtk.Align.CENTER,
                            css_classes=["flat"])
        delete.connect("clicked", lambda _b: self._remove(item))
        for b in (copy, send, delete):
            row.add_suffix(b)
        return row

    def _remove(self, item: dict) -> None:
        self._history = [h for h in self._history if h is not item]
        self._rebuild()
        self._save()

    def _on_clear(self, _b) -> None:
        self._history = []
        self._rebuild()
        self._save()

    def _on_compose(self, _w) -> None:
        text = self._entry.get_text()
        if text:
            self._on_send_to_phone(text)
            self.add(text, "pc")
            self._entry.set_text("")

    # --- persistence ---

    @staticmethod
    def _load() -> list[dict]:
        try:
            data = json.loads(HISTORY_PATH.read_text())
            return [d for d in data if isinstance(d, dict) and d.get("text")][:MAX_HISTORY]
        except (OSError, ValueError):
            return []

    def _save(self) -> None:
        try:
            HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
            HISTORY_PATH.write_text(json.dumps(self._history))
            HISTORY_PATH.chmod(0o600)
        except OSError:
            pass
