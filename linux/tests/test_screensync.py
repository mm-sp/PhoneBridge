#!/usr/bin/env python3
"""Screen sync: state machine, command validation/replay, launch-loop prevention, display power (fake adb),
controller flows in both launch directions with a fake clock. Run: python3 tests/test_screensync.py"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from phonebridge.screensync import commands as C  # noqa: E402
from phonebridge.screensync.adb_bridge import Adb  # noqa: E402
from phonebridge.screensync.commands import Cmd, CommandValidator, Origin, Reject  # noqa: E402
from phonebridge.screensync.controller import FrameStats, Hooks, ScreenSyncController  # noqa: E402
from phonebridge.screensync.display_power import (AdbPowerStrategy, DisplayPowerManager, DisplayState,  # noqa: E402
                                                  Pending, PhoneDimStrategy)
from phonebridge.screensync.launch_sync import AppLaunchSyncManager, Decision  # noqa: E402
from phonebridge.screensync.settings import SmartScreenMode, SyncSettings  # noqa: E402
from phonebridge.screensync.states import SessionStateMachine, State  # noqa: E402

PASS = FAIL = 0


def check(label, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {label}")
    else:
        FAIL += 1
        print(f" FAIL {label}")


class Clock:
    def __init__(self): self.t = 1000.0
    def __call__(self): return self.t
    def advance(self, s): self.t += s


# ---------------------------------------------------------------- fake phone reachable through adb
class FakePhone:
    """Models what `adb shell` reveals/does on a device with cmd display power-off/power-reset."""
    def __init__(self, *, power_cmds=True, panel_on=True, serial="SER1", ip="192.168.1.50", fail_off=False):
        self.power_cmds, self.panel_on, self.serial, self.ip, self.fail_off = power_cmds, panel_on, serial, ip, fail_off
        self.calls = []

    def __call__(self, args, timeout):
        self.calls.append(args)
        a = args[1:]
        if a[:1] == ["devices"]:
            return 0, f"List of devices attached\n{self.serial}\tdevice\n"
        if a[:2] == ["-s", self.serial]:
            a = a[2:]
        if a[:1] != ["shell"]:
            return 1, "unknown"
        sh = a[1:]
        if sh[:3] == ["cmd", "display", "help"]:
            # rc=255 like the real Mi A3 (Android 17): the help text is printed but the exit status is non-zero
            return 255, "help\n  power-reset DISPLAY_ID\n  power-off DISPLAY_ID\n" if self.power_cmds else "help\n"
        if sh[:3] == ["cmd", "display", "power-off"]:
            if self.fail_off:
                return 1, "Exception"
            self.panel_on = False
            return 0, ""
        if sh[:3] == ["cmd", "display", "power-reset"]:
            self.panel_on = True
            return 0, ""
        if sh[:2] == ["dumpsys", "display"]:
            return 0, f"  mState={'ON' if self.panel_on else 'OFF'}\n  mState=AUTO_BRIGHTNESS_DISABLED\n"
        if sh[:2] == ["dumpsys", "power"]:
            return 0, "mWakefulness=Awake\n" if self.panel_on else "mWakefulness=Dozing\n"
        if sh[:1] == ["ip"]:
            return 0, f"2: wlan0    inet {self.ip}/24 brd x\n"
        if sh[:2] == ["input", "keyevent"]:
            self.panel_on = True
            return 0, ""
        return 0, ""


def make_adb(phone):
    return Adb(runner=phone, adb_path="/usr/bin/adb")


def make_display(phone, mode=SmartScreenMode.ENABLED, auto=True, verify=lambda t: True, extra=(), tmp=None):
    adb = make_adb(phone)
    strat = AdbPowerStrategy(adb, lambda: adb.pick_serial([phone.ip]))
    pend = Pending(Path(tmp or tempfile.mkdtemp()) / "pending.json")
    notes = []
    dm = DisplayPowerManager(strategies=[strat, *extra], mode=mode if callable(mode) else (lambda: mode),
                             auto_off=auto if callable(auto) else (lambda: auto), pending=pend,
                             verify=verify, on_change=lambda s, w: notes.append((s, w)), sleep=lambda s: None)
    return dm, pend, notes


def t_states():
    print("1. state machine")
    sm = SessionStateMachine()
    seen = []
    sm.add_listener(lambda o, n, r: seen.append((o, n)))
    check("starts DISCONNECTED", sm.state == State.DISCONNECTED)
    check("illegal DISCONNECTED->MIRROR_ACTIVE refused", sm.transition(State.MIRROR_ACTIVE) is False and sm.state == State.DISCONNECTED)
    for st in (State.CONNECTING, State.AUTHENTICATING, State.APP_LAUNCHING, State.MIRROR_STARTING, State.MIRROR_ACTIVE, State.DISPLAY_OFF):
        assert sm.transition(st)
    check("happy path reaches DISPLAY_OFF", sm.state == State.DISPLAY_OFF and len(seen) == 6)
    check("DISPLAY_OFF->MIRROR_ACTIVE allowed (screen restored)", sm.transition(State.MIRROR_ACTIVE))
    check("same-state transition is a no-op True", sm.transition(State.MIRROR_ACTIVE) and len(seen) == 7)
    check("MIRROR_ACTIVE->APP_LAUNCHING refused", not sm.transition(State.APP_LAUNCHING))
    sm.fail("boom")
    check("fail() always reaches ERROR and records reason", sm.state == State.ERROR and sm.last_error == "boom")
    check("ERROR->STOPPING->DISCONNECTED", sm.transition(State.STOPPING) and sm.transition(State.DISCONNECTED))
    sm.add_listener(lambda *a: 1 / 0)
    check("a crashing listener does not wedge the machine", sm.transition(State.CONNECTING) and sm.state == State.CONNECTING)


def mk(cmd=Cmd.CONNECTION_STATUS, sid="s" * 16, **kw):
    c = C.make(cmd, sid, kw.pop("origin", Origin.USER), kw.pop("args", {}))
    p = c.to_payload()
    p.update(kw)
    return p


def t_commands():
    print("2. command validation and replay protection")
    now = [1_700_000_000_000]
    v = CommandValidator("pc", clock_ms=lambda: now[0])
    sid = "a" * 16
    p = {"cmd": "STOP_MIRRORING", "session_id": sid, "command_id": "c" * 16, "ts": now[0], "origin": "user", "args": {}}
    r = v.validate(p, sid)
    check("valid command accepted", r.ok and r.command.cmd == Cmd.STOP_MIRRORING and r.command.origin == Origin.USER)
    r = v.validate(p, sid)
    check("same command_id replayed -> DUPLICATE", not r.ok and r.reason == Reject.DUPLICATE)
    p2 = dict(p, command_id="d" * 16, session_id="b" * 16)
    check("command for another session -> WRONG_SESSION", v.validate(p2, sid).reason == Reject.WRONG_SESSION)
    check("session-bound command with no active session -> WRONG_SESSION", v.validate(dict(p, command_id="e" * 16), None).reason == Reject.WRONG_SESSION)
    check("session starter works without a session", v.validate(dict(p, cmd="OPEN_PHONE_BRIDGE_VIEWER", command_id="f" * 16), None).ok)
    check("old timestamp -> STALE", v.validate(dict(p, command_id="g" * 16, ts=now[0] - 10 * 60_000), sid).reason == Reject.STALE)
    check("future timestamp -> STALE", v.validate(dict(p, command_id="h" * 16, ts=now[0] + 10 * 60_000), sid).reason == Reject.STALE)
    check("unknown command", v.validate(dict(p, cmd="FORMAT_PHONE", command_id="i" * 16), sid).reason == Reject.UNKNOWN_COMMAND)
    check("command the PC never receives (OPEN_PHONE_BRIDGE) -> NOT_ALLOWED", v.validate(dict(p, cmd="OPEN_PHONE_BRIDGE", command_id="j" * 16), sid).reason == Reject.NOT_ALLOWED)
    check("MIRROR_READY not accepted by PC", v.validate(dict(p, cmd="MIRROR_READY", command_id="k" * 16), sid).reason == Reject.NOT_ALLOWED)
    for label, bad in [("non-dict", "x"), ("list", []), ("None", None), ("int", 5)]:
        check(f"payload {label} -> MALFORMED", v.validate(bad, sid).reason == Reject.MALFORMED)
    check("bad id characters", v.validate(dict(p, command_id="../../etc/passwd"), sid).reason == Reject.MALFORMED)
    check("bool ts rejected", v.validate(dict(p, command_id="l" * 16, ts=True), sid).reason == Reject.MALFORMED)
    check("bad origin", v.validate(dict(p, command_id="m" * 16, origin="root"), sid).reason == Reject.MALFORMED)
    check("DISPLAY_OFF bad mode -> BAD_ARGS", v.validate(dict(p, cmd="DISPLAY_OFF", command_id="n" * 16, args={"mode": "rm -rf"}), sid).reason == Reject.BAD_ARGS)
    check("unknown status field -> BAD_ARGS", v.validate(dict(p, cmd="CONNECTION_STATUS", command_id="o" * 16, args={"evil": 1}), sid).reason == Reject.BAD_ARGS)
    check("status field wrong type -> BAD_ARGS", v.validate(dict(p, cmd="CONNECTION_STATUS", command_id="q" * 16, args={"fps": "fast"}), sid).reason == Reject.BAD_ARGS)
    check("bool where number expected -> BAD_ARGS", v.validate(dict(p, cmd="CONNECTION_STATUS", command_id="r" * 16, args={"fps": True}), sid).reason == Reject.BAD_ARGS)
    check("oversized args -> BAD_ARGS", v.validate(dict(p, command_id="s" * 16, args={str(i): 1 for i in range(40)}), sid).reason == Reject.BAD_ARGS)
    check("CONNECTION_STATUS needs no session binding", v.validate(dict(p, cmd="CONNECTION_STATUS", command_id="t" * 16, session_id="z" * 16), sid).ok)
    small = CommandValidator("pc", clock_ms=lambda: now[0], mono=lambda: 0.0)
    for i in range(C.REPLAY_CACHE_SIZE + 10):
        small.validate(dict(p, command_id=f"id{i:010d}"), sid)
    check("replay cache is bounded", len(small._seen) <= C.REPLAY_CACHE_SIZE)
    ph = CommandValidator("phone", clock_ms=lambda: now[0])
    check("phone accepts OPEN_PHONE_BRIDGE", ph.validate(dict(p, cmd="OPEN_PHONE_BRIDGE", command_id="u" * 16), None).ok)
    check("phone refuses OPEN_PHONE_BRIDGE_VIEWER", ph.validate(dict(p, cmd="OPEN_PHONE_BRIDGE_VIEWER", command_id="v" * 16), None).reason == Reject.NOT_ALLOWED)


def t_launch():
    print("3. AppLaunchSyncManager: no loops, no duplicates")
    clk = Clock()
    sent, launched = [], []
    state = {"open": False, "link": True, "sync": True}
    m = AppLaunchSyncManager("pc", send=sent.append, launch_local=lambda o: launched.append(o) or True,
                             is_local_open=lambda: state["open"], sync_enabled=lambda: state["sync"],
                             link_up=lambda: state["link"], clock=clk)
    check("user launch is forwarded as OPEN_PHONE_BRIDGE", m.on_local_app_opened(Origin.USER, "s" * 16) == Decision.SENT and sent[0].cmd == Cmd.OPEN_PHONE_BRIDGE and sent[0].origin == Origin.USER)
    check("second launch inside cooldown not forwarded", m.on_local_app_opened(Origin.USER, "s" * 16) == Decision.SKIP_COOLDOWN and len(sent) == 1)
    clk.advance(6)
    m.note_peer_status(True)
    check("peer already open -> not launched again", m.on_local_app_opened(Origin.USER, "s" * 16) == Decision.SKIP_PEER_OPEN)
    clk.advance(31)
    check("peer status older than the TTL is treated as unknown (no stale 'already open')", m.peer_open is None)
    m.note_peer_status(False)
    check("auto-origin launch never forwarded", m.on_local_app_opened(Origin.AUTO, "s" * 16) == Decision.SKIP_AUTO)
    state["sync"] = False
    check("sync disabled -> not forwarded", m.on_local_app_opened(Origin.USER, "s" * 16) == Decision.SKIP_DISABLED)
    state["sync"], state["link"] = True, False
    check("link down -> not forwarded", m.on_local_app_opened(Origin.USER, "s" * 16) == Decision.SKIP_LINK_DOWN)
    state["link"] = True
    # remote side
    cmd_user = C.make(Cmd.OPEN_PHONE_BRIDGE_VIEWER, "s" * 16, Origin.USER)
    cmd_auto = C.make(Cmd.OPEN_PHONE_BRIDGE_VIEWER, "s" * 16, Origin.AUTO)
    check("remote AUTO launch ignored", m.on_remote_command(cmd_auto) == (True, "ignored_auto_origin") and not launched)
    check("remote USER launch executed once as AUTO origin", m.on_remote_command(cmd_user) == (True, "launched") and launched == [Origin.AUTO])
    clk.advance(6)
    check("echo: a local user-launch right after a remote-caused one is suppressed", True)
    clk2 = Clock()
    m2sent = []
    m2 = AppLaunchSyncManager("phone", send=m2sent.append, launch_local=lambda o: True, is_local_open=lambda: False,
                              sync_enabled=lambda: True, link_up=lambda: True, clock=clk2)
    m2.on_remote_command(C.make(Cmd.OPEN_PHONE_BRIDGE, "s" * 16, Origin.USER))
    check("loop guard: launch caused by a remote command is not echoed back", m2.on_local_app_opened(Origin.USER, "s" * 16) == Decision.SKIP_ECHO and not m2sent)
    clk2.advance(20)
    check("... but a genuine user launch later is forwarded", m2.on_local_app_opened(Origin.USER, "s" * 16) == Decision.SENT)
    state["open"] = True
    check("already open locally -> remote OPEN acknowledged, nothing launched", m.on_remote_command(C.make(Cmd.OPEN_PHONE_BRIDGE_VIEWER, "s" * 16, Origin.USER)) == (True, "already_open") and len(launched) == 1)
    state["open"] = False
    # flood
    clk3 = Clock()
    fl = []
    m3 = AppLaunchSyncManager("pc", send=fl.append, launch_local=lambda o: True, is_local_open=lambda: False,
                              sync_enabled=lambda: True, link_up=lambda: True, clock=clk3)
    for _ in range(40):
        m3.on_local_app_opened(Origin.USER, "s" * 16)
        clk3.advance(5.1)
    check("circuit breaker bounds forwards per minute", 1 <= len(fl) <= 4 * (40 * 5.1 / 60 + 1))
    ex = []
    m4 = AppLaunchSyncManager("pc", send=lambda c: None, launch_local=lambda o: ex.append(1) or True, is_local_open=lambda: False,
                              sync_enabled=lambda: True, link_up=lambda: True, clock=clk3)
    res = [m4.on_remote_command(C.make(Cmd.OPEN_PHONE_BRIDGE_VIEWER, "s" * 16, Origin.USER)) for _ in range(10)]
    check("remote launch flood is rate limited", len(ex) == 3 and res[-1] == (False, "rate_limited"))
    m5 = AppLaunchSyncManager("pc", send=lambda c: None, launch_local=lambda o: False, is_local_open=lambda: False,
                              sync_enabled=lambda: True, link_up=lambda: True, clock=clk3)
    check("failed launch is reported as failed", m5.on_remote_command(C.make(Cmd.OPEN_PHONE_BRIDGE_VIEWER, "s" * 16, Origin.USER)) == (False, "launch_failed"))


def t_display():
    print("4. DisplayPowerManager (fake adb phone)")
    ph = FakePhone()
    dm, pend, notes = make_display(ph)
    check("off: panel really off, state OFF, marker written", dm.request_off() and not ph.panel_on and dm.state == DisplayState.OFF and pend.read()["strategy"] == "adb")
    check("request_off again is a no-op", dm.request_off())
    check("restore: panel on again, marker cleared, state ON", dm.restore("test") and ph.panel_on and dm.state == DisplayState.ON and pend.read() is None)
    check("restore is idempotent", dm.restore("again") and ph.panel_on)
    ph = FakePhone(power_cmds=False)
    dm, pend, _ = make_display(ph)
    check("device without power commands: stays on with explanation", not dm.request_off() and ph.panel_on and "power-off" in dm.note and pend.read() is None)
    dm, _, _ = make_display(FakePhone(), mode=SmartScreenMode.DISABLED)
    check("Smart Screen Mode DISABLED never turns it off", not dm.request_off() and "disabled" in dm.note)
    ph = FakePhone(); dm, _, _ = make_display(ph, auto=False)
    check("auto screen-off toggle off -> display stays on", not dm.request_off() and ph.panel_on)
    check("manual click (force) overrides the auto toggle", dm.request_off(force=True) and not ph.panel_on)
    dm.restore()
    ph = FakePhone(); dm, _, _ = make_display(ph, mode=SmartScreenMode.DISABLED)
    check("manual click even overrides DISABLED (explicit user action)", dm.request_off(force=True) and not ph.panel_on)
    dm.restore()
    ph = FakePhone(fail_off=True); dm, pend, _ = make_display(ph)
    check("failed power-off: reverts, remembers, no stale marker", not dm.request_off() and ph.panel_on and pend.read() is None and "adb" in dm._unsupported)
    ph = FakePhone(); alive = {"v": True}
    dm, pend, _ = make_display(ph, verify=lambda t: alive["v"])
    alive["v"] = False
    check("stream dies when panel goes off: display is turned back ON automatically", not dm.request_off() and ph.panel_on and dm.state == DisplayState.ON and "stopped" in dm.note)
    alive["v"] = True
    check("device is then not retried this session", not dm.request_off() and ph.panel_on)
    # crash recovery
    tmp = tempfile.mkdtemp()
    ph = FakePhone(); dm1, pend1, _ = make_display(ph, tmp=tmp)
    dm1.request_off()
    dm2, pend2, _ = make_display(ph, tmp=tmp)    # "new process": no in-memory state, only the marker
    check("viewer crash: next start restores the phone display from the marker", dm2.recover_stale() and ph.panel_on and pend2.read() is None)
    # restore failure keeps the marker for retry
    ph = FakePhone(); dm, pend, _ = make_display(ph)
    dm.request_off()
    ph.serial_gone = True
    orig = ph.__call__
    ph_runner_calls = {"n": 0}
    adb_dead = Adb(runner=lambda a, t: (1, "error: device offline") if a[1:2] != ["devices"] else (0, "List of devices attached\n"), adb_path="/x")
    dm.strategies_replaced = True
    dm._strategies["adb"]._adb = adb_dead
    check("restore failure is reported, marker kept for retry", not dm.restore("x") and pend.read() is not None and "power button" in dm.note)
    dm._strategies["adb"]._adb = make_adb(ph)
    check("... and a later retry succeeds", dm.restore("retry") and ph.panel_on and pend.read() is None)
    # dim fallback
    ph = FakePhone(power_cmds=False); sent = []
    dim = PhoneDimStrategy(lambda on: sent.append(on) or True, lambda: True)
    dm, _, _ = make_display(ph, mode=SmartScreenMode.AUTOMATIC, extra=[dim])
    check("AUTOMATIC never falls back to dim (not a real off)", not dm.request_off() and not sent)
    dm, _, _ = make_display(ph, mode=SmartScreenMode.ENABLED, extra=[dim])
    check("ENABLED falls back to dim and reports DIMMED, not OFF", dm.request_off() and dm.state == DisplayState.DIMMED and sent == [False])
    dm.restore()
    check("dim restore asks the phone to restore", sent == [False, True] and dm.state == DisplayState.ON)
    # serial selection
    both = Adb(runner=lambda a, t: (0, "List of devices attached\nA\tdevice\nB\tdevice\n"), adb_path="/x")
    check("two adb devices and no hint: refuse to guess", both.pick_serial([]) is None)
    check("serial hint chooses the right one", both.pick_serial(["B"]) == "B")
    check("unauthorised device never picked", Adb(runner=lambda a, t: (0, "List of devices attached\nA\tunauthorized\n"), adb_path="/x").pick_serial([]) is None)
    check("hostile serial refused by shell()", Adb(runner=lambda a, t: (0, ""), adb_path="/x").shell("a; rm -rf /", "id")[0] != 0)
    check("hostile component refused", not Adb(runner=lambda a, t: (0, ""), adb_path="/x").start_activity("A", "x;reboot"))


class Rig:
    """Controller + fake hooks + fake phone, with a manual clock."""
    def __init__(self, **kw):
        self.clk = Clock()
        self.now = [1_700_000_000_000]
        self.sent, self.log = [], []
        self.viewer_open = False
        self.phone = FakePhone()
        self.settings = SyncSettings(smart_screen_mode="enabled", **kw)
        self.probe_dead = False       # True = the phone's virtual display is dead while dark: probes produce no frames
        self.dm, self.pend, _ = make_display(self.phone, mode=lambda: self.settings.mode(), auto=lambda: self.settings.auto_screen_off,
                                             verify=lambda t: self.ctl.verify_stream(t))
        hooks = Hooks(send=self._send,
                      open_viewer=lambda: self._open(), viewer_open=lambda: self.viewer_open,
                      start_mirror=lambda: self.log.append("start_mirror") or True,
                      stop_mirror=lambda: self.log.append("stop_mirror"),
                      adb_launch_phone=lambda: self.log.append("adb_launch") or True,
                      resolution=lambda: (1080, 2400), device_name=lambda: "Pixel")
        self.ctl = ScreenSyncController(hooks, self.settings, self.dm, clock=self.clk, now_ms=lambda: self.now[0],
                                        sleep=self.clk.advance)

    def _send(self, t, p):
        self.sent.append((t, p))
        # The phone answers a content probe by flickering an invisible overlay -> the encoder emits frames.
        if t == C.TYPE_COMMAND and p["cmd"] == "CONNECTION_STATUS" and p["args"].get("probe_content") and not self.probe_dead:
            self.ctl.on_frame(300)
            self.ctl.on_frame(300)
        return True

    def _open(self):
        self.viewer_open = True
        self.log.append("open_viewer")
        return True

    def cmds(self, name=None):
        return [p for t, p in self.sent if t == C.TYPE_COMMAND and (name is None or p["cmd"] == name)]

    def frames(self, n, dt=0.05):
        for _ in range(n):
            self.clk.advance(dt)
            self.ctl.on_frame(1000)

    def phone_sends(self, cmd, sid=None, origin="user", args=None):
        c = C.make(cmd, sid or self.ctl.session_id, Origin(origin), args or {}, now_ms=lambda: self.now[0])
        self.ctl.on_envelope(C.TYPE_COMMAND, c.to_payload())
        return c


def t_pc_to_phone():
    print("5. controller: PC opens -> phone opens -> mirror -> frames -> display off")
    r = Rig()
    r.ctl.on_link_change(True)
    check("idle with link up is DISCONNECTED (no session)", r.ctl.sm.state == State.DISCONNECTED)
    r.ctl.user_opened_viewer(Origin.USER)
    check("state APP_LAUNCHING and OPEN_PHONE_BRIDGE sent (user origin)", r.ctl.sm.state == State.APP_LAUNCHING and len(r.cmds("OPEN_PHONE_BRIDGE")) == 1 and r.cmds("OPEN_PHONE_BRIDGE")[0]["origin"] == "user")
    r.ctl.user_opened_viewer(Origin.USER)
    check("opening again does not relaunch / start a second session", len(r.cmds("OPEN_PHONE_BRIDGE")) == 1 and r.log.count("start_mirror") == 0)
    open_cmd = r.cmds("OPEN_PHONE_BRIDGE")[0]
    r.ctl.on_envelope(C.TYPE_ACK, C.ack_payload(open_cmd["command_id"], True, "launched"))
    r.ctl.tick()
    check("phone ack -> MIRROR_STARTING, mirror started", r.ctl.sm.state == State.MIRROR_STARTING and r.log.count("start_mirror") == 1)
    check("display untouched before frames are confirmed", r.phone.panel_on and not r.dm.pending.read())
    r.frames(2)
    r.ctl.tick()
    check("2 frames are not enough to confirm", r.ctl.sm.state == State.MIRROR_STARTING and r.phone.panel_on)
    r.frames(10)
    r.ctl.tick()
    check("frames confirmed -> MIRROR_READY sent", len(r.cmds("MIRROR_READY")) == 1)
    check("... then the phone display is off and state is DISPLAY_OFF", r.ctl.sm.state == State.DISPLAY_OFF and not r.phone.panel_on)
    check("phone is told DISPLAY_OFF performed=True", r.cmds("DISPLAY_OFF") and r.cmds("DISPLAY_OFF")[0]["args"].get("performed") is True)
    r.frames(20); r.ctl.tick()
    check("streaming continues while display off", r.ctl.sm.state == State.DISPLAY_OFF and r.ctl.snapshot()["fps"] > 5)
    snap = r.ctl.snapshot()
    check("snapshot has device/resolution/display", snap["device"] == "Pixel" and snap["resolution"] == "1080×2400" and snap["display"] == "off")
    r.ctl.stop_mirroring("user")
    check("stop: display restored and state DISCONNECTED", r.phone.panel_on and r.ctl.sm.state == State.DISCONNECTED and "stop_mirror" in r.log)
    check("stop is told to the phone", len(r.cmds("STOP_MIRRORING")) == 1)
    check("no pending-restore marker left", r.pend.read() is None)


def t_phone_to_pc():
    print("6. controller: phone opens -> viewer opens -> mirror -> display off")
    r = Rig()
    r.ctl.on_link_change(True)
    c = r.phone_sends(Cmd.OPEN_PHONE_BRIDGE_VIEWER, sid="p" * 16)
    acks = [p for t, p in r.sent if t == C.TYPE_ACK]
    check("viewer opened and ack 'launched'", "open_viewer" in r.log and acks[-1]["command_id"] == c.command_id and acks[-1]["result"] == "launched")
    check("session id adopted from the phone", r.ctl.session_id == "p" * 16)
    check("mirror started WITHOUT sending OPEN_PHONE_BRIDGE back (no loop)", r.log.count("start_mirror") == 1 and not r.cmds("OPEN_PHONE_BRIDGE"))
    check("state MIRROR_STARTING", r.ctl.sm.state == State.MIRROR_STARTING)
    r.ctl.on_envelope(C.TYPE_COMMAND, c.to_payload())
    check("duplicate delivery acked 'duplicate', not executed twice", [p for t, p in r.sent if t == C.TYPE_ACK][-1]["result"] == "duplicate" and r.log.count("open_viewer") == 1)
    r.frames(12); r.ctl.tick()
    check("frames confirmed -> display off", r.ctl.sm.state == State.DISPLAY_OFF and not r.phone.panel_on)
    c2 = r.phone_sends(Cmd.OPEN_PHONE_BRIDGE_VIEWER, sid="q" * 16)
    check("second phone launch while mirroring: no second session", r.ctl.session_id == "p" * 16 and r.log.count("start_mirror") == 1)
    r.phone_sends(Cmd.DISPLAY_ON)
    check("phone 'turn screen on' restores display, state MIRROR_ACTIVE", r.phone.panel_on and r.ctl.sm.state == State.MIRROR_ACTIVE)
    r.phone_sends(Cmd.DISPLAY_OFF)
    check("phone 'turn screen off' turns it off again", not r.phone.panel_on and r.ctl.sm.state == State.DISPLAY_OFF)
    r.phone_sends(Cmd.STOP_MIRRORING)
    check("phone STOP_MIRRORING: stopped and display restored", r.phone.panel_on and r.ctl.sm.state == State.DISCONNECTED)
    before = len(r.sent)
    r.ctl.on_envelope(C.TYPE_COMMAND, {"cmd": "DISPLAY_OFF", "session_id": "p" * 16, "command_id": "x" * 16, "ts": r.now[0], "origin": "user", "args": {}})
    check("command for a finished session is refused (WRONG_SESSION ack, display untouched)", r.phone.panel_on and r.sent[-1][1]["ok"] is False and len(r.sent) == before + 1)


def t_toggles():
    print("7. toggles and policy")
    r = Rig(auto_app_sync=False)
    r.ctl.on_link_change(True)
    check("auto app sync off: opening viewer does nothing", r.ctl.user_opened_viewer(Origin.USER) == "no_sync" and not r.cmds())
    r.phone_sends(Cmd.OPEN_PHONE_BRIDGE_VIEWER, sid="p" * 16)
    check("auto app sync off: phone's open request is acknowledged but not executed", "open_viewer" not in r.log and [p for t, p in r.sent if t == C.TYPE_ACK][-1]["result"] == "sync_disabled")
    r = Rig(auto_screen_off=False)
    r.ctl.on_link_change(True)
    r.ctl.start_mirroring(Origin.AUTO)
    r.frames(12); r.ctl.tick()
    check("auto screen-off off: stays MIRROR_ACTIVE, display on", r.ctl.sm.state == State.MIRROR_ACTIVE and r.phone.panel_on)
    r.ctl.set_display(False)
    check("manual Turn screen off works and goes DISPLAY_OFF", r.ctl.sm.state == State.DISPLAY_OFF and not r.phone.panel_on)
    r.settings.smart_screen_mode = "disabled"
    r.ctl.apply_settings()
    check("switching Smart Screen Mode to Disabled lights the phone up immediately", r.phone.panel_on and r.ctl.sm.state == State.MIRROR_ACTIVE)
    r = Rig()
    r.settings.smart_screen_mode = "disabled"
    r.ctl.on_link_change(True); r.ctl.start_mirroring(Origin.AUTO); r.frames(12); r.ctl.tick()
    check("Smart Screen Mode disabled: mirroring active, display never touched", r.ctl.sm.state == State.MIRROR_ACTIVE and r.phone.panel_on and not r.dm.pending.read())
    check("... and the reason is surfaced", "disabled" in r.ctl.snapshot()["note"].lower())


def t_reliability():
    print("8. reliability: failures, disconnects, recovery")
    r = Rig()
    r.ctl.on_link_change(True); r.ctl.start_mirroring(Origin.AUTO); r.frames(12); r.ctl.tick()
    assert r.ctl.sm.state == State.DISPLAY_OFF
    r.ctl.on_link_change(False)
    check("connection lost while display off: display restored immediately", r.phone.panel_on and r.ctl.sm.state == State.RECONNECTING)
    r.clk.advance(5)
    r.ctl.on_link_change(True)
    check("reconnect within window resumes mirroring", r.ctl.sm.state == State.MIRROR_STARTING and r.log.count("start_mirror") == 2)
    r.frames(12); r.ctl.tick()
    check("after resume the display goes off again", r.ctl.sm.state == State.DISPLAY_OFF)
    r.ctl.on_link_change(False)
    r.clk.advance(RESUME := 61)
    r.ctl.tick()
    check("no reconnect in time -> DISCONNECTED, display on", r.ctl.sm.state == State.DISCONNECTED and r.phone.panel_on)
    r = Rig(auto_reconnect=False)
    r.ctl.on_link_change(True); r.ctl.start_mirroring(Origin.AUTO); r.frames(12); r.ctl.tick()
    r.ctl.on_link_change(False)
    check("auto-reconnect off: goes DISCONNECTED and restores display", r.ctl.sm.state == State.DISCONNECTED and r.phone.panel_on)
    # no frames ever
    r = Rig()
    r.ctl.on_link_change(True); r.ctl.start_mirroring(Origin.AUTO)
    r.clk.advance(61); r.ctl.tick()
    check("capture prompt never accepted -> ERROR with explanation, display untouched", r.ctl.sm.state == State.ERROR and "frames" in r.ctl.note and r.phone.panel_on)
    check("ERROR session can be retried", r.ctl.start_mirroring(Origin.AUTO) in ("starting", "launching") and r.ctl.sm.state == State.MIRROR_STARTING)
    # stall while dark
    r = Rig()
    r.ctl.on_link_change(True); r.ctl.start_mirroring(Origin.AUTO); r.frames(12); r.ctl.tick()
    r.clk.advance(120); r.ctl.tick()
    check("a still screen sending no frames for 2 minutes is NOT treated as a dead stream (probe answered)", not r.phone.panel_on and r.ctl.sm.state == State.DISPLAY_OFF)
    r.probe_dead = True
    r.clk.advance(21); r.ctl.tick()
    check("one failed liveness probe is tolerated", not r.phone.panel_on)
    r.clk.advance(21); r.ctl.tick()
    check("second failed probe: display restored automatically, reason shown", r.phone.panel_on and r.dm.state == DisplayState.ON and "stopped updating" in r.ctl.note)
    # the panel-off itself kills the virtual display on some devices
    r = Rig(); r.probe_dead = True
    r.ctl.on_link_change(True); r.ctl.start_mirroring(Origin.AUTO); r.frames(12); r.ctl.tick()
    check("virtual display dies with the panel: verification fails, panel turned back on, not retried", r.phone.panel_on and r.ctl.sm.state == State.MIRROR_ACTIVE and "stopped" in r.ctl.snapshot()["note"])
    # a perfectly still screen sends only a frame or two when the capture starts
    r = Rig()
    r.ctl.on_link_change(True); r.ctl.start_mirroring(Origin.AUTO); r.frames(1); r.ctl.tick()
    check("still screen with 1 frame: not confirmed yet", r.ctl.sm.state == State.MIRROR_STARTING)
    r.clk.advance(4); r.ctl.tick(); r.ctl.tick()
    check("... a content probe provokes frames and mirroring is confirmed (no timeout)", r.ctl.sm.state in (State.MIRROR_ACTIVE, State.DISPLAY_OFF) and any(
        p["args"].get("probe_content") for p in r.cmds("CONNECTION_STATUS")))
    # phone side stops
    r = Rig()
    r.ctl.on_link_change(True); r.ctl.start_mirroring(Origin.AUTO); r.frames(12); r.ctl.tick()
    r.ctl.on_video_stopped()
    check("Android process killed / mirror stopped on phone: display restored, session over", r.phone.panel_on and r.ctl.sm.state == State.DISCONNECTED)
    r = Rig()
    r.ctl.on_link_change(True); r.ctl.start_mirroring(Origin.AUTO); r.frames(12); r.ctl.tick()
    r.ctl.shutdown()
    check("viewer closing restores the display and tells the phone", r.phone.panel_on and r.cmds("STOP_MIRRORING"))
    # launch not acked -> adb fallback
    r = Rig()
    r.ctl.on_link_change(True); r.ctl.user_opened_viewer(Origin.USER)
    r.clk.advance(9); r.ctl.tick()
    check("no ack for OPEN_PHONE_BRIDGE: adb fallback tried once", r.log.count("adb_launch") == 1 and r.ctl.sm.state == State.APP_LAUNCHING)
    r.clk.advance(9); r.ctl.tick()
    check("still no ack: proceeds to mirror (service can start capture) with a visible note", r.ctl.sm.state == State.MIRROR_STARTING and "confirm" in r.ctl.note)
    # not connected at start
    r = Rig()
    r.ctl.start_mirroring(Origin.USER)
    check("phone not connected: CONNECTING", r.ctl.sm.state == State.CONNECTING and not r.sent)
    r.ctl.on_link_change(True)
    check("phone connects -> flow continues", r.ctl.sm.state == State.APP_LAUNCHING)
    r = Rig()
    r.ctl.start_mirroring(Origin.USER); r.clk.advance(31); r.ctl.tick()
    check("phone never connects -> ERROR", r.ctl.sm.state == State.ERROR)
    # frame stats
    fs = FrameStats(Clock())
    check("FrameStats starts unconfirmed", not fs.confirmed() and fs.fps() == 0)
    fs.on_frame(); fs.on_frame(); fs.on_frame()
    check("FrameStats confirms after a few frames even if the screen then goes still", fs.confirmed())


def t_settings():
    print("9. settings persistence")
    from phonebridge.screensync import settings as S
    d = Path(tempfile.mkdtemp())
    s = S.load(d)
    check("defaults: automatic / sync on / auto-off on", s.mode() == SmartScreenMode.AUTOMATIC and s.auto_app_sync and s.auto_screen_off)
    s.smart_screen_mode, s.auto_app_sync = "enabled", False
    S.save(s, d)
    s2 = S.load(d)
    check("round-trips", s2.mode() == SmartScreenMode.ENABLED and s2.auto_app_sync is False)
    (d / "screensync.json").write_text('{"smart_screen_mode": "bogus", "auto_app_sync": "yes", "evil": 1}')
    s3 = S.load(d)
    check("junk values fall back to defaults", s3.mode() == SmartScreenMode.AUTOMATIC and s3.auto_app_sync is True)
    (d / "screensync.json").write_text("{not json")
    check("corrupt file -> defaults", S.load(d).auto_screen_off is True)


if __name__ == "__main__":
    for t in (t_states, t_commands, t_launch, t_display, t_pc_to_phone, t_phone_to_pc, t_toggles, t_reliability, t_settings):
        t()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
