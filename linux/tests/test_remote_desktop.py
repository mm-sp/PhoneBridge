#!/usr/bin/env python3
"""The PC's screen-share / remote-input dialog (xdg-desktop-portal), driven through a fake session bus: what the phone
can do while the dialog is still open, after it is refused, and after PC control is revoked while it waits.
Run directly: `python3 tests/test_remote_desktop.py`
"""
import os
import stat
import sys
import tempfile
import time
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from phonebridge import pairing, pc_control as pcmod, pc_remote  # noqa: E402

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


class Params:
    def __init__(self, code, results):
        self.value = (code, results)

    def unpack(self):
        return self.value


class FakeBus:
    """Records portal calls and lets the test play the portal's replies."""

    def __init__(self) -> None:
        self.subs: dict[int, tuple[str, str, object]] = {}
        self.calls: list[tuple[str, str]] = []   # (object path, method)
        self._next = 0

    def get_unique_name(self):
        return ":1.42"

    def signal_subscribe(self, _sender, _iface, member, path, _arg0, _flags, callback):
        self._next += 1
        self.subs[self._next] = (member, path, callback)
        return self._next

    def signal_unsubscribe(self, sub):
        self.subs.pop(sub, None)

    def call(self, _dest, path, _iface, method, *_rest):
        self.calls.append((path, method))

    def methods(self):
        return [m for _p, m in self.calls]

    def pending(self):
        """The reply handler of the newest unanswered portal request."""
        return [cb for _k, (member, _path, cb) in sorted(self.subs.items()) if member == "Response"][-1]

    def respond(self, code=0, results=None, callback=None):
        (callback or self.pending())(None, None, None, None, None, Params(code, results or {}))

    def close_session(self):
        for member, _path, cb in list(self.subs.values()):
            if member == "Closed":
                cb()

    def walk_to_start(self):
        """Answers CreateSession, SelectDevices and SelectSources: the owner is now looking at the dialog."""
        self.respond(results={"session_handle": "/session/pb"})
        self.respond()
        self.respond()

    def approve(self, devices=3):
        self.respond(results={"devices": devices, "restore_token": "token-1",
                              "streams": [(7, {"size": (1920, 1080), "source_type": 1})]})


def new_remote(bus):
    pc_remote.Gio.bus_get_sync = lambda *_a: bus
    return pc_remote.RemoteDesktop()


def session_tests(tmp: Path) -> None:
    pc_remote.TOKEN_PATH = tmp / "cfg" / "remote_desktop_token"

    print("1. while the dialog is open")
    bus = FakeBus()
    remote = new_remote(bus)
    first, second = [], []
    remote.ensure(first.append)
    bus.walk_to_start()
    check("a session handle alone does not make the session ready", remote.session is not None and not remote.ready)
    remote.ensure(second.append)
    check("a second request waits for the same dialog instead of succeeding", second == [] and first == [])
    check("…and does not open another one", bus.methods().count("CreateSession") == 1)
    bus.approve()
    check("approval answers both and the session is ready", first == [None] and second == [None] and remote.ready and remote.can_control)
    check("the restore token is saved readable by this user only",
          pc_remote.TOKEN_PATH.read_text() == "token-1" and stat.S_IMODE(os.stat(pc_remote.TOKEN_PATH).st_mode) == 0o600)

    print("2. the owner refuses")
    bus = FakeBus()
    remote = new_remote(bus)
    answers = []
    remote.ensure(answers.append)
    bus.walk_to_start()
    bus.respond(code=1)
    check("the refusal is reported", answers == ["Remote control was not allowed on the PC"])
    check("the half-made session is closed and not ready",
          not remote.ready and remote.session is None and ("/session/pb", "Close") in bus.calls)
    again = []
    remote.ensure(again.append)
    check("the next request asks again", bus.methods().count("CreateSession") == 2 and again == [])
    bus.walk_to_start()
    bus.approve()
    check("…and can be approved", again == [None] and remote.ready)

    print("3. the session closes before it starts")
    bus = FakeBus()
    remote = new_remote(bus)
    answers = []
    remote.ensure(answers.append)
    bus.walk_to_start()
    stale = bus.pending()
    bus.close_session()
    check("waiting requests are answered with an error", len(answers) == 1 and bool(answers[0]) and not remote.ready)
    again = []
    remote.ensure(again.append)
    check("a new attempt can start afterwards", bus.methods().count("CreateSession") == 2)
    bus.respond(code=1, callback=stale)
    check("a late reply to the abandoned attempt does not end the new one", again == [] and remote.starting)
    bus.walk_to_start()
    bus.approve()
    check("the new attempt completes", again == [None] and remote.ready)
    bus.close_session()
    check("closing a running session makes it not ready", not remote.ready and not remote.can_control)

    print("4. the token cannot be saved")
    blocker = tmp / "blocker"
    blocker.write_text("a file where the folder should be")
    pc_remote.TOKEN_PATH = blocker / "remote_desktop_token"
    bus = FakeBus()
    remote = new_remote(bus)
    answers = []
    remote.ensure(answers.append)
    bus.walk_to_start()
    bus.approve()
    check("the session still starts", answers == [None] and remote.ready)


