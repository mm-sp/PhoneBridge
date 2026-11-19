#!/usr/bin/env python3
"""Integration: real MainWindow + real ControlChannelServer + a fake phone speaking the real v4 protocol, with a fake adb.
Covers both launch directions end to end over the encrypted channel and the dashboard page.
Needs a display: `xvfb-run -a python3 tests/test_screensync_ui.py`"""
import asyncio
import os
import ssl
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gtk  # noqa: E402

Gtk.init()
if Gdk.Display.get_default() is None:
    print("SKIP: no display (run under xvfb-run)")
    raise SystemExit(0)
Adw.init()

tmp = Path(tempfile.mkdtemp())
os.environ["HOME"] = str(tmp)
from phonebridge import crypto, pairing  # noqa: E402

pairing.DEVICES_DIR = tmp / "devices"
from _isolate import isolate_tls  # noqa: E402

isolate_tls()
from _phone import new_identity, phone_connect  # noqa: E402
from phonebridge.net_thread import NetThread  # noqa: E402
from phonebridge.protocol import Envelope  # noqa: E402
from phonebridge.screensync import commands as C, glue  # noqa: E402
from phonebridge.screensync.adb_bridge import Adb  # noqa: E402
from phonebridge.transport import ControlChannelServer  # noqa: E402
from phonebridge.ui.main_window import MainWindow  # noqa: E402
from test_screensync import FakePhone  # noqa: E402

PASS = FAIL = 0


def check(label, cond):
    global PASS, FAIL
    PASS, FAIL = (PASS + 1, FAIL) if cond else (PASS, FAIL + 1)
    print(f"  {'ok ' if cond else 'FAIL'} {label}")


def pump(seconds=0.3):
    end = time.time() + seconds
    ctx = GLib.MainContext.default()
    while time.time() < end:
        while ctx.pending():
            ctx.iteration(False)
        time.sleep(0.01)


fake = FakePhone(ip="127.0.0.1")
glue.ADB_FACTORY = lambda: Adb(runner=fake, adb_path="/usr/bin/adb")

ident = new_identity()
key = os.urandom(32)
device = pairing.PairedDevice(fingerprint="deadbeefcafef00d", name="Test Pixel", session_key=key, identity_pub=ident.public_b64)
device.save()
net = NetThread()
net.start()
server = ControlChannelServer(port=0)
net.run_coro(server.start()).result(5)
net.run_coro(server.serve_forever())
port = server._server.sockets[0].getsockname()[1]  # noqa: SLF001
app = Adw.Application(application_id="dev.phonebridge.synctest")
win = MainWindow(app, server, net)
win.set_visible(False)
# what PhoneBridgeApp._wire_server_events does for connection changes
server.on_connection_change(lambda conn, up: GLib.idle_add(win.on_connection_changed, conn.fingerprint, up,
                                                           (conn._writer.get_extra_info("peername") or ("",))[0]))  # noqa: SLF001


class Phone:
    """Fake phone on the real protocol: send/recv encrypted control envelopes."""
    def __init__(self):
        self.seq = 0
        self.inbox = []

    async def connect(self):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        self.r, self.w = await asyncio.open_connection("127.0.0.1", port, ssl=ctx)
        self.session = await phone_connect(self.r, self.w, device.fingerprint, key, ident, server.cert_fingerprint)
        self.cipher = self.session.cipher

    async def send(self, type_, payload):
        self.seq += 1
        self.w.write(crypto.pack_frame(self.cipher.encrypt(Envelope("control", type_, self.seq, payload).to_json())))
        await self.w.drain()

    async def pump_in(self, secs=0.5):
        end = time.time() + secs
        while time.time() < end:
            try:
                n = crypto.unpack_frame_len(await asyncio.wait_for(self.r.readexactly(crypto.FRAME_LEN_BYTES), 0.1))
                blob = await self.r.readexactly(n)
                env = Envelope.from_json(self.cipher.decrypt(blob))
                self.inbox.append(env)
                # The real phone answers a content probe by flickering an invisible overlay -> the encoder emits frames.
                if env.type == C.TYPE_COMMAND and env.payload.get("args", {}).get("probe_content"):
                    ctl.on_frame(300)
                    ctl.on_frame(300)
            except asyncio.TimeoutError:
                pass

    def of(self, type_, cmd=None):
        return [e.payload for e in self.inbox if e.type == type_ and (cmd is None or e.payload.get("cmd") == cmd)]


