#!/usr/bin/env python3
"""The part of the phone folder mount that talks to the phone (phone_fs.ServerBackend), without FUSE: file managers call it
from several threads at once. Also the mount point's name. Needs neither fusepy nor /dev/fuse.
Run: python3 tests/test_phone_fs_backend.py"""
import asyncio
import errno
import sys
import threading
import types
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from phonebridge import phone_fs  # noqa: E402

PASS = FAIL = 0


def check(label, cond):
    global PASS, FAIL
    PASS, FAIL = (PASS + 1, FAIL) if cond else (PASS, FAIL + 1)
    print(f"  {'ok ' if cond else 'FAIL'} {label}")


class FakePhone:
    """Stands in for the control server: answers files.list after a short delay (or not at all)."""

    def __init__(self) -> None:
        self.asked: list[str] = []
        self.connected, self.answers = True, True
        self.handler = None

    def on(self, _type, handler):
        self.handler = handler

    async def send(self, fp, _ch, _type, payload):
        if not self.connected:
            return False
        self.asked.append(payload["path"])
        if self.answers:
            async def reply():
                await asyncio.sleep(0.2)
                conn = types.SimpleNamespace(fingerprint=fp)
                await self.handler(conn, types.SimpleNamespace(payload={"path": payload["path"], "entries": [payload["path"]]}))
            self.task = asyncio.ensure_future(reply())
        return True


def main() -> int:
    phone_fs.LIST_TIMEOUT = 1.5
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    phone = FakePhone()
    backend = phone_fs.ServerBackend(phone, loop, "cafe")

    def attempt(path):
        try:
            return backend.list(path)["entries"]
        except OSError as exc:
            return exc.errno

    pool = ThreadPoolExecutor(8)
    print("1. many readers of one folder")
    got = list(pool.map(attempt, ["/DCIM"] * 6))
    check("every concurrent request for a folder gets the listing", got == [["/DCIM"]] * 6)
    check("…from a single question to the phone", phone.asked == ["/DCIM"])
    check("different folders are asked separately", list(pool.map(attempt, ["/a", "/b"])) == [["/a"], ["/b"]])
    phone.asked.clear()
    check("a later request asks again", attempt("/DCIM") == ["/DCIM"] and phone.asked == ["/DCIM"])

    print("2. failures")
    phone.answers = False
    phone.asked.clear()
    got = list(pool.map(attempt, ["/slow"] * 3))
    check("an unanswered request fails with an I/O error for every reader", got == [errno.EIO] * 3 and phone.asked == ["/slow"])
    phone.answers = True
    check("…and the next request is a new one that works", attempt("/slow") == ["/slow"] and phone.asked == ["/slow", "/slow"])
    phone.connected = False
    check("with the phone disconnected it fails at once", attempt("/gone") == errno.ENOTCONN)
    phone.connected = True
    check("…and works again after reconnecting", attempt("/gone") == ["/gone"])
    check("no request is left waiting for a failed folder", not [f for f in backend._waiting.values() if not f.done()])  # noqa: SLF001

    print("3. the mount point stays inside ~/PhoneBridge")
    base = Path.home() / "PhoneBridge"
    for name in ("..", ".", " .. ", "...", "", "../x", "a/b", "Pixel 9", ".hidden"):
        path = phone_fs.Mount(None, None, "cafe", name).path
        check(f"phone name {name!r}", path.parent == base and path.name not in ("", ".", "..") and not path.name.startswith("."))

    loop.call_soon_threadsafe(loop.stop)
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
