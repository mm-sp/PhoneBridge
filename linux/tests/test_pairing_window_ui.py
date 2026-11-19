#!/usr/bin/env python3
"""The Pair dialog swaps in a fresh QR code/PIN when the current one is spent (one-minute life, single use) and keeps the
countdown honest. Needs a display or xvfb-run: `xvfb-run -a python3 tests/test_pairing_window_ui.py`"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
tmp = Path(tempfile.mkdtemp())
os.environ["HOME"] = str(tmp)

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib  # noqa: E402

from phonebridge import pairing, tls_identity, wifi_pairing  # noqa: E402
from phonebridge.ui.pairing_window import PairingWindow  # noqa: E402

PASS = FAIL = 0


def check(label, cond):
    global PASS, FAIL
    PASS, FAIL = (PASS + 1, FAIL) if cond else (PASS, FAIL + 1)
    print(f"  {'ok ' if cond else 'FAIL'} {label}")


class FakeServer:
    port = 28100
    cert_fingerprint = "ab" * 32


def main():
    pairing.DEVICES_DIR = tmp / "devices"
    pairing.PAIRING_TTL_SECONDS = 2          # shrink the minute so the test is quick
    wifi_pairing.PAIR_PORT = 28192
    app = Adw.Application(application_id="dev.phonebridge.pairtest")
    seen = []

    def run(_a):
        host = Adw.ApplicationWindow(application=app)
        host.present()
        win = PairingWindow(host, on_paired=lambda d: None, server=FakeServer())
        win.present()

        state = {"first": None, "t": 0}

        def poll():
            state["t"] += 0.25
            p = win._pending
            if state["first"] is None:
                if p is not None and win._pin_label.get_label():
                    state["first"] = (p, p.token, win._pin_label.get_label(), win._timer_label.get_label())
                elif state["t"] > 20:
                    check("a code was shown at all", False); app.quit(); return False
                return True
            p1, t1, pin1, timer1 = state["first"]
            if p is p1 and state["t"] < 30:
                return True            # not replaced yet (2 s life + up to 1 s tick)
            pin2 = win._pin_label.get_label()
            check("a code is shown with a countdown", bool(pin1) and "refreshes in" in timer1)
            check("when the code expires a NEW one replaces it", p is not p1 and p.token != t1)
            check("the displayed PIN changed with it", pin1 != pin2 and p.token in pin2.replace(" ", ""))
            check("the expired code can no longer pair anyone", p1.is_spent())
            check("the new code is fresh", not p.is_spent())
            win.close(); app.quit()
            return False

        GLib.timeout_add(250, poll)

    app.connect("activate", run)
    app.run([])
    print(f"\n{PASS} passed, {FAIL} failed")
    shutil.rmtree(tmp, ignore_errors=True)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
