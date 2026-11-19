#!/usr/bin/env python3
"""The Files tab shows a Cancel button on a running transfer and it really cancels (stops the stream, deletes the partial).
Run: `xvfb-run -a python3 tests/test_files_cancel_ui.py`"""
import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
tmp = Path(tempfile.mkdtemp())
os.environ["HOME"] = str(tmp)

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib  # noqa: E402

from phonebridge import file_transfer  # noqa: E402
from phonebridge.net_thread import NetThread  # noqa: E402
from phonebridge.ui.files_page import FilesPage  # noqa: E402

PASS = FAIL = 0


def check(label, cond):
    global PASS, FAIL
    PASS, FAIL = (PASS + 1, FAIL) if cond else (PASS, FAIL + 1)
    print(f"  {'ok ' if cond else 'FAIL'} {label}")


class FakeConn:
    fingerprint = "aaaa"

    def __init__(self):
        self.sent = []

    async def send(self, ch, t, p):
        self.sent.append((t, p))


class FakeServer:
    def __init__(self, conn):
        self.conn = conn

    def get_connection(self, fp):
        return self.conn if fp == self.conn.fingerprint else None

    def connected_fingerprints(self):
        return [self.conn.fingerprint]


def main():
    net = NetThread(); net.start()
    conn = FakeConn()
    app = Adw.Application(application_id="dev.phonebridge.canceltest")

    def run(_a):
        host = Adw.ApplicationWindow(application=app)
        page = FilesPage(FakeServer(conn), net, on_list=lambda p: None, on_get=lambda p: None)
        host.set_content(page); host.present()

        # a running upload from the phone: partial file + registered inbound transfer
        async def setup():
            inb = file_transfer._Inbound(payload={"size": 10}, device_fp="aaaa", dest_dir=tmp, part_path=tmp / ".pb-x.part", offset=0)
            (tmp / ".pb-x.part").write_bytes(b"half")
            file_transfer._IN["tid1"] = inb
            return inb
        inb = asyncio.run_coroutine_threadsafe(setup(), net.loop).result(5)

        page._transfer_row("in:tid1", "big.iso", "Receiving…", incoming=True)
        btn = page._cancel_buttons["in:tid1"]
        check("a transfer row starts without a Cancel button until it is bound", not btn.get_visible())
        page._bind_cancel("in:tid1", "aaaa", "tid1")
        check("binding makes Cancel visible", btn.get_visible())
        btn.emit("clicked")

        def after():
            check("clicking Cancel cancelled the transfer", inb.cancelled)
            check("…deleted the partial download", not (tmp / ".pb-x.part").exists())
            check("…and told the phone", ("file.cancel", {"transfer_id": "tid1"}) in conn.sent)
            check("the row shows it was cancelled and the button is gone",
                  page._transfer_rows["in:tid1"][0].get_subtitle() == "Cancelled" and not btn.get_visible())
            app.quit()
            return False
        GLib.timeout_add(600, after)

    app.connect("activate", run)
    app.run([])
    print(f"\n{PASS} passed, {FAIL} failed")
    shutil.rmtree(tmp, ignore_errors=True)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
