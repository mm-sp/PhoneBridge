"""Background clipboard access that works while PhoneBridge's window isn't focused.

GNOME on Wayland only reports clipboard changes to the focused window, so the main
(Wayland) app would miss everything copied in other apps. X11 clients under XWayland
still see every change, so this helper runs as a separate process with
GDK_BACKEND=x11 and talks to the app over stdin/stdout, one JSON object per line:

    stdout: {"changed": "<text>"}     whenever the desktop clipboard text changes
    stdout: {"changed_image": "<base64 PNG>"}   when an image (<= 4 MiB) is copied
    stdin:  {"set": "<text>"}         put text on the clipboard (e.g. from the phone)
    stdout: {"replaced": true}          another program copied something that isn't forwarded as text
    stdin:  {"set_image": "<base64 PNG/JPEG>"}  put an image on the clipboard

Run as: python -m phonebridge.clipboard_watcher
"""
from __future__ import annotations

import base64
import json
import sys

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, Gio, GLib, Gtk  # noqa: E402


MAX_IMAGE_BYTES = 4 * 1024 * 1024
SENSITIVE_MIMES = ("x-kde-passwordManagerHint",)   # keep in sync with privacy.SENSITIVE_CLIPBOARD_MIMES


class Watcher:
    def __init__(self) -> None:
        display = Gdk.Display.get_default()
        if display is None:
            raise SystemExit("no display")
        self._clipboard = display.get_clipboard()
        self._last: str | None = None
        self._last_image: str | None = None
        self._clipboard.connect("changed", self._on_changed)

        stdin = Gio.DataInputStream.new(Gio.UnixInputStream.new(sys.stdin.fileno(), False))
        self._read_next(stdin)

    def _on_changed(self, clipboard: Gdk.Clipboard) -> None:
        # Password managers tag what they copy; that must never leave the PC (or be stored in the history).
        formats = clipboard.get_formats()
        if any(formats.contain_mime_type(m) for m in SENSITIVE_MIMES):
            self._report_replaced(clipboard)   # only the fact, never the content
            return
        clipboard.read_text_async(None, self._on_read)

    @staticmethod
    def _report_replaced(clipboard: Gdk.Clipboard) -> None:
        """Another program put something there that isn't reported as text: the app must know, so that expiring the
        phone's text later doesn't wipe it."""
        if not clipboard.is_local():
            print(json.dumps({"replaced": True}), flush=True)

    def _on_read(self, clipboard: Gdk.Clipboard, result) -> None:
        try:
            text = clipboard.read_text_finish(result)
        except GLib.Error:
            self._report_replaced(clipboard)
            self._maybe_read_image(clipboard)  # non-text content: an image, files…
            return
        if text and text != self._last:
            self._last = text
            print(json.dumps({"changed": text}), flush=True)

    def _maybe_read_image(self, clipboard: Gdk.Clipboard) -> None:
        if clipboard.get_formats().contain_mime_type("image/png") or clipboard.get_formats().contain_gtype(Gdk.Texture):
            clipboard.read_texture_async(None, self._on_texture)

    def _on_texture(self, clipboard: Gdk.Clipboard, result) -> None:
        try:
            texture = clipboard.read_texture_finish(result)
            png = texture.save_to_png_bytes().get_data() if texture else b""
        except GLib.Error:
            return
        if not png or len(png) > MAX_IMAGE_BYTES:
            return
        b64 = base64.b64encode(png).decode()
        if b64 != self._last_image:
            self._last_image = b64
            print(json.dumps({"changed_image": b64}), flush=True)

    def _read_next(self, stream: Gio.DataInputStream) -> None:
        stream.read_line_async(GLib.PRIORITY_DEFAULT, None, self._on_line, stream)

    def _on_line(self, stream: Gio.DataInputStream, result, _data=None) -> None:
        line, _length = stream.read_line_finish_utf8(result)
        if line is None:  # parent went away
            loop.quit()
            return
        try:
            msg = json.loads(line)
        except ValueError:
            msg = {}
        if "set" in msg:
            self._last = msg["set"]  # don't report our own write back as a change
            self._clipboard.set(msg["set"])
        elif "set_image" in msg:
            try:
                data = base64.b64decode(msg["set_image"])
                if len(data) <= MAX_IMAGE_BYTES:
                    self._last_image = msg["set_image"]
                    self._clipboard.set(Gdk.Texture.new_from_bytes(GLib.Bytes.new(data)))
            except (ValueError, GLib.Error):
                pass
        self._read_next(stream)


if __name__ == "__main__":
    Gtk.init()
    Watcher()
    loop = GLib.MainLoop()
    loop.run()
