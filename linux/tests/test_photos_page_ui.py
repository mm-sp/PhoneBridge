#!/usr/bin/env python3
"""The Photos tab loads a page at a time, and starts over after the phone reconnects, also if the link dropped while a
page was still loading. Run: `xvfb-run -a python3 tests/test_photos_page_ui.py`"""
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
from gi.repository import Adw  # noqa: E402

from phonebridge.ui.photos_page import PAGE_SIZE, PhotosPage  # noqa: E402

PASS = FAIL = 0


def check(label, cond):
    global PASS, FAIL
    PASS, FAIL = (PASS + 1, FAIL) if cond else (PASS, FAIL + 1)
    print(f"  {'ok ' if cond else 'FAIL'} {label}")


def tiles(page) -> int:
    n, child = 0, page._flow.get_first_child()
    while child is not None:
        n, child = n + 1, child.get_next_sibling()
    return n


def answer(page, offset, count, total):
    page.set_photos({"offset": offset, "total": total,
                     "photos": [{"id": offset + i, "name": f"{offset + i}.jpg"} for i in range(count)]})


def main():
    app = Adw.Application(application_id="dev.phonebridge.photostest")

    def run(_a):
        asked = []
        page = PhotosPage(on_request_list=lambda offset, limit: asked.append(offset), on_request_photo=lambda _i: None)
        page.on_shown()
        check("opening the tab asks for the first page", asked == [0])
        page.on_shown()
        check("…only once while it is loading", asked == [0])
        answer(page, 0, PAGE_SIZE, 120)
        page._load_next()
        check("the next page continues where the first ended", asked == [0, PAGE_SIZE] and tiles(page) == PAGE_SIZE)

        page.reset()                       # the link dropped during that load and the phone reconnected
        check("after a reconnect no spinner is left", not page._spinner.get_visible())
        page.on_shown()
        check("opening the tab loads again from the start", asked == [0, PAGE_SIZE, 0] and tiles(page) == 0)
        answer(page, PAGE_SIZE, PAGE_SIZE, 120)
        check("a late page from before the reconnect is ignored", tiles(page) == 0)
        answer(page, 0, 3, 3)
        check("the new answer is shown", tiles(page) == 3)

        page.reset()
        page.on_shown()
        answer(page, 0, 2, 2)
        check("a phone with fewer photos replaces the old grid", tiles(page) == 2)
        app.quit()

    app.connect("activate", run)
    app.run([])
    print(f"\n{PASS} passed, {FAIL} failed")
    shutil.rmtree(tmp, ignore_errors=True)
    return 1 if FAIL or not PASS else 0


if __name__ == "__main__":
    sys.exit(main())
