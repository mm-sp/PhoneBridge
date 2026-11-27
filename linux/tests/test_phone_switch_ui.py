#!/usr/bin/env python3
"""When another phone takes over, nothing of the previous one's stays on the tabs (a reply typed for its contact must not
leave from the new phone). Also: the Files tab keeps its "empty"/error text, shows downloads that fail, and opens the
newest copy. Runs the real MainWindow with a server that has no sockets.
Needs a display: `xvfb-run -a python3 tests/test_phone_switch_ui.py`"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
tmp = Path(tempfile.mkdtemp())
os.environ["HOME"] = str(tmp)  # keep settings/pairings out of the real home

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

Gtk.init()
if Gdk.Display.get_default() is None:
    print("SKIP: no display (run under xvfb-run)")
    raise SystemExit(0)
Adw.init()

from phonebridge import pairing  # noqa: E402
from phonebridge.security import keystore  # noqa: E402

pairing.DEVICES_DIR = tmp / "devices"
keystore.DEFAULT_CONFIG_DIR = tmp
from phonebridge.net_thread import NetThread  # noqa: E402
from phonebridge.transport import ControlChannelServer  # noqa: E402
from phonebridge.ui.main_window import MainWindow  # noqa: E402

PASS = FAIL = 0
FIRST, SECOND = "1111111111111111", "2222222222222222"


def check(label, cond):
    global PASS, FAIL
    PASS, FAIL = (PASS + 1, FAIL) if cond else (PASS, FAIL + 1)
    print(f"  {'ok ' if cond else 'FAIL'} {label}")


def pump(seconds: float = 0.05) -> None:
    ctx = GLib.MainContext.default()
    end = GLib.get_monotonic_time() + int(seconds * 1e6)
    while GLib.get_monotonic_time() < end:
        ctx.iteration(False)


class Server(ControlChannelServer):
    """Records what would be sent; `linked` is the connected phone."""

    def __init__(self) -> None:
        super().__init__(port=0)
        self.linked: str | None = None
        self.sent: list[tuple[str, str, dict]] = []

    def connected_fingerprints(self):
        return [self.linked] if self.linked else []

    async def send(self, fingerprint, ch, type_, payload):
        self.sent.append((fingerprint, type_, payload))
        return True


for fp, name in ((FIRST, "First"), (SECOND, "Second")):
    pairing.PairedDevice(fingerprint=fp, name=name, session_key=b"k" * 32).save()
net = NetThread()
net.start()
app = Adw.Application(application_id="dev.phonebridge.switchtest")
app.register(None)
server = Server()
win = MainWindow(app, server, net)
toasts: list[str] = []
win.toast = toasts.append
calls, messages, photos = win._calls_page, win._messages_page, win._photos_page
files, apps = win._files_page, win._apps_page


def connect(fp):
    server.linked = fp
    win.on_connection_changed(fp, True, "192.168.1.5")
    pump()


def disconnect(fp):
    server.linked = None
    win.on_connection_changed(fp, False, "")
    pump()


def rows(listbox) -> int:
    n = 0
    while listbox.get_row_at_index(n) is not None:
        n += 1
    return n


def children(widget) -> int:
    n, child = 0, widget.get_first_child()
    while child is not None:
        n, child = n + 1, child.get_next_sibling()
    return n


def texts(widget) -> list[str]:
    found = [widget.get_label()] if isinstance(widget, Gtk.Label) else []
    child = widget.get_first_child()
    while child is not None:
        found += texts(child)
        child = child.get_next_sibling()
    return found


def titles(widget) -> list[str]:
    """Every title, subtitle and label under `widget`."""
    found = [value for attr in ("get_title", "get_subtitle", "get_label") if hasattr(widget, attr)
             and isinstance(value := getattr(widget, attr)(), str) and value]
    child = widget.get_first_child()
    while child is not None:
        found += titles(child)
        child = child.get_next_sibling()
    return found


def fill(fp):
    win.on_phone_message(fp, "sms.thread_list", {"offset": 0, "total": 1, "threads": [
        {"id": "7", "address": "+15550001", "name": "Alex", "last_text": "see you", "ts": 1000}]})
    messages._open_thread("7")
    win.on_phone_message(fp, "sms.thread_messages", {"thread_id": "7", "address": "+15550001", "name": "Alex", "offset": 0,
                                                     "messages": [{"id": "1", "text": "private words", "ts": 1000}]})
    win.on_phone_message(fp, "calls.history", {"offset": 0, "total": 1, "calls": [{"number": "+15550001", "type": "missed"}]})
    win.on_phone_message(fp, "contacts.list", {"offset": 0, "contacts": [
        {"name": "Alex", "numbers": [{"number": "+15550001", "label": ""}]}]})
    win.on_phone_message(fp, "photos.list_result", {"offset": 0, "total": 1, "photos": [{"id": "1", "name": "a.jpg"}]})
    files.navigate("/Download")
    win.on_phone_message(fp, "files.list_result", {"path": "/Download", "free": 1, "total": 2,
                                                   "entries": [{"name": "secret.pdf", "size": 1}]})
    win.on_phone_message(fp, "apps.list", {"apps": [{"pkg": "a.b", "label": "Bank"}]})
    pump()


print("1. the same phone reconnecting keeps what is on screen")
connect(FIRST)
fill(FIRST)
messages._entry.set_text("half written")
disconnect(FIRST)
connect(FIRST)
check("the open conversation and the draft stay", messages._active_thread_id == "7" and messages._entry.get_text() == "half written"
      and "private words" in texts(messages._messages_box))
check("…and contacts, files and apps stay", len(calls._contacts) == 1 and files._path == "/Download"
      and apps._count.get_label() == "Apps · 1")

print("2. a different phone takes over")
fill(FIRST)
messages._entry.set_text("for Alex")
messages._set_attachments([{"mime": "image/png", "name": "p.png", "data": "AAAA"}])
disconnect(FIRST)
check("a disconnect alone keeps the data", messages._active_thread_id == "7")
server.sent.clear()
connect(SECOND)
check("no conversation of the first phone is listed or open", rows(messages._thread_list) == 0 and not messages._threads
      and messages._active_thread_id is None and "private words" not in texts(messages._messages_box))
check("…the draft and its pictures are gone", messages._entry.get_text() == "" and not messages._attachments)
server.sent.clear()
messages._entry.set_text("hello")
messages._on_send_clicked(None)
pump()
check("…so nothing can be sent to its contact from the new phone", not [t for _f, t, _p in server.sent if t.startswith("sms.send")])
check("calls and contacts are gone", rows(calls._recent_list) == 0 and rows(calls._contact_list) == 0 and not calls._contacts
      and calls._name_for("+15550001") == "")
check("photos are gone", children(photos._flow) == 0 and photos._count.get_label() == "Photos")
check("the files tab is back at the top, empty", files._path == "/" and rows(files._file_list) == 0
      and files._free_label.get_label() == "")
check("apps are gone", children(apps._flow) == 0 and apps._stack.get_visible_child_name() == "empty")
fill(SECOND)
check("the new phone's data fills the tabs", rows(messages._thread_list) == 1 and rows(calls._recent_list) == 1
      and rows(calls._contact_list) == 1 and rows(files._file_list) == 1 and children(photos._flow) == 1)

print("3. an empty or unreadable folder says so (also after another listing)")
placeholder = files._placeholder
for payload, expected in (({"entries": []}, "This folder is empty"), ({"entries": [], "error": "No access"}, "No access")):
    files.navigate("/Download")
    win.on_phone_message(SECOND, "files.list_result", {"path": "/Download", "entries": [{"name": "x", "size": 1}]})
    win.on_phone_message(SECOND, "files.list_result", {"path": "/Download", **payload})
    pump()
    check(f"“{expected}” is shown", placeholder.get_label() == expected and placeholder.get_parent() is files._file_list
          and rows(files._file_list) == 0)


def row_text(key):
    return files._transfer_rows[key][0].get_subtitle()


print("4. a download that fails says so")
files._download("/Download/movie.mp4", open_after=True)
win.on_file_failed({"source_path": "/Download/movie.mp4", "filename": "movie.mp4"}, "the connection to the phone was lost")
check("the row names the reason", row_text("get:/Download/movie.mp4") == "Failed: the connection to the phone was lost")
check("…and it is not opened by itself later", "/Download/movie.mp4" not in files._open_when_saved)
toasts.clear()
upload = {"transfer_id": "t1", "filename": "up.bin", "_fingerprint": SECOND}
win.on_receive_progress(upload, 1, 10)
check("a running upload has a Cancel button", files._cancel_buttons["in:t1"].get_visible())
win.on_file_failed(upload, "the phone stopped sending")
check("…which goes away when it fails", row_text("in:t1").startswith("Failed") and not files._cancel_buttons["in:t1"].get_visible())
check("a file the phone sent by itself also raises a notice", any("didn't arrive" in t for t in toasts))
toasts.clear()
win.on_file_failed({"transfer_id": "t2", "filename": "big.bin"}, "not enough free space on the PC")
check("a refused upload gets a row too", row_text("in:t2") == "Failed: not enough free space on the PC")
toasts.clear()
win.on_file_failed({"transfer_id": "t3", "filename": "c.bin"}, "cancelled")
check("a cancel is shown as such, without a notice", row_text("in:t3") == "Cancelled" and not toasts)
photos._on_photo_clicked("9", "p.jpg")
win.on_file_failed({"photo_id": "9", "filename": "p.jpg"}, "the connection to the phone was lost")
check("a failed photo download is shown on the Photos tab", photos._status_row.get_label().startswith("Download failed")
      and "9" not in photos._open_when_saved)

print("5. downloading the same file again")
opened: list[str] = []
real_launch = Gio.AppInfo.launch_default_for_uri
Gio.AppInfo.launch_default_for_uri = lambda uri, _ctx=None: opened.append(uri)
cancelled: list[str] = []


class Conn:
    fingerprint = SECOND


server.get_connection = lambda _fp: Conn()
from phonebridge import file_transfer  # noqa: E402


async def fake_cancel(_conn, tid, notify=True):
    cancelled.append(tid)
    return True


real_cancel = file_transfer.cancel_transfer
file_transfer.cancel_transfer = fake_cancel
try:
    again = {"source_path": "/Download/a.txt", "filename": "a.txt", "_fingerprint": SECOND}
    key = "get:/Download/a.txt"
    for tid, saved in (("d1", tmp / "a.txt"), ("d2", tmp / "a (1).txt")):
        files._download("/Download/a.txt", open_after=False)
        win.on_receive_progress({**again, "transfer_id": tid}, 1, 10)
        if tid == "d1":
            win.on_file_received(again, saved)
    files._cancel_buttons[key].emit("clicked")
    pump(0.2)
    check("Cancel on a reused row cancels only the running transfer", cancelled == ["d2"])
    win.on_file_received(again, tmp / "a (1).txt")
    box = files._transfer_rows[key][0]
    buttons = []

    def find(widget):
        if isinstance(widget, Gtk.Button) and widget.get_label() == "Open":
            buttons.append(widget)
        child = widget.get_first_child()
        while child is not None:
            find(child)
            child = child.get_next_sibling()

    find(box)
    opened.clear()
    buttons[0].emit("clicked")
    check("there is one Open button and it opens the newest copy", len(buttons) == 1 and opened == [(tmp / "a (1).txt").as_uri()])
finally:
    Gio.AppInfo.launch_default_for_uri = real_launch
    file_transfer.cancel_transfer = real_cancel
    del server.get_connection   # back to the real one

print("6. the Security page is current with or without a phone")
security = win._security_page
disconnect(SECOND)
pairing.PairedDevice(fingerprint="3333333333333333", name="Newly paired", session_key=b"k" * 32).save()
win._select_sidebar("security")
pump()
check("opening it without a phone shows the pairings as they are now", "Newly paired" in titles(security))
connect(SECOND)
check("…a phone connecting while it is open is shown", any(t.startswith("Connected — 2222") for t in titles(security)))
disconnect(SECOND)
check("…and one leaving no longer reads as connected", not any(t.startswith("Connected — ") for t in titles(security)))

print(f"\n{PASS} passed, {FAIL} failed")
shutil.rmtree(tmp, ignore_errors=True)
raise SystemExit(1 if FAIL else 0)
