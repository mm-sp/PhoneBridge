#!/usr/bin/env python3
"""PC-side indicator for phone remote control: the Device card shows the minutes left and unticks itself on expiry,
and the main window's banner appears while a phone has control and goes away on revoke.
Needs a display or xvfb-run: `xvfb-run -a python3 tests/test_pc_control_ui.py`"""
import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_TMP = Path(tempfile.mkdtemp())
os.environ["HOME"] = str(_TMP)

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw  # noqa: E402

from phonebridge import pairing  # noqa: E402
from phonebridge.security import permissions as P  # noqa: E402
from phonebridge.ui.device_page import DevicePage, _DeviceCard  # noqa: E402
from phonebridge.ui.main_window import MainWindow  # noqa: E402

PASS = FAIL = 0


def check(label, cond):
    global PASS, FAIL
    PASS, FAIL = (PASS + 1, FAIL) if cond else (PASS, FAIL + 1)
    print(f"  {'ok ' if cond else 'FAIL'} {label}")


pairing.DEVICES_DIR = _TMP / "devices"   # never the real ~/.config/phonebridge
Adw.init()
dev = pairing.PairedDevice(name="Test phone", fingerprint="beef", session_key=os.urandom(32))
dev.save()
banner = Adw.Banner(title="", revealed=False)
fake_window = SimpleNamespace(_control_banner=banner)
refresh = lambda: MainWindow._refresh_control_banner(fake_window)  # noqa: E731

refresh()
check("default: nothing can control the PC, banner hidden", not banner.get_revealed() and not dev.pc_control_active())

card = _DeviceCard(dev, lambda d: None, lambda *a: None, lambda fp: None)
check("checkbox starts unticked", not card._pc_control.get_active())

card._pc_control.set_active(True)   # the person at the PC ticks the box
fresh = pairing.PairedDevice.load(dev.fingerprint)
check("ticking enables control with a deadline of at most one hour", fresh.pc_control_active() and fresh.pc_control_until - time.time() <= 3600 + 2)
check("label shows minutes left", "min left" in card._pc_control.get_label())
refresh()
check("banner appears and names the phone", banner.get_revealed() and "Test phone" in banner.get_title() and "switches off" in banner.get_title())

pairing.PairedDevice.update(dev.fingerprint, pc_control_until=time.time() - 1)   # time runs out
dev.pc_control_until = time.time() - 1
card._update_pc_control_label()
check("expired: checkbox unticks itself", not card._pc_control.get_active())
check("expired: switch cleared on disk", not pairing.PairedDevice.load(dev.fingerprint).allow_pc_control)
refresh()
check("expired: banner hidden", not banner.get_revealed())

P.set_mode(pairing.PairedDevice.load(dev.fingerprint), "remote_control", P.ALLOW)
refresh()
check("allowed again: banner shown", banner.get_revealed())
revoke_self = SimpleNamespace(_control_banner=banner, toast=lambda m: None, _refresh_control_banner=refresh)
MainWindow._revoke_pc_control(revoke_self)
check("Revoke now: control off immediately and banner hidden", not pairing.PairedDevice.load(dev.fingerprint).pc_control_active() and not banner.get_revealed())

# The card follows changes made elsewhere (a request approved in a dialog, "Revoke now", the Security page, expiry).
page = DevicePage(on_pair=lambda: None, on_forget=lambda d: None, on_control=lambda fp: None, on_disconnect=lambda fp: None)
card = page._cards[dev.fingerprint]
window = SimpleNamespace(_control_banner=banner, _device_page=page)
changes: list[str] = []
P.on_change.append(lambda _d, _f, mode: changes.append(mode))
P.set_mode(pairing.PairedDevice.load(dev.fingerprint), "remote_control", P.ALLOW)
MainWindow._refresh_control_banner(window)
check("approved elsewhere: the card's box is ticked, with the time left",
      card._pc_control.get_active() and "min left" in card._pc_control.get_label())
check("…and showing it records no further change or new deadline", changes == [P.ALLOW])
until = pairing.PairedDevice.load(dev.fingerprint).pc_control_until
MainWindow._refresh_control_banner(window)
check("…however often it is refreshed", pairing.PairedDevice.load(dev.fingerprint).pc_control_until == until)
changes.clear()
P.set_mode(pairing.PairedDevice.load(dev.fingerprint), "remote_control", P.DENY)   # what "Revoke now" does
MainWindow._refresh_control_banner(window)
check("revoked elsewhere: the box is unticked", not card._pc_control.get_active() and changes == [P.DENY]
      and card._pc_control.get_label() == "Allow PC control")
changes.clear()
card._pc_control.set_active(True)
check("the box still switches control on when the person ticks it",
      changes == [P.ALLOW] and pairing.PairedDevice.load(dev.fingerprint).pc_control_active())
pairing.PairedDevice.update(dev.fingerprint, pc_control_until=time.time() - 1)
changes.clear()
MainWindow._refresh_control_banner(window)
check("run out: unticked, switched off on disk and recorded once", not card._pc_control.get_active()
      and not pairing.PairedDevice.load(dev.fingerprint).allow_pc_control and changes == [P.DENY])

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
