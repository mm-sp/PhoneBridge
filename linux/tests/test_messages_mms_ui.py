"""MessagesPage MMS flow (attach, send_mms callback, sms.mms_status updates). Needs a display: `xvfb-run -a python3 tests/test_messages_mms_ui.py`."""
import base64
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gdk
from gi.repository import Gtk as _Gtk
_Gtk.init()
if Gdk.Display.get_default() is None:
    print("SKIP: no display (run under xvfb-run)")
    raise SystemExit(0)
gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw
Adw.init()
from phonebridge.ui.messages_page import MessagesPage
sent = []
p = MessagesPage(lambda o: None, lambda t, o: None, lambda n, t: sent.append(("sms", n, t)),
                 lambda n, t, a, r: sent.append(("mms", n, t, len(a), r)))
p.open_compose("5550100", "")
p._entry.set_text("hi")
p._attachments = [{"mime": "image/png", "name": "a.png", "data": base64.b64encode(b"notapng").decode()}]
p._on_send_clicked(None)
assert sent[0][0] == "mms" and sent[0][3] == 1, sent
rid = sent[0][4]
p.mms_status({"request_id": rid, "state": "failed", "message": "no data"})
assert p._thread_msgs[-1]["status"] == "failed" and p._thread_msgs[-1]["error"] == "no data"
p.mms_status({"request_id": rid, "state": "sent"})
assert p._thread_msgs[-1]["status"] is None
p._entry.set_text("plain"); p._on_send_clicked(None)
assert sent[1][0] == "sms", sent
print("  ok  attach -> MMS request, status updates, plain SMS still works\n\n1 passed, 0 failed")
