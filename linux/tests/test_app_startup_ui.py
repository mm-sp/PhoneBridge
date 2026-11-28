#!/usr/bin/env python3
"""The desktop app's start-up: it accepts phones only once the window and PC control are listening, so a phone that is
already waiting to reconnect is never missed. The real app on a (virtual) display, with a real server on a free port.
Run directly: `xvfb-run -a python3 tests/test_app_startup_ui.py`
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
tmp = Path(tempfile.mkdtemp())
os.environ["HOME"] = str(tmp)
os.environ["PHONEBRIDGE_NO_WATCHDOG"] = "1"

from phonebridge import app as appmod, ble_wake, mdns, pairing, tls_identity, transport  # noqa: E402
from phonebridge.screensync import glue as sync_glue  # noqa: E402
from phonebridge.screensync.adb_bridge import Adb  # noqa: E402
from phonebridge.security import keystore  # noqa: E402

PASS = 0
FAIL = 0


def check(label: str, cond: bool) -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {label}")
    else:
        FAIL += 1
        print(f" FAIL {label}")


def main() -> int:
    pairing.DEVICES_DIR = tmp / "devices"
    tls_identity.CONFIG_DIR, tls_identity.CERT_PATH, tls_identity.KEY_PATH = tmp, tmp / "c.pem", tmp / "k.pem"
    keystore.DEFAULT_CONFIG_DIR = tmp
    transport.DISCOVERY_PORT = 0
    ble_wake.start = lambda: None          # no Bluetooth, mDNS or adb in a test
    ble_wake.stop = lambda: None
    mdns.advertise = lambda *_a: None
    sync_glue.ADB_FACTORY = lambda: Adb(runner=lambda _a, _t: (1, "no adb here"), adb_path=None)

    app = appmod.PhoneBridgeApp()
    app.set_application_id("dev.phonebridge.startuptest")
    app._usb_tick = lambda: False          # noqa: SLF001
    app.server = server = transport.ControlChannelServer(port=0)
    at_start: dict = {}
    real_start = server.start

    async def start() -> None:
        at_start.update(listeners=len(server._connection_listeners),          # noqa: SLF001
                        handlers=set(server._handlers),                       # noqa: SLF001
                        ask=server.ask_callback is not None,
                        approval=getattr(getattr(app, "pc_control", None), "approval_callback", None) is not None)
        await real_start()

    server.start = start
    seen: dict = {}

    def look() -> bool:
        window = app.get_active_window()
        seen.update(window=window is not None and window.get_visible(), listening=server._server is not None)  # noqa: SLF001
        app.quit()
        return False

    app.connect_after("activate", lambda _a: appmod.GLib.timeout_add(500, look))
    app.run([])

    check("the server was started", bool(at_start) and seen.get("listening"))
    check("the window and PC control already follow connections", at_start.get("listeners", 0) >= 2)
    wanted = set(appmod.PHONE_MESSAGE_TYPES) | {"file.offer", "file.cancel", "sync.command", "pc.input", "pc.term_open"}
    check("every handler for a phone's messages is registered", wanted <= at_start.get("handlers", set()))
    check("permission prompts can be raised", at_start.get("ask") and at_start.get("approval"))
    check("the window is shown", seen.get("window"))

    print(f"\n{PASS} passed, {FAIL} failed")
    shutil.rmtree(tmp, ignore_errors=True)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