def pc_control_tests(tmp: Path) -> None:
    pc_remote.TOKEN_PATH = tmp / "cfg2" / "remote_desktop_token"
    pairing.DEVICES_DIR = tmp / "devices"

    streams: list = []

    class FakeStreamer:
        size = (1280, 720)

        def __init__(self, remote, on_frame):
            self.stopped = False
            streams.append(self)

        def stop(self):
            self.stopped = True

    pcmod.ScreenStreamer = FakeStreamer
    pcmod.GLib.idle_add = lambda fn, *a: fn(*a)

    class Server:
        current = None

        def on(self, *_a):
            pass

        def on_connection_change(self, *_a):
            pass

        def get_connection(self, _fp):
            return self.current

        def audit(self, *_a, **_k):
            pass

    loop = types.SimpleNamespace(call_soon_threadsafe=lambda *a: None, call_later=lambda *a: None)
    device = pairing.PairedDevice("cafe", "Test Phone", os.urandom(32), allow_pc_control=True,
                                  pc_control_until=time.time() + 3600)

    def run(coro):
        """Drives a handler that never actually suspends."""
        try:
            coro.send(None)
        except StopIteration:
            return
        raise AssertionError("handler suspended")

    def setup():
        streams.clear()
        device.allow_pc_control, device.pc_control_until = True, time.time() + 3600
        device.save()
        bus = FakeBus()
        new_remote(bus)
        server = Server()
        conn = types.SimpleNamespace(fingerprint="cafe")
        server.current = conn
        pc = pcmod.PcControl(server, loop, clipboard_text=lambda: "")
        sent, applied = [], []
        pc._send_from_gtk = lambda _conn, type_, payload: sent.append((type_, payload))
        pc._apply_input = lambda _remote, kind, p: applied.append(p.get("text", kind))
        return pc, server, conn, bus, sent, applied

    def start_screen(pc, conn):
        run(pc._on_screen_start(conn, None))

    def send_input(pc, conn, text):
        run(pc._on_input(conn, types.SimpleNamespace(payload={"kind": "text", "text": text})))

    print("5. screen view and the dialog")
    pc, server, conn, bus, sent, applied = setup()
    start_screen(pc, conn)
    bus.walk_to_start()
    start_screen(pc, conn)
    check("tapping Start again while the dialog is open starts nothing and reports no error",
          not streams and not any(p.get("state") == "denied" for _t, p in sent))
    bus.approve()
    check("approval starts exactly one stream", len(streams) == 1 and not streams[0].stopped
          and [t for t, _p in sent].count("pc.screen_started") == 1)

    def pending_screen():
        pc, server, conn, bus, sent, applied = setup()
        start_screen(pc, conn)
        bus.walk_to_start()
        return pc, server, conn, bus, sent

    pc, server, conn, bus, sent = pending_screen()
    device.allow_pc_control, device.pc_control_until = False, None
    device.save()
    bus.approve()
    check("revoked while the dialog was open: approving it starts no stream",
          not streams and pc._stream_conn is None and not any(t == "pc.screen_started" for t, _p in sent))

    pc, server, conn, bus, sent = pending_screen()
    device.pc_control_until = time.time() - 1
    device.save()
    bus.approve()
    check("expired while the dialog was open: no stream", not streams and pc._stream_conn is None)

    pc, server, conn, bus, sent = pending_screen()
    server.current = None
    bus.approve()
    check("phone disconnected while the dialog was open: no stream", not streams and pc._stream_conn is None)

    pc, server, conn, bus, sent = pending_screen()
    server.current = types.SimpleNamespace(fingerprint="cafe")
    bus.approve()
    check("phone reconnected meanwhile: the old connection gets no stream", not streams)

    pc, server, conn, bus, sent = pending_screen()
    run(pc._on_screen_stop(conn, None))
    bus.approve()
    check("Stop while the dialog was open: no stream", not streams and pc._stream_conn is None)
    start_screen(pc, conn)
    check("…and a later Start works", len(streams) == 1)

    pc, server, conn, bus, sent = pending_screen()
    bus.approve()
    pc._on_connection_change(conn, True)  # noqa: SLF001
    check("being told about the viewing connection itself changes nothing", not streams[0].stopped)
    replacement = types.SimpleNamespace(fingerprint="cafe")
    server.current = replacement
    pc._on_connection_change(replacement, True)  # noqa: SLF001
    check("the phone reconnecting over a dead link ends the old capture", streams[0].stopped and pc._stream_conn is None)  # noqa: SLF001
    start_screen(pc, replacement)
    check("…and the new connection can start its own", len(streams) == 2 and not streams[1].stopped)
    pc._on_connection_change(replacement, False)  # noqa: SLF001
    check("a disconnect ends the capture", streams[1].stopped)

    print("6. touch and typing and the dialog")
    pc, server, conn, bus, sent, applied = setup()
    send_input(pc, conn, "a")
    send_input(pc, conn, "b")
    bus.walk_to_start()
    send_input(pc, conn, "c")
    check("nothing is injected before the dialog is answered", applied == [])
    check("a burst of input reports 'waiting' only once",
          [p.get("state") for t, p in sent if t == "pc.remote_status"] == ["waiting"])
    bus.approve()
    check("input that waited only briefly is applied once, in order", applied == ["a", "b", "c"])
    send_input(pc, conn, "d")
    check("afterwards input is applied directly", applied == ["a", "b", "c", "d"] and bus.methods().count("CreateSession") == 1)

    pc, server, conn, bus, sent, applied = setup()
    pc.INPUT_MAX_AGE = -1.0   # as if the dialog had been open for a long time
    send_input(pc, conn, "stale")
    bus.walk_to_start()
    bus.approve()
    check("input that waited too long is dropped, never replayed", applied == [])

    pc, server, conn, bus, sent, applied = setup()
    send_input(pc, conn, "x")
    bus.walk_to_start()
    device.allow_pc_control, device.pc_control_until = False, None
    device.save()
    bus.approve()
    check("revoked while the dialog was open: queued input is not injected", applied == [])

    pc, server, conn, bus, sent, applied = setup()
    send_input(pc, conn, "x")
    bus.walk_to_start()
    bus.respond(code=1)
    send_input(pc, conn, "y")
    send_input(pc, conn, "z")
    check("after a refusal nothing is injected and touch events do not re-open the dialog",
          applied == [] and bus.methods().count("CreateSession") == 1)
    check("the phone is told about the refusal once",
          [p.get("state") for _t, p in sent].count("denied") == 1)
    pc._portal_refused_at -= pc.INPUT_REASK_COOLDOWN + 1
    send_input(pc, conn, "later")
    check("after the cooldown, input may ask again", bus.methods().count("CreateSession") == 2)

    pc, server, conn, bus, sent, applied = setup()
    start_screen(pc, conn)
    bus.walk_to_start()
    bus.respond(code=1)
    start_screen(pc, conn)
    check("an explicit Start after a refusal asks again at once", bus.methods().count("CreateSession") == 2 and not streams)


def main() -> int:
    tmp = Path(tempfile.mkdtemp())
    session_tests(tmp)
    pc_control_tests(tmp)
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
