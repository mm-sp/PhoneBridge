#!/usr/bin/env python3
"""The phone can refuse a request (permission.denied), or the link can be missing or drop: whatever on the PC was waiting
must stop waiting and say so. Runs the real MainWindow with a server that has no sockets.
Needs a display: `xvfb-run -a python3 tests/test_phone_refusals_ui.py`"""
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
from gi.repository import Adw, Gdk, GLib, Gtk  # noqa: E402

Gtk.init()
if Gdk.Display.get_default() is None:
    print("SKIP: no display (run under xvfb-run)")
    raise SystemExit(0)
Adw.init()

from phonebridge import app as app_module  # noqa: E402
from phonebridge import call_audio, pairing  # noqa: E402

pairing.DEVICES_DIR = tmp / "devices"
from phonebridge.net_thread import NetThread  # noqa: E402
from phonebridge.security import permissions  # noqa: E402
from phonebridge.transport import ControlChannelServer  # noqa: E402
from phonebridge.ui.main_window import MainWindow  # noqa: E402

PASS = FAIL = 0
FP = "2222222222222222"


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
    """Records what would be sent; `linked` stands for a connected phone."""

    def __init__(self) -> None:
        super().__init__(port=0)
        self.linked = True
        self.sent: list[tuple[str, dict]] = []

    def connected_fingerprints(self):
        return [FP] if self.linked else []

    async def send(self, fingerprint, ch, type_, payload):
        self.sent.append((type_, payload))
        return True


pairing.PairedDevice(fingerprint=FP, name="Pixel", session_key=b"k" * 32).save()
net = NetThread()
net.start()
app = Adw.Application(application_id="dev.phonebridge.refusalstest")
app.register(None)
server = Server()
win = MainWindow(app, server, net)
toasts: list[str] = []
win.toast = toasts.append
calls, messages, photos, files = win._calls_page, win._messages_page, win._photos_page, win._files_page


def sent(type_):
    pump()
    return [p for t, p in server.sent if t == type_]


def refuse(type_, feature, **extra):
    win.on_phone_message(FP, "permission.denied", {"type": type_, "feature": feature, **extra})
    pump()


def bar(page, what):
    b = page._loading_bars[what]
    return b.get_reveal_child(), b._label.get_label(), b._timeout_id


print("1. connecting")
check("the app passes the phone's refusals to the window", "permission.denied" in app_module.PHONE_MESSAGE_TYPES)
win.on_connection_changed(FP, True, "192.168.1.5")
pump()
check("connecting loads texts, calls and contacts", all(sent(t) for t in ("sms.list_threads", "calls.history_request",
                                                                           "contacts.list_request")))
check("…without asking for the screen state (it needs the phone's remote-control permission)",
      not sent("screen.state_request"))

print("2. refused loading")
refuse("calls.history_request", "calls")
shown, text, timer = bar(calls, "calls.history")
check("the call history bar names the phone permission", shown and "Calls and contacts" in text and "didn't allow" in text)
check("…and stops waiting", timer == 0)
check("…and the tab asks again when reopened", not calls._loaded)
refuse("sms.list_threads", "sms")
check("the conversations bar says so too", "Messages (read)" in bar(messages, "sms.threads")[1])
check("…and refresh stays usable", messages._more_threads_btn.get_sensitive() and not messages._loaded)
calls.set_contacts({"offset": 0, "contacts": [{"name": "A", "numbers": [{"number": "1", "label": ""}]}], "has_more": True})
calls._loaded = True
calls._more_contacts_btn.set_sensitive(False)
refuse("contacts.list_request", "calls")
check("a refused further page re-enables its button and keeps the tab's data",
      calls._more_contacts_btn.get_sensitive() and calls._loaded and len(calls._contacts) == 1)
win._send("photos.list", {"offset": 0, "limit": 50})
photos._loading = True
photos._spinner.set_visible(True)
refuse("photos.list", "photos")
check("photos: no spinner left, and a new load is possible", not photos._spinner.get_visible() and not photos._loading)
files.set_listing({"path": "/", "entries": [{"name": "DCIM", "is_dir": True, "size": 1}]})
files.navigate("/Download")
refuse("files.list", "files")
check("files: another folder's entries don't stay under the new path",
      files._file_list.get_row_at_index(0) is None and "isn't allowed" in files._placeholder.get_label())
before = len(toasts)
refuse("screen.state_request", "remote_control")
check("a refused background request stays quiet", len(toasts) == before)

print("3. refused messages")
messages.open_compose("5550100", "Sam")
messages._entry.set_text("hello")
messages._on_send_clicked(None)
bubble = messages._thread_msgs[-1]
check("a sent text starts as 'sending', with today's time", bubble["status"] == "sending" and bubble["ts"] > 1e12)
messages._entry.set_text("second")
messages._on_send_clicked(None)
refuse("sms.send", "sms_send")
check("the refused (oldest) text is marked failed", bubble["status"] == "failed"
      and "Sending messages" in bubble["error"])
