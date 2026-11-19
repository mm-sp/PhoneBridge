#!/usr/bin/env python3
"""The Device card says HOW the phone is connected (USB / Wi-Fi / mobile data / hotspot), not just "Connected".
Needs a display or xvfb-run: `xvfb-run -a python3 tests/test_device_link_ui.py`"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["HOME"] = tempfile.mkdtemp()

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw  # noqa: E402

from phonebridge import pairing  # noqa: E402
from phonebridge.ui.device_page import _DeviceCard  # noqa: E402

PASS = FAIL = 0


def check(label, cond):
    global PASS, FAIL
    PASS, FAIL = (PASS + 1, FAIL) if cond else (PASS, FAIL + 1)
    print(f"  {'ok ' if cond else 'FAIL'} {label}")


Adw.init()
dev = pairing.PairedDevice(name="Test phone", fingerprint="a" * 64, session_key=b"k" * 32) \
    if "session_key" in getattr(pairing.PairedDevice, "__dataclass_fields__", {}) else None
if dev is None:
    print("SKIP: cannot build a PairedDevice here")
    sys.exit(0)
card = _DeviceCard(dev, lambda d: None, lambda *a: None, lambda fp: None)

card.set_connected(False)
check("offline: pill says Offline", card._status.get_label() == "○ Offline")

card.set_connected(True, "127.0.0.1", "usb")
check("USB tunnel -> 'USB cable'", card._status.get_label() == "● Connected · USB cable")
check("USB row subtitle", card._rows["link"].get_subtitle().startswith("USB cable"))

card.set_connected(True, "192.168.1.20", "lan")
check("LAN with no info yet -> Wi-Fi", card._status.get_label() == "● Connected · Wi-Fi")
check("LAN subtitle keeps the address", "192.168.1.20" in card._rows["link"].get_subtitle())

card.update({"on_wifi": False, "hotspot_on": True})
check("phone sharing hotspot, not on Wi-Fi -> Phone hotspot", card._status.get_label() == "● Connected · Phone hotspot")

card.set_connected(True, "1.2.3.4", "relay")
card.update({"on_wifi": True, "wifi_ssid": "Home"})
check("relay + phone on Wi-Fi", card._status.get_label() == "● Connected · Wi-Fi (remote, via relay)")
check("relay hides the relay's address", "1.2.3.4" not in card._rows["link"].get_subtitle())

card.update({"on_wifi": False, "network_type": "LTE", "signal_level": 3})
check("relay + cellular -> Mobile data LTE", card._status.get_label() == "● Connected · Mobile data LTE (remote, via relay)")

card.update({})
check("relay, nothing known -> Internet", card._status.get_label() == "● Connected · Internet (remote, via relay)")

card.set_connected(False)
check("disconnect resets the pill", card._status.get_label() == "○ Offline")
print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