loop = asyncio.new_event_loop()
phone = Phone()


def run(coro, secs=None):
    return loop.run_until_complete(coro)


def wait_until(cond, timeout=6.0):
    """Keeps pumping GTK and the fake phone until `cond()` holds. GTK idle callbacks run late on a loaded machine, so checks on
    their effects must not assume a fixed delay."""
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        settle(0.3)
    return cond()


def settle(secs=0.8):
    """Alternates GTK-thread work and reading what the phone received, so cause and effect both get to run."""
    for _ in range(3):
        pump(0.2)
        run(phone.pump_in(secs / 3))
    pump(0.2)


run(phone.connect())
pump(0.8)
settle(0.5)
ctl = win._sync  # noqa: SLF001
check("link seen by the sync controller", ctl.link_up)

print("A. phone -> PC: user opens PhoneBridge on Android")
sid = "phonesession0001"
cmd = C.make(C.Cmd.OPEN_PHONE_BRIDGE_VIEWER, sid, C.Origin.USER)
run(phone.send(C.TYPE_COMMAND, cmd.to_payload()))
settle(1.0)
acks = phone.of(C.TYPE_ACK)
check("phone got a sync.ack for its command_id with result launched/already_open", any(a["command_id"] == cmd.command_id and a["ok"] and a["result"] in ("launched", "already_open") for a in acks))
check("viewer window presented, Screen mirror tab selected", wait_until(lambda: win._current_key == "mirror"))  # noqa: SLF001
check("PC adopted the phone's session id and started mirroring", ctl.session_id == sid and ctl.sm.state.value == "MIRROR_STARTING")
check("existing mirror.start was sent to the phone (reuse of the existing mirror path)", any(e.type == "mirror.start" for e in phone.inbox))
check("no OPEN_PHONE_BRIDGE echoed back to the phone (loop prevention)", not phone.of(C.TYPE_COMMAND, "OPEN_PHONE_BRIDGE"))
run(phone.send(C.TYPE_COMMAND, cmd.to_payload()))
settle(0.6)
check("replayed command acked as duplicate, not re-executed", [a for a in phone.of(C.TYPE_ACK) if a["command_id"] == cmd.command_id][-1]["result"] == "duplicate")
before = len(phone.of(C.TYPE_COMMAND, "START_MIRRORING"))

for _ in range(12):
    ctl.on_frame(2000)
    time.sleep(0.02)
GLib.timeout_add(1, lambda: False)
time.sleep(1.1)
settle(2.5)
check("frames confirmed -> MIRROR_READY delivered to the phone", len(phone.of(C.TYPE_COMMAND, "MIRROR_READY")) >= 1)
check("physical display turned off (fake adb) and state DISPLAY_OFF", not fake.panel_on and ctl.sm.state.value == "DISPLAY_OFF")
check("phone told DISPLAY_OFF performed", any(c["args"].get("performed") for c in phone.of(C.TYPE_COMMAND, "DISPLAY_OFF")))
win._sync_page.refresh()  # noqa: SLF001
rows = {k: r.get_subtitle() for k, r in win._sync_page._rows.items()}  # noqa: SLF001
check("dashboard shows device, mirroring state and display state", rows["device"] == "Test Pixel" and "display off" in rows["mirror"] and "Off" in rows["display"])

