#!/usr/bin/env python3
"""Headless "PC" for smoke-testing a real, already-paired phone — no GUI.

Runs the real ControlChannelServer (same cert, same pairing in ~/.config/phonebridge) in place of the
GTK app, waits for the phone to connect, then exercises: device.info, clipboard, PC->phone file
transfer (verified by sha256 over adb), phone->PC file fetch (files.list / files.get), and screen
mirroring over UDP/TCP video (counts frames).

Stop the GUI app first (it owns port 28100):  pkill -x phonebridge   (NOT `pkill -f`)
Run:  ~/.local/share/phonebridge/venv/bin/python linux/tools/phone_smoke.py [--adb SERIAL] [--only a,b]
Checks: info, clipboard, push, pull, video.  Exit status is the number of failed checks.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from phonebridge import file_transfer  # noqa: E402
from phonebridge.transport import ControlChannelServer  # noqa: E402

RESULTS: list[tuple[str, str, str]] = []   # (check, PASS|FAIL|SKIP, detail)


def record(name: str, status: str, detail: str = "") -> None:
    RESULTS.append((name, status, detail))
    print(f"  {status:4} {name}" + (f" — {detail}" if detail else ""), flush=True)


class Smoke:
    def __init__(self, adb: str | None) -> None:
        self.adb = adb
        self.server = ControlChannelServer()
        self.conn = None
        self.inbox: dict[str, asyncio.Queue] = {}
        self.workdir = Path(tempfile.mkdtemp(prefix="pb-smoke-"))

    # -- plumbing ---------------------------------------------------------------------------
    def _queue(self, type_: str) -> asyncio.Queue:
        return self.inbox.setdefault(type_, asyncio.Queue())

    def _listen(self, *types: str) -> None:
        for t in types:
            async def handler(_conn, env, t=t):
                self._queue(t).put_nowait(env.payload)
            self.server.on(t, handler)

    async def expect(self, type_: str, timeout: float = 15) -> dict:
        return await asyncio.wait_for(self._queue(type_).get(), timeout)

    def drain(self, type_: str) -> None:
        q = self._queue(type_)
        while not q.empty():
            q.get_nowait()

    def adb_sh(self, *args: str) -> str:
        cmd = ["adb"] + (["-s", self.adb] if self.adb else []) + list(args)
        return subprocess.run(cmd, capture_output=True, text=True, timeout=60).stdout.strip()

    # -- run --------------------------------------------------------------------------------
    async def start(self, wait: float) -> bool:
        self._listen("device.info", "clipboard.update", "files.list_result", "file.saved",
                     "video.started", "video.stopped", "mirror.status", "file.reject")
        file_transfer.register(self.server, lambda _p: self.workdir, None, None)
        connected = asyncio.Event()
        loop = asyncio.get_running_loop()

        def on_conn(conn, up):
            if up:
                self.conn = conn
                loop.call_soon_threadsafe(connected.set)
        self.server.on_connection_change(on_conn)
        await self.server.start()
        asyncio.ensure_future(self.server.serve_forever())
        try:
            await asyncio.wait_for(connected.wait(), wait)
        except asyncio.TimeoutError:
            return False
        return True

    async def check_info(self) -> None:
        self.drain("device.info")
        await self.conn.send("control", "device.info_request", {})
        p = await self.expect("device.info")
        record("info", "PASS", f"{p.get('model') or p.get('name')}, android {p.get('android_version', '?')}, "
                               f"battery {p.get('battery_pct')}%")

    async def check_clipboard(self) -> None:
        text = f"pb-smoke-{int(time.time())}"
        await self.conn.send("control", "clipboard.update", {"text": text})
        await asyncio.sleep(2)
        # Android 10+ only lets the foreground app read the clipboard, so we can only check the
        # phone side didn't drop the link, plus the request/response path phone->PC.
        await self.conn.send("control", "device.info_request", {})
        await self.expect("device.info")
        self.drain("clipboard.update")
        record("clipboard", "PASS", f"pc->phone sent {text!r}, link alive (phone-side content not readable via adb)")

    async def check_push(self) -> None:
        data = os.urandom(3 * 1024 * 1024 + 123)
        src = self.workdir / f"pb-smoke-push-{int(time.time())}.bin"
        src.write_bytes(data)
        want = hashlib.sha256(data).hexdigest()
        t0 = time.time()
        await asyncio.wait_for(file_transfer.send_file(self.conn, src), 120)
        secs = time.time() - t0
        try:
            saved = await self.expect("file.saved", 10)
        except asyncio.TimeoutError:
            saved = {}
        path = saved.get("path") or f"/sdcard/Download/PhoneBridge/{src.name}"
        if self.adb is None and not saved:
            record("push", "PASS", f"{len(data)} bytes in {secs:.1f}s (no adb serial, hash not verified)")
            return
        out = self.adb_sh("shell", f"sha256sum '{path}' 2>/dev/null || sha256sum '/sdcard/Download/PhoneBridge/{src.name}'")
        got = out.split()[0] if out else ""
        if got == want:
            record("push", "PASS", f"{len(data)} bytes in {secs:.1f}s, sha256 matches on phone ({path})")
        else:
            record("push", "FAIL", f"phone hash {got or 'unreadable'} != {want[:12]}…")

    async def check_pull(self) -> None:
        await self.conn.send("control", "files.list", {"path": "Download"})
        listing = await self.expect("files.list_result")
        if listing.get("error"):
            record("pull", "FAIL", f"files.list error: {listing['error']}")
            return
        files = [e for e in listing["entries"] if not e["is_dir"] and 0 < e["size"] < 20 * 1024 * 1024]
        if not files:
            record("pull", "SKIP", f"listing worked ({len(listing['entries'])} entries) but no small file in Download/")
            return
        entry = files[0]
        rel = f"Download/{entry['name']}"
        fut = file_transfer.expect_fetch(rel, self.workdir)
        try:
            await self.conn.send("control", "files.get", {"path": rel})
            got = await asyncio.wait_for(fut, 120)
        finally:
            file_transfer.cancel_fetch(rel)
        ok = got.stat().st_size == entry["size"]
        if ok and self.adb is not None:
            theirs = self.adb_sh("shell", f"sha256sum '/sdcard/{rel}'").split()[:1]
            ok = bool(theirs) and theirs[0] == hashlib.sha256(got.read_bytes()).hexdigest()
        record("pull", "PASS" if ok else "FAIL", f"{rel} ({entry['size']} bytes) listed + fetched" + ("" if ok else ", size/hash mismatch"))

    async def check_video(self, seconds: float = 8) -> None:
        frames = 0
        first = 0.0

        def sink(_frame: bytes) -> None:
            nonlocal frames, first
            frames += 1
            first = first or time.time()
        self.server.set_video_sink(self.conn.fingerprint, sink)
        self.drain("video.started"); self.drain("mirror.status")
        await self.conn.send("control", "mirror.start", {})
        self.adb_consent_tap()
        try:
            started = await asyncio.wait_for(self._wait_started(), 40)
        except asyncio.TimeoutError:
            record("video", "FAIL", "no video.started in 40 s — screen-capture consent is probably waiting on the phone")
            await self.conn.send("control", "mirror.stop", {})
            return
        if started.get("state"):
            record("video", "FAIL", f"mirror.status {started}")
            return
        await asyncio.sleep(seconds)
        transport = self.server.video_transport.get(self.conn.fingerprint, "none")
        await self.conn.send("control", "mirror.stop", {})
        self.server.set_video_sink(self.conn.fingerprint, None)
        if frames:
            record("video", "PASS", f"{frames} frames in {seconds:.0f}s over {transport.upper()} "
                                    f"({started.get('width')}x{started.get('height')})")
        else:
            record("video", "FAIL", f"video.started but 0 frames in {seconds:.0f}s (transport {transport})")

    async def _wait_started(self) -> dict:
        while True:
            q_started, q_status = self._queue("video.started"), self._queue("mirror.status")
            if not q_started.empty():
                return q_started.get_nowait()
            if not q_status.empty():
                s = q_status.get_nowait()
                if s.get("state") not in ("consent_pending", "pending", "waiting_consent"):
                    return s
            await asyncio.sleep(0.2)

    def adb_consent_tap(self) -> None:
        """Best effort: press 'Start now' on Android's screen-capture consent dialog."""
        if self.adb is None:
            return
        for _ in range(10):
            time.sleep(1)
            self.adb_sh("shell", "uiautomator", "dump", "/sdcard/pb-ui.xml")
            xml = self.adb_sh("shell", "cat", "/sdcard/pb-ui.xml")
            import re
            m = re.search(r'text="(Start now|START NOW|Allow|Share screen)"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml)
            if m:
                x = (int(m[2]) + int(m[4])) // 2
                y = (int(m[3]) + int(m[5])) // 2
                self.adb_sh("shell", "input", "tap", str(x), str(y))
                return


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--adb", help="adb serial: verifies pushed files by hash and taps the capture-consent dialog")
    ap.add_argument("--only", help="comma list of: info,clipboard,push,pull,video")
    ap.add_argument("--wait", type=float, default=90, help="seconds to wait for the phone to connect")
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING)

    s = Smoke(args.adb)
    print(f"Listening on :{s.server.port}; waiting up to {args.wait:.0f}s for the paired phone…", flush=True)
    if not await s.start(args.wait):
        record("connect", "FAIL", "phone did not connect (is the GUI app still running on :28100?)")
        return 1
    record("connect", "PASS", f"{s.conn.device.name} ({s.conn.fingerprint})")
    wanted = set((args.only or "info,clipboard,push,pull,video").split(","))
    for name in ("info", "clipboard", "push", "pull", "video"):
        if name not in wanted:
            continue
        try:
            await getattr(s, f"check_{name}")()
        except Exception as exc:  # noqa: BLE001
            record(name, "FAIL", f"{type(exc).__name__}: {exc}")
    failed = sum(1 for _, st, _ in RESULTS if st == "FAIL")
    print(f"\n{sum(1 for _, st, _ in RESULTS if st == 'PASS')} passed, {failed} failed, "
          f"{sum(1 for _, st, _ in RESULTS if st == 'SKIP')} skipped")
    return failed


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(130)
