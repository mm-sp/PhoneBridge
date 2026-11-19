#!/usr/bin/env python3
"""Clipboard privacy on the PC: text from the phone is cleared after its expiry (only if still ours), old history entries
are forgotten, and password-manager copies are excluded. Run: `xvfb-run -a python3 tests/test_clipboard_privacy_ui.py`"""
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
tmp = Path(tempfile.mkdtemp())
os.environ["HOME"] = str(tmp)
os.environ["XDG_DATA_HOME"] = str(tmp / "data")

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib  # noqa: E402

from phonebridge import pairing, privacy  # noqa: E402
from phonebridge.ui import clipboard_page as cp  # noqa: E402

PASS = FAIL = 0


def check(label, cond):
    global PASS, FAIL
    PASS, FAIL = (PASS + 1, FAIL) if cond else (PASS, FAIL + 1)
    print(f"  {'ok ' if cond else 'FAIL'} {label}")


def main():
    pairing.DEVICES_DIR = tmp / "cfg" / "devices"
    privacy.set_value("clipboard_expiry_s", 1)
    privacy.set_value("clipboard_history_hours", 24)
    # history on disk: one entry from 3 days ago, one fresh
    cp.HISTORY_PATH = tmp / "data" / "history.json"
    cp.HISTORY_PATH.parent.mkdir(parents=True)
    now = time.time()
    cp.HISTORY_PATH.write_text(json.dumps([
        {"text": "old secret", "source": "phone", "ts": now - 3 * 86400},
        {"text": "fresh note", "source": "pc", "ts": now - 60}]))
    app = Adw.Application(application_id="dev.phonebridge.cliptest")

    def run(_a):
        win = Adw.ApplicationWindow(application=app); win.present()
        page = cp.ClipboardPage(on_send_to_phone=lambda t: None)
        win.set_content(page)
        check("history older than the retention is forgotten at start", [h["text"] for h in page._history] == ["fresh note"])
        check("…and it is gone from the file too", "old secret" not in cp.HISTORY_PATH.read_text())
        page._history.append({"text": "stale", "source": "pc", "ts": now - 2 * 86400})
        page._purge_old()
        check("the periodic purge drops entries that aged while the app ran", [h["text"] for h in page._history] == ["fresh note"])
        privacy.set_value("clipboard_history_hours", 0)
        page._history.append({"text": "ancient", "source": "pc", "ts": 1})
        page._purge_old()
        check("retention 0 means keep forever", any(h["text"] == "ancient" for h in page._history))

        bridge = cp.ClipboardBridge.__new__(cp.ClipboardBridge)
        bridge._proc = None; bridge._stdin = None; bridge._current = None; bridge._widget = win
        written = []
        bridge._set_text_now = lambda t: written.append(t)
        bridge.set_text("from-phone-token", expire=True)
        bridge.set_text("typed-by-user-here")   # a user copy with no expiry
        bridge.set_text("another-phone-text", expire=True)
        bridge._current = "something the user copied afterwards"

        def after():
            check("text received from the phone is set immediately", written[:3] == ["from-phone-token", "typed-by-user-here", "another-phone-text"])
            check("after the expiry only still-synced text would be cleared — here the user copied something else, so nothing is wiped",
                  "" not in written)
            bridge2 = cp.ClipboardBridge.__new__(cp.ClipboardBridge)
            bridge2._proc = None; bridge2._stdin = None; bridge2._current = None; bridge2._widget = win
            w2 = []
            bridge2._set_text_now = lambda t: w2.append(t)
            bridge2.set_text("expires-soon", expire=True)
            GLib.timeout_add(2600, lambda: (check("synced text that is still on the clipboard IS cleared after the expiry", w2 == ["expires-soon", ""]), finish())[1])
            return False

        def finish():
            privacy.set_value("clipboard_expiry_s", 0)
            b3 = cp.ClipboardBridge.__new__(cp.ClipboardBridge)
            b3._proc = None; b3._stdin = None; b3._current = None; b3._widget = win
            w3 = []
            b3._set_text_now = lambda t: w3.append(t)
            b3.set_text("never-expires", expire=True)
            GLib.timeout_add(1300, lambda: (check("expiry 0 keeps the text", w3 == ["never-expires"]), app.quit())[1])
            return False
        GLib.timeout_add(1500, after)

    app.connect("activate", run)
    app.run([])
    print(f"\n{PASS} passed, {FAIL} failed")
    shutil.rmtree(tmp, ignore_errors=True)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