print("B. phone sends STOP_MIRRORING")
run(phone.send(C.TYPE_COMMAND, C.make(C.Cmd.STOP_MIRRORING, sid, C.Origin.USER).to_payload()))
settle(1.0)
check("display restored and session over", fake.panel_on and ctl.sm.state.value == "DISCONNECTED")
check("mirror.stop sent via the existing path", any(e.type == "mirror.stop" for e in phone.inbox))

print("C. security")
n_before = len(phone.inbox)
run(phone.send(C.TYPE_COMMAND, {"cmd": "DISPLAY_OFF", "session_id": sid, "command_id": "aaaaaaaaaaaaaaaa", "ts": int(time.time() * 1000), "origin": "user", "args": {}}))
settle(0.8)
check("command for an ended session is refused and display untouched", fake.panel_on and any(a["ok"] is False and a["command_id"] == "aaaaaaaaaaaaaaaa" for a in phone.of(C.TYPE_ACK)))
run(phone.send(C.TYPE_COMMAND, {"cmd": "REBOOT", "session_id": sid, "command_id": "bbbbbbbbbbbbbbbb", "ts": int(time.time() * 1000), "origin": "user", "args": {}}))
run(phone.send(C.TYPE_COMMAND, "garbage"))
settle(0.8)
check("unknown command / junk payload do not crash the PC and the link stays up", ctl.link_up and server.is_connected(device.fingerprint))
# permission: screen feature denied -> sync.* blocked like mirror.*
from phonebridge.security import permissions as P  # noqa: E402
P.set_mode(device, "screen", P.DENY)
settle(0.2)
run(phone.send(C.TYPE_COMMAND, C.make(C.Cmd.OPEN_PHONE_BRIDGE_VIEWER, "denied000000001", C.Origin.USER).to_payload()))
settle(0.8)
check("with the 'screen' permission denied, sync.command is blocked (permission.denied)", any(e.type == "permission.denied" and e.payload.get("feature") == "screen" for e in phone.inbox) and ctl.session_id == sid)
P.set_mode(device, "screen", P.ALLOW)

print("D. PC -> phone: user opens PhoneBridge on Ubuntu")
phone.inbox.clear()
ctl.stop_mirroring("reset")
settle(0.5)
ctl.launch.note_peer_status(False)   # the phone's status report says its app is closed
ctl.user_opened_viewer(C.Origin.USER)
settle(1.0)
opens = phone.of(C.TYPE_COMMAND, "OPEN_PHONE_BRIDGE")
check("PC sends exactly one OPEN_PHONE_BRIDGE (user origin) in APP_LAUNCHING", len(opens) == 1 and opens[0]["origin"] == "user" and ctl.sm.state.value == "APP_LAUNCHING")
ctl.user_opened_viewer(C.Origin.USER)
settle(0.5)
check("second open does not launch again", len(phone.of(C.TYPE_COMMAND, "OPEN_PHONE_BRIDGE")) == 1)
run(phone.send(C.TYPE_ACK, C.ack_payload(opens[0]["command_id"], True, "launched")))
settle(2.0)
check("after the phone's ack the mirror starts (mirror.start sent)", ctl.sm.state.value == "MIRROR_STARTING" and any(e.type == "mirror.start" for e in phone.inbox))

print("E. link loss restores the display")
for _ in range(12):
    ctl.on_frame(2000)
time.sleep(1.1)
settle(2.5)
check("display off while mirroring", not fake.panel_on)
phone.w.close()
pump(2.0)
check("phone disconnect: display restored immediately, state RECONNECTING", fake.panel_on and ctl.sm.state.value == "RECONNECTING")

print("F. dashboard settings")
page = win._sync_page  # noqa: SLF001
page._mode_row.set_selected(2)  # Disabled
page._off_sw.set_active(False)
check("changing Smart Screen Mode / toggles updates and persists settings", ctl.settings.smart_screen_mode == "disabled" and ctl.settings.auto_screen_off is False
      and '"smart_screen_mode": "disabled"' in (tmp / "screensync.json").read_text())

print(f"\n{PASS} passed, {FAIL} failed")
sys.stdout.flush()
os._exit(1 if FAIL else 0)