check("…the next one is still on its way", messages._thread_msgs[-1]["status"] == "sending")
check("…and a toast says it was not sent", toasts[-1].startswith("Not sent."))
listed = {"id": "7", "from_me": True, "text": "from the phone", "ts": 1, "status": "sending"}
messages._thread_msgs.insert(0, listed)
refuse("sms.send", "sms_send")
check("messages listed by the phone are not touched", listed["status"] == "sending"
      and messages._thread_msgs[-1]["status"] == "failed")
picture = {"mime": "image/png", "name": "a.png", "data": "AAAA"}
messages._set_attachments([picture])
messages._on_send_clicked(None)
first = messages._local_mms[-1][1]
messages._set_attachments([picture])
messages._on_send_clicked(None)
second = messages._local_mms[-1][1]
refuse("sms.send_mms", "sms_send", request_id=second["request_id"])
check("a picture message is matched by its request id", second["status"] == "failed" and first["status"] == "sending")
refuse("sms.send_mms", "sms_send")
check("…without an id, the oldest unanswered one", first["status"] == "failed")

print("4. nothing connected")
server.linked = False
count = len(messages._thread_msgs)
messages._entry.set_text("keep me")
messages._on_send_clicked(None)
check("an unsendable text stays in the box without a bubble",
      messages._entry.get_text() == "keep me" and len(messages._thread_msgs) == count)
messages._set_attachments([picture])
messages._on_send_clicked(None)
check("…and so do its pictures", messages._attachments == [picture] and messages._entry.get_text() == "keep me"
      and len(messages._thread_msgs) == count)
calls._dial("5550100")
check("dialling shows no 'Calling…' banner", not calls._call_box.get_visible())
server.linked = True
messages._on_send_clicked(None)
check("once connected the same message goes out", not messages._attachments and messages._entry.get_text() == "")

messages.open_compose("5550100, 5550101", "")
messages._entry.set_text("hi all")
before = len(sent("sms.send"))
messages._on_send_clicked(None)
check("a plain text to several numbers is not sent; it says why and stays in the box",
      len(sent("sms.send")) == before and messages._entry.get_text() == "hi all"
      and "one number" in messages._attach_label.get_label() and messages._attach_row.get_visible())
messages._to_entry.set_text("5550100")
messages._on_send_clicked(None)
check("…to one number it goes out and the note is gone",
      len(sent("sms.send")) == before + 1 and not messages._attach_row.get_visible())

print("5. refused calls")
calls._dial("5550100")
check("dialling shows the banner", calls._call_box.get_visible())
refuse("call.dial", "call_dial")
check("a refused call removes it", not calls._call_box.get_visible() and toasts[-1].startswith("Not called."))
calls.on_state({"state": "active", "number": "5550111"})
calls._dial("5550100")
refuse("call.dial", "call_dial")
check("…but never the banner of a call in progress", calls._call_box.get_visible())

print("6. the link drops during a call")
calls._toggle_mute()
win.on_connection_changed(FP, False, "")
pump()
check("the call banner goes away", not calls._call_box.get_visible() and calls._call_started is None)
check("…and the next call starts unmuted", not calls._muted and calls._mute_btn.get_label() == "Mute")
win.on_connection_changed(FP, True, "192.168.1.5")
routed: list[tuple[str, str]] = []
call_audio.route_to_pc = lambda name: (routed.append(("pc", name)), (True, ""))[1]
call_audio.restore = lambda name: routed.append(("phone", name))
calls._phone_name = lambda: "Pixel" if server.linked else ""
calls.on_state({"state": "active", "number": "5550111"})
calls._pc_audio_btn.set_active(True)
pump(0.2)
server.linked = False
win.on_connection_changed(FP, False, "")
pump(0.2)
check("with call audio on the PC the audio is left alone", routed == [("pc", "Pixel")] and calls._pc_audio)
check("…and only its switch remains", calls._call_box.get_visible() and calls._pc_audio_btn.get_visible()
      and not calls._end_btn.get_visible() and not calls._mute_btn.get_visible())
calls._pc_audio_btn.set_active(False)
pump(0.2)
check("switching it off restores the phone's audio although disconnected", routed[-1] == ("phone", "Pixel"))
check("…and closes the banner", not calls._call_box.get_visible())
server.linked = True
win.on_connection_changed(FP, True, "192.168.1.5")
pump()

print("7. other refusals")
win._mirror_page._mirroring = True
refuse("mirror.start", "screen")
check("a refused mirror stops waiting for video", not win._mirror_page.is_mirroring()
      and "Screen mirroring and camera" in win._mirror_page._status.get_label())
win._webcam_page._streaming = True
refuse("webcam.start", "screen")
check("a refused camera stops waiting too", not win._webcam_page._streaming
      and "didn't allow" in win._webcam_page._status.get_label())
