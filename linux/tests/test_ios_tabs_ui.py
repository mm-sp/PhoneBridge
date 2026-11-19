#!/usr/bin/env python3
"""The real MainWindow hides Messages/Calls/etc. for an iPhone and shows everything again for an Android phone.
Needs a display: `xvfb-run -a python3 tests/test_ios_tabs_ui.py`"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gtk  # noqa: E402

Gtk.init()
if Gdk.Display.get_default() is None:
    print("SKIP: no display (run under xvfb-run)")
    raise SystemExit(0)
Adw.init()

tmp = Path(tempfile.mkdtemp())
os.environ["HOME"] = str(tmp)  # keep settings/pairings out of the real home
from phonebridge import pairing  # noqa: E402

pairing.DEVICES_DIR = tmp / "devices"
from phonebridge.net_thread import NetThread  # noqa: E402
from phonebridge.transport import ControlChannelServer  # noqa: E402
from phonebridge.ui.main_window import SECTIONS, MainWindow  # noqa: E402

PASS = FAIL = 0


def check(label, cond):
    global PASS, FAIL
    PASS, FAIL = (PASS + 1, FAIL) if cond else (PASS, FAIL + 1)
    print(f"  {'ok ' if cond else 'FAIL'} {label}")


iphone = pairing.PairedDevice(fingerprint="1111111111111111", name="iPhone", session_key=b"k" * 32, platform="ios")
pixel = pairing.PairedDevice(fingerprint="2222222222222222", name="Pixel", session_key=b"k" * 32)
iphone.save(); pixel.save()

net = NetThread()
net.start()
app = Adw.Application(application_id="dev.phonebridge.test")
win = MainWindow(app, ControlChannelServer(port=0), net)


def visible():
    return [key for i, (key, _t, _ic) in enumerate(SECTIONS) if win._sidebar_list.get_row_at_index(i).get_visible()]


win._apply_platform(iphone.fingerprint)
check("iPhone keeps only Device, Files and Security", visible() == ["device", "files", "security"])
win._apply_platform(pixel.fingerprint)
check("Android phone gets every tab back", visible() == [k for k, _t, _i in SECTIONS])
win._current_key = "messages"
win._apply_platform(iphone.fingerprint)
check("an open tab the iPhone can't back falls back to Device", win._current_key == "device")

shutil.rmtree(tmp, ignore_errors=True)
print(f"\n{PASS} passed, {FAIL} failed")
raise SystemExit(1 if FAIL else 0)
