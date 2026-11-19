#!/usr/bin/env python3
"""Per-app streaming UI wiring (protocol/PROTOCOL.md section 4f): the Apps page's "Stream only the
app" mode drives MirrorPage.start_app, which makes mirror.start carry {"app": pkg}.
Needs a display: `xvfb-run -a python3 tests/test_app_stream_ui.py`"""
import sys
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

from phonebridge.ui.apps_page import AppsPage  # noqa: E402
from phonebridge.ui.mirror_page import MirrorPage  # noqa: E402

PASS = FAIL = 0


def check(label, cond):
    global PASS, FAIL
    PASS, FAIL = (PASS + 1, FAIL) if cond else (PASS, FAIL + 1)
    print(f"  {'ok ' if cond else 'FAIL'} {label}")


started, stopped = [], []
mirror = MirrorPage(on_start=lambda: (started.append(mirror.app), True)[1], on_stop=lambda: stopped.append(1),
                    on_input=lambda p: None, on_send_clipboard=lambda t: None, on_copy_to_pc=lambda t: None,
                    pc_clipboard=lambda: "")
# H264Renderer needs a decoder; the page reports that as a status instead of raising.
calls = []
apps = AppsPage(lambda: None, lambda p: calls.append(("open", p)), lambda p: calls.append(("mirror", p)),
                lambda p, label: (calls.append(("stream", p, label)), mirror.start_app(p, label)))
apps.set_apps([{"pkg": "org.example.notes", "label": "Notes", "icon": ""}])

print("1. apps page modes")
apps._mode.set_selected(AppsPage.OPEN)
apps._launch("org.example.notes")
apps._mode.set_selected(AppsPage.MIRROR)
apps._launch("org.example.notes")
apps._mode.set_selected(AppsPage.STREAM)
apps._launch("org.example.notes")
check("open / mirror / stream route to different callbacks",
      [c[0] for c in calls] == ["open", "mirror", "stream"] and calls[2][2] == "Notes")

print("2. mirror page")
if mirror.is_mirroring():
    check("mirror.start is built with the app package", started == ["org.example.notes"])
    mirror.on_video_started({"kind": "mirror", "app": "org.example.notes", "width": 720, "height": 1280, "input_available": True})
    check("status says which app is streaming", "Notes" in mirror._status.get_label())
    mirror.stop_if_running()
    check("app is cleared when streaming stops", mirror.app is None and stopped == [1])
else:
    check("no H.264 decoder here: start_app leaves no stale app", mirror.app is None)
print(f"\n{PASS} passed, {FAIL} failed")
raise SystemExit(1 if FAIL else 0)
