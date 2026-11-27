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

        # the browser shows only the folder it asked for (the mounted phone folder receives listings too)
        def listing(path, name):
            page.set_listing({"path": path, "entries": [{"name": name, "is_dir": True, "size": 0}]})

        def shown():
            return page._path, page._path_label.get_label()

        page.navigate("/Download")
        listing("/Download", "a")
        check("the requested folder's listing is shown", shown() == ("/Download", "Phone/Download"))
        listing("/DCIM/Camera", "b")
        check("a listing for another folder does not move the view", shown() == ("/Download", "Phone/Download"))
        page.navigate("/Music/")
        listing("/Music", "c")
        check("a trailing slash is the same folder", shown() == ("/Music", "Phone/Music"))
        page.navigate("/")
        listing("/", "d")
        check("the root folder is shown", shown() == ("/", "Phone"))

        # PC -> phone rows are matched to the phone's "saved" notice by transfer id
        def sent_row(key, name, tid=None):
            page._transfer_row(key, name, "Sending…", incoming=False)
            if tid:
                page._on_offered(key, "aaaa", tid)

        def subtitle(key):
            return page._transfer_rows[key][0].get_subtitle()

        sent_row("out:1.0:data.jpg", "data.jpg", "t-data")
        sent_row("out:2.0:a.jpg", "a.jpg", "t-a")
        page.on_phone_saved({"transfer_id": "t-a", "filename": "a (1).jpg", "path": "/Download/a (1).jpg"})
        check("a file the phone renamed is marked saved", subtitle("out:2.0:a.jpg") == "Saved on phone: /Download/a (1).jpg")
        check("…and its Cancel button goes away", not page._cancel_buttons["out:2.0:a.jpg"].get_visible())
        check("…while other rows stay as they were", subtitle("out:1.0:data.jpg") == "Sending…")
        page._mark_sent("out:2.0:a.jpg")
        check("a 'sent' update arriving after the notice doesn't undo it", subtitle("out:2.0:a.jpg").startswith("Saved"))
        page._mark_sent("out:1.0:data.jpg")
        check("…and without one the row says it is waiting", "waiting for phone" in subtitle("out:1.0:data.jpg"))
        sent_row("out:3.0:a.jpg", "a.jpg")
        sent_row("out:4.0:x:a.jpg", "x:a.jpg")
        page.on_phone_saved({"filename": "a.jpg", "path": "/Download/a.jpg"})
        check("without an id only the exact file name matches",
              subtitle("out:3.0:a.jpg").startswith("Saved") and subtitle("out:4.0:x:a.jpg") == "Sending…"
              and "waiting for phone" in subtitle("out:1.0:data.jpg"))
        page.on_phone_failed("t-data", "couldn't save data.jpg")
        check("a file the phone couldn't save is marked failed", subtitle("out:1.0:data.jpg") == "Failed: couldn't save data.jpg"
              and not page._cancel_buttons["out:1.0:data.jpg"].get_visible())
        page._mark_sent("out:1.0:data.jpg")
        page.on_phone_failed("unknown", "x")
        check("…and stays failed", subtitle("out:1.0:data.jpg").startswith("Failed"))

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