files._transfer_row("get:/a.txt", "a.txt", "Requesting from phone…", incoming=True)
files._transfer_row("get:/b.txt", "b.txt", "Requesting from phone…", incoming=True)
refuse("files.get", "files", path="/a.txt")
rows = files._transfer_rows
check("the refused download is marked failed", rows["get:/a.txt"][0].get_subtitle().startswith("Failed: ")
      and rows["get:/b.txt"][0].get_subtitle() == "Requesting from phone…")
refuse("files.get", "files")
check("without a path every unanswered download is", rows["get:/b.txt"][0].get_subtitle().startswith("Failed: "))
photos._on_photo_clicked("9", "p.jpg")
refuse("photos.get", "photos")
check("a refused photo download is reported", "didn't allow" in photos._status_row.get_label()
      and not photos._open_when_saved)
before = len(toasts)
for _ in range(20):
    refuse("clipboard.update", "clipboard")
check("repeated refusals of one permission make one toast", len(toasts) == before + 1)
refuse("apps.launch", "apps")
check("…another permission gets its own", len(toasts) == before + 2)
refuse("something.new", "brand_new")
check("an unknown permission is shown by its key", "brand_new" in toasts[-1])

server.sent.clear()
for state in ("error", "stopped"):
    win._webcam_page._streaming = True
    win.on_phone_message(FP, "webcam.status", {"state": state, "message": f"camera {state}"})
    pump()
    check(f"a camera that reports '{state}' ends the PC's view and keeps the reason",
          not win._webcam_page._streaming and win._webcam_page._status.get_label() == f"camera {state}"
          and win._webcam_page._toggle_btn.get_label() == "Start camera" and toasts[-1] == f"camera {state}")
check("…and the phone is told to release it", len(sent("webcam.stop")) == 2)
win._webcam_page._streaming = True
win.on_phone_message(FP, "video.stopped", {"kind": "webcam"})
check("a stop notice alone (sent while switching cameras) leaves the view running", win._webcam_page._streaming)
win._webcam_page._streaming = False

print("8. permissions switched off on this PC")


def deny(feature, mode=permissions.DENY):
    permissions.set_mode(pairing.PairedDevice.load(FP), feature, mode)


server.linked = True
win.on_connection_changed(FP, False, "")
pump()
for feature in ("sms", "files", "photos", "notifications", "apps"):
    deny(feature)
server.sent.clear()
messages._rows.clear()
win.on_connection_changed(FP, True, "192.168.1.5")
pump()
check("a switched-off feature is not requested from the phone", not sent("sms.list_threads") and not sent("notifications.sync"))
check("…others still are", bool(sent("calls.history_request")) and bool(sent("contacts.list_request")))
shown, text, timer = bar(messages, "sms.threads")
check("the tab says it is off on this PC", shown and "SMS / messages" in text and "on this PC" in text and timer == 0)
check("…and asks again when reopened", not messages._loaded)
photos.reload()
check("photos: nothing requested, nothing spinning", not sent("photos.list") and not photos._loading
      and not photos._spinner.get_visible() and "Photo access" in bar(photos, "photos")[1])
files.navigate("/DCIM")
check("files: nothing requested", not sent("files.list") and "File transfer" in bar(files, "files")[1])
win._apps_page._request()
check("apps: nothing requested", not sent("apps.list_request"))
files._download("/c.txt", open_after=True)
check("a download is refused here", not sent("files.get") and not files._open_when_saved
      and files._transfer_rows["get:/c.txt"][0].get_subtitle().startswith("Failed: "))
photos._on_photo_clicked("3", "q.jpg")
check("…and so is a photo", not sent("photos.get") and "Photo access" in photos._status_row.get_label())
for feature in ("sms", "files", "photos"):
    deny(feature, permissions.ALLOW)
deny("files", permissions.ASK)
messages.on_shown()
files.navigate("/DCIM")
check("switched on again (or on Ask) it is requested", bool(sent("sms.list_threads")) and bool(sent("files.list")))

print("9. clipboard sync switched off on this PC")
server.sent.clear()
win._on_local_clipboard_changed("first copy")
check("a copy on the PC goes to the phone", sent("clipboard.update") == [{"text": "first copy"}])
deny("clipboard")
server.sent.clear()
win._on_local_clipboard_changed("secret copy")
win._on_local_image_changed("aW1hZ2U=")
check("with Deny no copied text or picture is sent", not sent("clipboard.update"))
win._send_clipboard_to_phone("explicit")
check("…nor by the send buttons, which say why", not sent("clipboard.update") and "Clipboard sync" in toasts[-1])
deny("clipboard", permissions.ALLOW)
win._on_local_clipboard_changed("later copy")
check("switched on again, only what is copied now is sent", sent("clipboard.update") == [{"text": "later copy"}])

shutil.rmtree(tmp, ignore_errors=True)
print(f"\n{PASS} passed, {FAIL} failed")
raise SystemExit(1 if FAIL or not PASS else 0)
