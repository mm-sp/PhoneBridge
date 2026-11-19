"""ScreenSyncController: the session orchestrator on the Ubuntu side.

It owns the session state machine and drives the existing pieces (control channel, mirror page, video sink) through
`Hooks`, so it has no GTK/asyncio dependency and is deterministic under test: time only moves in `tick()`.

Flows
  PC -> phone  (user opens PhoneBridge here):  start_mirroring(USER)
      AUTHENTICATING -> APP_LAUNCHING (OPEN_PHONE_BRIDGE, adb `am start` fallback) -> MIRROR_STARTING (mirror starts;
      the phone shows Android's own capture consent) -> frames confirmed -> MIRROR_ACTIVE (MIRROR_READY) -> display off.
  phone -> PC  (user opens PhoneBridge on the phone):  sync.command OPEN_PHONE_BRIDGE_VIEWER
      viewer opens here, then the same sequence from MIRROR_STARTING.
The phone display is only touched after frames are confirmed, and restored on every way out.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable

from . import commands as C
from .commands import Cmd, Command, CommandValidator, Origin, Reject
from .display_power import DisplayPowerManager, DisplayState
from .launch_sync import AppLaunchSyncManager
from .settings import SyncSettings
from .states import MIRRORING_STATES, SessionStateMachine, State

log = logging.getLogger("phonebridge.sync.controller")

CONFIRM_FRAMES = 3            # frames that must have arrived since the capture started (a still screen sends few)
CONFIRM_PROBE_AFTER_S = 3.0   # fewer than that after this long: provoke a frame with a content probe
PROBE_PERIOD_S = 20.0         # while the display is off: prove the stream is still live this often
PROBE_FAILS_TO_RESTORE = 2
APP_LAUNCH_TIMEOUT_S = 8.0
MIRROR_START_TIMEOUT_S = 60.0 # includes the user tapping "Start now" on Android's capture prompt
LINK_WAIT_S = 30.0
RESUME_WINDOW_S = 60.0
MAX_RESUME_ATTEMPTS = 3
STATUS_PERIOD_S = 5.0
STATS_LOG_PERIOD_S = 2.0


class FrameStats:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._times: deque[float] = deque(maxlen=256)
        self.total = 0
        self.last = 0.0
        self.bytes = 0

    def reset(self) -> None:
        self._times.clear()
        self.total = 0
        self.last = 0.0
        self.bytes = 0

    def on_mirror_error(self, message: str) -> None:
        """The phone refused or failed to start the capture (prompt declined, permission missing...)."""
        with self._lock:
            if self.sm.state in (State.MIRROR_STARTING, State.APP_LAUNCHING, State.AUTHENTICATING):
                self._fail(message or "the phone could not start screen capture")

    def on_frame(self, nbytes: int = 0) -> None:
        now = self._clock()
        self._times.append(now)
        self.total += 1
        self.bytes += nbytes
        self.last = now

    def fps(self, window: float = 3.0) -> float:
        now = self._clock()
        n = sum(1 for t in self._times if now - t <= window)
        return n / window

    def age(self) -> float:
        return self._clock() - self.last if self.total else float("inf")

    def confirmed(self) -> bool:
        return self.total >= CONFIRM_FRAMES

    def alive(self, within: float = 2.5) -> bool:
        return self.total > 0 and self.age() <= within


@dataclass
class Hooks:
    send: Callable[[str, dict], bool]
    open_viewer: Callable[[], bool] = lambda: True
    viewer_open: Callable[[], bool] = lambda: True
    start_mirror: Callable[[], bool] = lambda: True
    stop_mirror: Callable[[], None] = lambda: None
    adb_launch_phone: Callable[[], bool] = lambda: False
    run_bg: Callable[[Callable[[], None]], None] = lambda fn: fn()
    resolution: Callable[[], tuple[int, int] | None] = lambda: None
    device_name: Callable[[], str] = lambda: ""
    notify: Callable[[str], None] = lambda msg: None
    on_update: Callable[[], None] = lambda: None


class ScreenSyncController:
    def __init__(self, hooks: Hooks, settings: SyncSettings, display: DisplayPowerManager, *,
                 clock: Callable[[], float] = time.monotonic, now_ms: Callable[[], int] | None = None,
                 stats: FrameStats | None = None, sleep: Callable[[float], None] = time.sleep) -> None:
        self.h, self.settings, self.display = hooks, settings, display
        self._clock, self._sleep = clock, sleep
        self._now_ms = now_ms or (lambda: int(time.time() * 1000))
        self.stats = stats or FrameStats(clock)
        self.sm = SessionStateMachine(clock)
        self.validator = CommandValidator("pc", clock_ms=self._now_ms, mono=clock)
        self.launch = AppLaunchSyncManager(
            "pc", send=self._send_cmd, launch_local=self._launch_viewer, is_local_open=hooks.viewer_open,
            sync_enabled=lambda: self.settings.auto_app_sync, link_up=lambda: self.link_up, clock=clock)
        self._lock = threading.RLock()
        self.session_id = ""
        self.link_up = False
        self.latency_ms: float | None = None
        self.peer: dict = {}
        self.note = ""
        self._deadline = 0.0
        self._phase_start = 0.0
        self._launch_acked = False
        self._adb_tried = False
        self._resume_until = 0.0
        self._resume_attempts = 0
        self._next_status = 0.0
        self._next_stats_log = 0.0
        self._last_bytes = 0
        self._probes: dict[int, float] = {}
        self._display_busy = False
        self._next_probe = 0.0
        self._probe_fails = 0
        self._probing = False
        self._probe_unavailable = False
        self.phone_supports_dim = False
        self.sm.add_listener(lambda *_: self.h.on_update())

    # ---------------------------------------------------------------- lifecycle
    def startup(self) -> None:
        """After a crash with the phone's display off, put it back before anything else."""
        self.h.run_bg(lambda: self.display.recover_stale())

    def shutdown(self) -> None:
        """Window closing / app exiting: never leave the phone dark."""
        if self.sm.state in MIRRORING_STATES or self.display.state != DisplayState.ON or self.display.pending.read():
            self.display.restore("PhoneBridge is closing")
        if self.link_up and self.session_id:
            self._send_cmd(C.make(Cmd.STOP_MIRRORING, self.session_id, Origin.AUTO, {"reason": "pc_closing"}))

    # ---------------------------------------------------------------- user/UI entry points
    def user_opened_viewer(self, origin: Origin = Origin.USER) -> str:
        """The PhoneBridge window was opened here. With auto-sync, bring the phone side up and start mirroring."""
        if not self.settings.auto_app_sync or origin != Origin.USER:
            return "no_sync"
        return self.start_mirroring(origin)

    def start_mirroring(self, origin: Origin = Origin.USER, session_id: str | None = None) -> str:
        with self._lock:
            st = self.sm.state
            if st in (State.MIRROR_STARTING, State.MIRROR_ACTIVE, State.DISPLAY_OFF, State.APP_LAUNCHING,
                      State.AUTHENTICATING, State.CONNECTING):
                self.h.open_viewer()
                return "already_active"        # one mirroring session at a time
            if st == State.STOPPING:
                return "busy_stopping"
            if st == State.ERROR:
                self.sm.transition(State.DISCONNECTED, "retry")
            self.session_id = session_id or C.new_id()
            self.note = ""
            self.stats.reset()
            self._resume_attempts = 0
            if not self.link_up:
                self.sm.transition(State.CONNECTING, "waiting for the phone to connect")
                self._deadline = self._clock() + LINK_WAIT_S
                return "connecting"
            self.sm.transition(State.AUTHENTICATING, "control link is authenticated")
            return self._begin_launch(origin)

    def _begin_launch(self, origin: Origin) -> str:
        if origin == Origin.USER and self.settings.auto_app_sync and self.launch.peer_open is not True:
            self.sm.transition(State.APP_LAUNCHING, "opening PhoneBridge on the phone")
            self._launch_acked = False
            self._adb_tried = False
            self._phase_start = self._clock()
            self._deadline = self._phase_start + APP_LAUNCH_TIMEOUT_S
            self.launch.on_local_app_opened(Origin.USER, self.session_id)
            return "launching"
        return self._begin_mirror()

    def _begin_mirror(self) -> str:
        if not self.sm.transition(State.MIRROR_STARTING, "starting screen capture"):
            return "invalid_state"
        self.stats.reset()
        self._phase_start = self._clock()
        self._deadline = self._phase_start + MIRROR_START_TIMEOUT_S
        self._send_cmd(C.make(Cmd.START_MIRRORING, self.session_id, Origin.USER))
        if not self.h.start_mirror():
            self._fail("could not send the mirror request to the phone")
            return "failed"
        return "starting"

    def adopt_manual_start(self) -> None:
        """The user pressed Start on the Screen mirror tab themselves: track it as a session (no launch commands)."""
        with self._lock:
            if self.sm.state in (State.DISCONNECTED, State.ERROR):
                if self.sm.state == State.ERROR:
                    self.sm.transition(State.DISCONNECTED, "manual start")
                self.session_id = C.new_id()
                self.stats.reset()
                if self.sm.transition(State.AUTHENTICATING, "manual start"):
                    self.sm.transition(State.MIRROR_STARTING, "manual start")
                    self._phase_start = self._clock()
                    self._deadline = self._phase_start + MIRROR_START_TIMEOUT_S

    def stop_mirroring(self, reason: str = "user", notify_phone: bool = True, stop_local: bool = True) -> None:
        with self._lock:
            st = self.sm.state
            if st in (State.DISCONNECTED, State.STOPPING):
                self.display.restore(reason) if self.display.pending.read() else None
                return
            if st == State.ERROR:
                self.sm.transition(State.STOPPING, reason)
            elif not self.sm.transition(State.STOPPING, reason):
                self.sm.fail(f"cannot stop from {st.value}")
                self.sm.transition(State.STOPPING, reason)
            sid = self.session_id
        if stop_local:
            self.h.stop_mirror()
        if notify_phone and self.link_up and sid:
            self._send_cmd(C.make(Cmd.STOP_MIRRORING, sid, Origin.USER, {"reason": reason}))
        self._restore_display_then(lambda: self._finish_stop(reason))

    def _finish_stop(self, reason: str) -> None:
        with self._lock:
            if self.sm.state == State.STOPPING:
                self.sm.transition(State.DISCONNECTED, reason)
            self.stats.reset()
            self.note = ""

    link_kind: str = ""   # "usb" / "lan" / "relay" - how the phone is connected right now

    def set_display(self, on: bool) -> str:
        """Manual 'Turn screen on/off' button. An explicit click overrides the automatic toggle."""
        if on:
            self._restore_display_then(lambda: self._display_back())
            return "restoring"
        if self.sm.state not in (State.MIRROR_ACTIVE, State.DISPLAY_OFF):
            return "not_mirroring"
        self._try_display_off(force=True)
        return "turning_off"

    def apply_settings(self) -> None:
        """Settings changed in the UI. Switching Smart Screen Mode to Disabled restores a dark phone right away."""
        from .settings import SmartScreenMode
        if self.settings.mode() == SmartScreenMode.DISABLED and self.sm.state == State.DISPLAY_OFF:
            self.set_display(True)
        elif self.sm.state == State.MIRROR_ACTIVE and self.settings.auto_screen_off:
            self._try_display_off()
        self.h.on_update()

    # ---------------------------------------------------------------- events from the app
    def on_link_change(self, up: bool) -> None:
        with self._lock:
            was = self.link_up
            self.link_up = up
            st = self.sm.state
            if up == was:
                return
            if not up:
                self.peer = {}
                self.launch.peer_open = None
                self.latency_ms = None
                if st in MIRRORING_STATES or st == State.APP_LAUNCHING:
                    # The phone may be dark and we can no longer show it: light it up first (adb may still be reachable).
                    self._restore_display_then(lambda: None)
                    if self.settings.auto_reconnect:
                        self.sm.transition(State.RECONNECTING, "connection lost")
                        self._resume_until = self._clock() + RESUME_WINDOW_S
                        self.note = "Connection lost — reconnecting"
                    else:
                        self.sm.transition(State.DISCONNECTED, "connection lost")
                        self.h.stop_mirror()
                elif st == State.CONNECTING:
                    pass
                return
            # link came up
            if st == State.CONNECTING:
                self.sm.transition(State.AUTHENTICATING, "phone connected")
                self._begin_launch(Origin.USER)
            elif st == State.RECONNECTING:
                self._resume()
        self.h.on_update()

    def _resume(self) -> None:
        if self._clock() > self._resume_until or self._resume_attempts >= MAX_RESUME_ATTEMPTS:
            self.sm.transition(State.DISCONNECTED, "reconnected too late to resume")
            return
        self._resume_attempts += 1
        self.sm.transition(State.AUTHENTICATING, "reconnected")
        self.h.stop_mirror()   # clear the old viewer state; the phone's capture ended with the connection
        self._begin_mirror()

    def on_video_started(self, payload: dict) -> None:
        self.note = ""
        self.h.on_update()

    def on_video_stopped(self) -> None:
        """The phone ended the mirror (user pressed Stop on the phone, projection revoked, app killed)."""
        st = self.sm.state
        if st in MIRRORING_STATES:
            log.info("phone stopped the mirror")
            self.stop_mirroring("stopped on the phone", notify_phone=False)

    def on_mirror_error(self, message: str) -> None:
        """The phone refused or failed to start the capture (prompt declined, permission missing...)."""
        with self._lock:
            if self.sm.state in (State.MIRROR_STARTING, State.APP_LAUNCHING, State.AUTHENTICATING):
                self._fail(message or "the phone could not start screen capture")

    def on_frame(self, nbytes: int = 0) -> None:
        self.stats.on_frame(nbytes)

    # ---------------------------------------------------------------- periodic
    def tick(self) -> None:
        """Call about once a second. All timeouts and confirmations are evaluated here."""
        now = self._clock()
        with self._lock:
            st = self.sm.state
            if st == State.CONNECTING and now > self._deadline:
                self._fail("the phone did not connect in time")
            elif st == State.APP_LAUNCHING:
                if self._launch_acked:
                    self._begin_mirror()
                elif now > self._deadline:
                    if not self._adb_tried:
                        self._adb_tried = True
                        self._deadline = now + APP_LAUNCH_TIMEOUT_S
                        ok = self.h.adb_launch_phone()
                        self.note = ("Opened PhoneBridge on the phone with adb" if ok
                                     else "The phone did not confirm opening PhoneBridge")
                    else:
                        log.warning("app launch not confirmed; continuing, the service can still start the capture")
                        self.note = "Phone did not confirm opening the app; continuing"
                        self._begin_mirror()
            elif st == State.MIRROR_STARTING:
                if (0 < self.stats.total < CONFIRM_FRAMES and now - self._phase_start > CONFIRM_PROBE_AFTER_S
                        and not self._probing):
                    self._probing = True   # a still screen sends only a frame or two: provoke more
                    self.h.run_bg(lambda: (self.verify_stream(2.0), setattr(self, "_probing", False)))
                if self.stats.confirmed():
                    self.sm.transition(State.MIRROR_ACTIVE, f"{self.stats.total} frames received")
                    self.note = ""
                    self._send_cmd(C.make(Cmd.MIRROR_READY, self.session_id, Origin.AUTO,
                                          {"fps": round(self.stats.fps(), 1), **self._res_args()}))
                    if self.settings.auto_screen_off:
                        self._try_display_off()
                elif now > self._deadline:
                    self._fail("no screen frames arrived (was the capture prompt on the phone accepted?)")
            elif st == State.DISPLAY_OFF and now >= self._next_probe and not self._probing:
                self._next_probe = now + PROBE_PERIOD_S
                self._probing = True
                self.h.run_bg(self._periodic_probe)
            elif st == State.RECONNECTING and now > self._resume_until:
                self.sm.transition(State.DISCONNECTED, "could not reconnect in time")
                self.h.stop_mirror()
                self.note = "Reconnect timed out"
            if st in MIRRORING_STATES and now >= self._next_stats_log:
                dt = STATS_LOG_PERIOD_S if self._next_stats_log else 0
                kbps = (self.stats.bytes - self._last_bytes) / 1000 / dt if dt else 0.0
                self._last_bytes = self.stats.bytes
                self._next_stats_log = now + STATS_LOG_PERIOD_S
                log.info("mirror stats: state=%s display=%s frames=%d fps=%.1f rate=%.1f kB/s latency=%s", st.value,
                         self.display.state.value, self.stats.total, self.stats.fps(), kbps,
                         "-" if self.latency_ms is None else f"{self.latency_ms:.0f}ms")
            if self.link_up and now >= self._next_status and self.session_id:
                self._next_status = now + STATUS_PERIOD_S
                self._send_status(probe=True)
        self.h.on_update()

    # ---------------------------------------------------------------- stream liveness
    def verify_stream(self, timeout: float) -> bool:
        """Proves the stream is live *now*: asks the phone to cause a tiny, invisible content change and waits for a frame.
        Frame silence alone proves nothing (a static screen sends none), so this is the only liveness evidence we trust."""
        n0 = self.stats.total
        self._probe_unavailable = False
        self._send_status(probe_content=True)
        end = self._clock() + timeout
        while self._clock() < end:
            if self.stats.total > n0:
                return True
            if self.sm.state not in MIRRORING_STATES:
                return False
            self._sleep(0.25)
        return self.stats.total > n0

    def _periodic_probe(self) -> None:
        try:
            if self.sm.state != State.DISPLAY_OFF:
                return
            if self.verify_stream(5.0):
                self._probe_fails = 0
                return
            self._probe_fails += 1
            log.warning("stream liveness probe %d/%d failed with the display off", self._probe_fails, PROBE_FAILS_TO_RESTORE)
            if self._probe_fails >= PROBE_FAILS_TO_RESTORE:
                self.note = "Mirroring stopped updating while the phone display was off — display restored"
                self._probe_fails = 0
                self._restore_display_then(self._display_back)
        finally:
            self._probing = False

    # ---------------------------------------------------------------- display
    def _try_display_off(self, force: bool = False) -> None:
        if self._display_busy:
            return
        self._display_busy = True

        def work() -> None:
            try:
                ok = self.display.request_off("mirroring confirmed", force=force)
                with self._lock:
                    if ok and self.sm.state == State.MIRROR_ACTIVE:
                        self.sm.transition(State.DISPLAY_OFF, self.display.note)
                        self._send_cmd(C.make(Cmd.DISPLAY_OFF, self.session_id, Origin.AUTO,
                                              {"performed": True, "mode": self.display.active or ""}))
                    elif not ok:
                        self.note = self.display.note
                if not ok and force:   # the user clicked: tell them why nothing happened, not just a dim label
                    self.h.notify(self.display.note[:300] or "The phone screen could not be turned off")
            finally:
                self._display_busy = False
                self.h.on_update()
        self.h.run_bg(work)

    def _restore_display_then(self, then: Callable[[], None]) -> None:
        def work() -> None:
            ok = self.display.restore("session ended")
            if not ok:
                self.h.notify("Could not turn the phone display back on — press its power button")
            then()
            self.h.on_update()
        self.h.run_bg(work)

    def _display_back(self) -> None:
        with self._lock:
            if self.sm.state == State.DISPLAY_OFF:
                self.sm.transition(State.MIRROR_ACTIVE, "display restored")
            if self.link_up and self.session_id:
                self._send_cmd(C.make(Cmd.DISPLAY_ON, self.session_id, Origin.AUTO, {"performed": True}))

    # ---------------------------------------------------------------- inbound sync.* messages
    def on_envelope(self, type_: str, payload: dict) -> None:
        if type_ == C.TYPE_ACK:
            self._on_ack(payload)
            return
        verdict = self.validator.validate(payload, self.session_id or None)
        cid = payload.get("command_id") if isinstance(payload, dict) else None
        if not verdict.ok:
            log.warning("rejected sync.command: %s (%s)", verdict.reason and verdict.reason.value, verdict.detail)
            if isinstance(cid, str) and verdict.reason in (Reject.DUPLICATE,):
                self._ack(cid, True, "duplicate")     # idempotent: already handled, don't execute again
            elif isinstance(cid, str):
                self._ack(cid, False, "", verdict.reason.value if verdict.reason else "rejected")
            return
        cmd = verdict.command
        if cmd is None:
            return
        try:
            ok, result = self._execute(cmd)
        except Exception:  # noqa: BLE001
            log.exception("sync command %s failed", cmd.cmd.value)
            ok, result = False, "internal_error"
        self._ack(cmd.command_id, ok, result)

    def _execute(self, cmd: Command) -> tuple[bool, str]:
        c = cmd.cmd
        if c == Cmd.OPEN_PHONE_BRIDGE_VIEWER:
            ok, result = self.launch.on_remote_command(cmd)
            if ok and result in ("launched", "already_open") and cmd.origin == Origin.USER and self.settings.auto_app_sync:
                self.peer["app_open"] = True
                self.launch.note_peer_status(True)
                self.start_mirroring(Origin.AUTO, session_id=cmd.session_id)
            return ok, result
        if c == Cmd.START_MIRRORING:
            if not self.settings.auto_app_sync:
                return True, "sync_disabled"
            return True, self.start_mirroring(Origin.AUTO, session_id=cmd.session_id)
        if c == Cmd.STOP_MIRRORING:
            self.stop_mirroring("stopped from the phone", notify_phone=False)
            return True, "stopping"
        if c == Cmd.DISPLAY_OFF:
            if self.sm.state not in (State.MIRROR_ACTIVE, State.DISPLAY_OFF):
                return False, "not_mirroring"
            return True, self.set_display(False)
        if c == Cmd.DISPLAY_ON:
            return True, self.set_display(True)
        if c == Cmd.CONNECTION_STATUS:
            a = cmd.args
            if "echo_ts" in a:
                t0 = self._probes.pop(a["echo_ts"], None)
                if t0 is not None:
                    self.latency_ms = (self._clock() - t0) * 1000
            if "app_open" in a:
                self.launch.note_peer_status(bool(a["app_open"]))
            self.peer.update({k: v for k, v in a.items() if k not in ("probe_ts", "echo_ts")})
            if "dim_ok" in a:
                self.phone_supports_dim = bool(a["dim_ok"])
            if "probe_ts" in a:   # answer the phone's probe so it can measure RTT too
                self._send_status(echo=a["probe_ts"])
            return True, "ok"
        return False, "unhandled"

    def _on_ack(self, p: dict) -> None:
        if not isinstance(p, dict):
            return
        result = str(p.get("result", ""))
        if result == "probe_unavailable":
            self._probe_unavailable = True
        if self.sm.state == State.APP_LAUNCHING and p.get("ok") and result in (
                "launched", "already_open", "sync_disabled", "ignored_auto_origin", "duplicate"):
            self._launch_acked = True
        if not p.get("ok"):
            log.info("phone refused a command: %s/%s %s", result, p.get("reason"), p.get("command_id"))
            if self.sm.state == State.APP_LAUNCHING and (result in ("launch_failed", "rate_limited") or
                                                         p.get("reason") in ("launch_failed", "rate_limited")):
                self.note = f"The phone could not open PhoneBridge ({result or p.get('reason')})"
                self._deadline = 0.0     # don't wait out the timeout: the next tick tries the adb fallback

    # ---------------------------------------------------------------- helpers
    def _res_args(self) -> dict:
        r = self.h.resolution()
        return {"width": r[0], "height": r[1]} if r else {}

    def _send_cmd(self, cmd: Command) -> None:
        if self.link_up:
            self.h.send(C.TYPE_COMMAND, cmd.to_payload())

    def _ack(self, command_id: str, ok: bool, result: str = "", reason: str = "") -> None:
        if self.link_up:
            self.h.send(C.TYPE_ACK, C.ack_payload(command_id, ok, result, reason))

    def _send_status(self, probe: bool = False, echo: int | None = None, probe_content: bool = False) -> None:
        args: dict = {"state": self.sm.state.value, "display": self.display.state.value,
                      "app_open": bool(self.h.viewer_open()), "mirroring": self.sm.state in MIRRORING_STATES,
                      "fps": round(self.stats.fps(), 1), **self._res_args()}
        if probe:
            ts = self._now_ms()
            args["probe_ts"] = ts
            self._probes[ts] = self._clock()
            if len(self._probes) > 32:
                self._probes.pop(next(iter(self._probes)))
        if echo is not None:
            args["echo_ts"] = echo
        if probe_content:
            args["probe_content"] = True
        self._send_cmd(C.make(Cmd.CONNECTION_STATUS, self.session_id or C.new_id(), Origin.AUTO, args))

    def _launch_viewer(self, origin: Origin) -> bool:
        return bool(self.h.open_viewer())

    def _fail(self, reason: str) -> None:
        log.error("session failed: %s", reason)
        self.note = reason
        self.sm.fail(reason)
        self.h.stop_mirror()
        self._restore_display_then(lambda: None)
        if self.link_up and self.session_id:
            self._send_cmd(C.make(Cmd.STOP_MIRRORING, self.session_id, Origin.AUTO, {"reason": "error"}))

    # ---------------------------------------------------------------- UI snapshot
    def snapshot(self) -> dict:
        res = self.h.resolution()
        return {
            "device": self.h.device_name(), "link_up": self.link_up, "state": self.sm.state.value,
            "mirroring": self.sm.state in MIRRORING_STATES, "display": self.display.state.value,
            "display_note": self.display.note, "fps": round(self.stats.fps(), 1),
            "resolution": f"{res[0]}×{res[1]}" if res else "—",
            "latency_ms": None if self.latency_ms is None else round(self.latency_ms),
            "note": self.note or (self.sm.last_error if self.sm.state == State.ERROR else ""),
            "session": self.session_id[:8], "peer_app_open": self.peer.get("app_open"),
        }
