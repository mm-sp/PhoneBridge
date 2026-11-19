#!/usr/bin/env python3
"""Builds the Security page against a real ControlChannelServer and drives its buttons (needs a display or xvfb-run).
Run: `xvfb-run -a python3 tests/test_security_ui.py`"""
import asyncio
import os
import shutil
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
tmp = Path(tempfile.mkdtemp())
os.environ["HOME"] = str(tmp)

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib  # noqa: E402

from phonebridge import pairing, tls_identity, transport  # noqa: E402
from phonebridge.net_thread import NetThread  # noqa: E402
from phonebridge.security import keystore, ledger as L, permissions as P  # noqa: E402
from phonebridge.ui.security_page import SecurityPage  # noqa: E402

PASS = FAIL = 0


def check(label, cond):
    global PASS, FAIL
    PASS, FAIL = (PASS + 1, FAIL) if cond else (PASS, FAIL + 1)
    print(f"  {'ok ' if cond else 'FAIL'} {label}")


def texts(widget, out=None):
    out = [] if out is None else out
    for attr in ("get_title", "get_subtitle", "get_label"):
        fn = getattr(widget, attr, None)
        if fn:
            try:
                v = fn()
                if v:
                    out.append(v)
            except Exception:  # noqa: BLE001
                pass
    child = widget.get_first_child()
    while child is not None:
        texts(child, out)
        child = child.get_next_sibling()
    return out


def main():
    pairing.DEVICES_DIR = tmp / "devices"
    tls_identity.CONFIG_DIR, tls_identity.CERT_PATH, tls_identity.KEY_PATH = tmp, tmp / "c.pem", tmp / "k.pem"
    keystore.DEFAULT_CONFIG_DIR = tmp
    dev = pairing.PairedDevice(fingerprint="abcd1234abcd1234", name="UI Phone", session_key=os.urandom(32))
    dev.save()
    net = NetThread(); net.start()
    server = transport.ControlChannelServer(port=0)
    server.ledger = L.LocalLedger(tmp / "audit.jsonl", keystore.for_config_dir(tmp))
    server.audit(L.AUTH_FAIL, "zzzz", reason="unknown_device")
    net.run_coro(server.start()).result(10)
    toasts = []
    revoked = []
    app = Adw.Application(application_id="dev.phonebridge.test")

    def run(_app):
        win = Adw.ApplicationWindow(application=app)
        page = SecurityPage(server, net, on_toast=toasts.append, on_revoked=revoked.append)
        win.set_content(page); win.present()
        t = texts(page)
        check("page shows the verified ledger", any("Audit ledger verified" in x for x in t))
        check("page shows the paired device and its fingerprint", "UI Phone" in t and any("abcd 1234 abcd 1234" in x for x in t))
        check("page lists every feature", all(label in t for label in P.FEATURES.values()))
        check("page shows the failed-auth event", any("Authentication FAILED" in x for x in t))
        check("keystore status is shown honestly (file fallback is flagged as not protected)",
              any("NOT PROTECTED" in x for x in t) or any("Keys are held" in x for x in t))

        # flip a permission through the UI model
        P.on_change.append(lambda d, f, m: server.audit(L.PERMISSION, d.fingerprint, feature=f, mode=m))
        P.set_mode(pairing.PairedDevice.load(dev.fingerprint), "sms", P.DENY)
        check("a UI permission change persists", pairing.PairedDevice.load(dev.fingerprint).permissions["sms"] == "deny")
        check("…and is recorded in the ledger via the app's hook",
              any(r["type"] == L.PERMISSION and r["meta"] == {"feature": "sms", "mode": "deny"} for r in server.ledger.records()))

        page._on_disconnect_all(None)
        check("DISCONNECT ALL puts the server in lockdown", server.locked_down and toasts and "blocked" in toasts[-1])
        check("…and the page now offers to resume", any("Resume connections" in x for x in texts(page)))
        page._on_resume(None)

        def after_resume():
            check("resume lifts the lockdown", not server.locked_down)
            asyncio.run_coroutine_threadsafe(server.revoke(dev.fingerprint), net.loop).result(5)
            page.refresh()
            check("revoked device disappears from the dashboard", "UI Phone" not in texts(page))
            check("ledger on the page still verifies after all of it", server.ledger.verify().ok)
            app.quit()
            return False
        GLib.timeout_add(600, after_resume)

    app.connect("activate", run)
    app.run([])
    print(f"\n{PASS} passed, {FAIL} failed")
    shutil.rmtree(tmp, ignore_errors=True)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
