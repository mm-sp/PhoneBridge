"""DisplayPowerManager: switches the phone's physical display off while mirroring continues, and always puts it back.

Honest summary of what Android allows (details in docs/SCREEN_SYNC.md):
  * MediaProjection alone cannot power the panel off; an ordinary app has no such API.
  * The panel can be powered off by the shell user (ADB / Wireless Debugging): `cmd display power-off 0`, the same
    SurfaceControl display-power call scrcpy's --turn-screen-off makes. It needs no root, but it does need ADB.
    Restoring is `cmd display power-reset 0` (there is no `power-on`).
  * Without ADB the only on-device option is *dimming*: a transparent overlay requesting minimum brightness. That is
    NOT "off" and is reported as such (DisplayState.DIMMED), never as OFF.
  * Locking via the accessibility service turns the panel off, but ends the capture on most devices, so it is not used.

Safety rules: the display is only turned off after frames are confirmed arriving; if they stop (or never resume) after the
panel goes off, we turn it straight back on and remember that this device cannot do it; every exit path calls restore();
the pending-restore marker survives a crash of this process and is honoured on the next start.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Protocol

from .adb_bridge import Adb
from .settings import SmartScreenMode

log = logging.getLogger("phonebridge.sync.display")


class DisplayState(str, Enum):
    ON = "on"
    OFF = "off"            # panel powered off (real)
    DIMMED = "dimmed"      # fallback: minimum brightness, panel still lit
    UNKNOWN = "unknown"


@dataclass
class StepResult:
    ok: bool
    detail: str = ""


class PowerStrategy(Protocol):
    name: str
    real_off: bool
    def probe(self) -> StepResult: ...
    def off(self) -> StepResult: ...
    def restore(self) -> StepResult: ...


class AdbPowerStrategy:
    """Real panel power-off through adb shell (no root)."""
    name = "adb"
    real_off = True

    def __init__(self, adb: Adb, serial_for: Callable[[], str | None]) -> None:
        self._adb, self._serial_for = adb, serial_for
        self.serial: str | None = None
        self.off_serial: str | None = None   # the device whose panel was turned off: the one to restore

    def _serial(self) -> str | None:
        self.serial = self._serial_for() or self.serial
        return self.serial

    def _restore_serial(self) -> str | None:
        current = self._serial()   # also (re)connects a configured Wireless Debugging address
        if self.off_serial and any(d.serial == self.off_serial and d.state == "device" for d in self._adb.devices()):
            return self.off_serial
        return current   # the same phone may now be reached another way (USB unplugged, Wireless Debugging left)

    def probe(self) -> StepResult:
        if not self._adb.available:
            return StepResult(False, "adb is not installed on this PC")
        s = self._serial()
        if s is None:
            return StepResult(False, "the PC cannot reach this phone over ADB. Plug in the USB cable (USB debugging on and authorised), or turn on Wireless Debugging in Developer options")
        if not self._adb.has_display_power_commands(s):
            return StepResult(False, "this Android build has no `cmd display power-off` (needs Android 14+/LineageOS 21+)")
        return StepResult(True, f"adb device {s}")

    def off(self) -> StepResult:
        s = self._serial()
        if s is None:
            return StepResult(False, "adb device not found")
        self.off_serial = s
        rc, out = self._adb.shell(s, "cmd", "display", "power-off", "0")
        if rc != 0:
            return StepResult(False, out.strip()[:200])
        state = self._adb.display_on(s)
        if state is True:
            return StepResult(False, "power-off accepted but the panel is still on")
        return StepResult(True, "panel powered off" if state is False else "power-off sent (state unreadable)")

    def restore(self) -> StepResult:
        s = self._restore_serial()
        if s is None:
            return StepResult(False, "adb device not found")
        rc, out = self._adb.shell(s, "cmd", "display", "power-reset", "0")
        ok = rc == 0
        if self._adb.display_on(s) is False or self._adb.interactive(s) is False:
            # power-reset returns the display to what PowerManager wants; if the phone itself slept, wake it.
            self._adb.shell(s, "input", "keyevent", "KEYCODE_WAKEUP")
        return StepResult(ok and self._adb.display_on(s) is not False, out.strip()[:200])


class PhoneDimStrategy:
    """Fallback performed by the phone app itself (no ADB): transparent overlay at minimum brightness."""
    name = "dim"
    real_off = False

    def __init__(self, send_display_cmd: Callable[[bool], bool], phone_supports: Callable[[], bool]) -> None:
        self._send, self._supports = send_display_cmd, phone_supports

    def probe(self) -> StepResult:
        return StepResult(True, "dim overlay") if self._supports() else StepResult(False, "phone app has no overlay permission")

    def off(self) -> StepResult:
        return StepResult(self._send(False), "asked the phone to dim")

    def restore(self) -> StepResult:
        return StepResult(self._send(True), "asked the phone to restore brightness")


class Pending:
    """On-disk marker: 'we turned this phone's display off and have not restored it'. Survives a crash."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def write(self, strategy: str, serial: str | None) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"strategy": strategy, "serial": serial, "pid": os.getpid(), "ts": time.time()}))
            os.replace(tmp, self.path)
        except OSError:
            log.exception("could not write pending-restore marker")

    def read(self) -> dict | None:
        try:
            d = json.loads(self.path.read_text())
            return d if isinstance(d, dict) else None
        except (OSError, ValueError):
            return None

    def clear(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            log.exception("could not remove pending-restore marker")


class DisplayPowerManager:
    VERIFY_WAIT_S = 4.0

    def __init__(self, *, strategies: list[PowerStrategy], mode: Callable[[], SmartScreenMode],
                 auto_off: Callable[[], bool], pending: Pending,
                 verify: Callable[[float], bool] = lambda timeout: True,
                 on_change: Callable[[DisplayState, str], None] = lambda s, why: None,
                 watchdog: Callable[[], None] | None = None,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic) -> None:
        self._strategies = {s.name: s for s in strategies}
        self._order = [s.name for s in strategies]
        self._mode, self._auto_off, self.pending = mode, auto_off, pending
        self._verify, self._on_change = verify, on_change
        self._watchdog = watchdog   # starts the out-of-process "restore if the app dies" safety net (display_watchdog.py)
        self._sleep, self._clock = sleep, clock
        self._lock = threading.RLock()
        self.state = DisplayState.ON
        self.active: str | None = None
        self.note = "Phone display is on"
        self._unsupported: dict[str, str] = {}   # strategy -> why it failed here; not retried this session
        self._restores = 0         # counts restore() calls that had something to undo
        self.interrupted = False   # the last request_off was overtaken (restored or no longer wanted): not a failure

    # --- policy ------------------------------------------------------------------------------------------------
    def choose(self, force: bool = False) -> tuple[PowerStrategy | None, str]:
        """Returns (strategy, explanation). Explanation is user-visible when strategy is None. `force` = the user
        clicked "Turn screen off": that overrides the automatic toggle and Smart Screen Mode."""
        mode = SmartScreenMode.ENABLED if force else self._mode()
        if mode == SmartScreenMode.DISABLED:
            return None, "Smart Screen Mode is disabled: the phone display stays on"
        if not force and not self._auto_off():
            return None, "Automatic screen-off is switched off"
        reasons = []
        for name in self._order:
            strat = self._strategies[name]
            if name in self._unsupported:
                reasons.append(f"{name}: {self._unsupported[name]}")
                continue
            if not strat.real_off and mode == SmartScreenMode.AUTOMATIC:
                reasons.append("dim: only used when Smart Screen Mode is Enabled (it is not a real screen-off)")
                continue
            p = strat.probe()
            if p.ok:
                return strat, p.detail
            reasons.append(f"{name}: {p.detail}")
        return None, "Cannot turn the display off — " + "; ".join(reasons) if reasons else "No screen-off method available"

    # --- operations --------------------------------------------------------------------------------------------
    def request_off(self, reason: str = "mirroring confirmed", force: bool = False,
                    wanted: Callable[[], bool] = lambda: True, unverifiable: Callable[[], str] = lambda: "") -> bool:
        """Turns the display off if policy allows. Call only after frames are confirmed. Returns True if it is now off/dimmed.
        `wanted` says whether the caller still wants it off (the session may end meanwhile); `unverifiable` returns why the
        stream check could not be made at all, or "" if its answer counts."""
        with self._lock:
            self.interrupted = not wanted()
            if self.interrupted:
                return False
            if self.state in (DisplayState.OFF, DisplayState.DIMMED):
                return True
            strat, why = self.choose(force)
            if strat is None:
                self._set(DisplayState.ON, why)
                return False
            self.pending.write(strat.name, getattr(strat, "serial", None))   # BEFORE acting: a crash must be recoverable
            r = strat.off()
            if not r.ok:
                log.warning("display %s failed: %s", strat.name, r.detail)
                self._unsupported[strat.name] = r.detail
                if self._restore_with(strat).ok:    # the attempt may have half-applied; undo it
                    self.pending.clear()
                self._set(DisplayState.ON, f"{strat.name} screen-off failed: {r.detail}")
                return False
            self.active = strat.name
            if strat.real_off and self._watchdog is not None:
                try:
                    self._watchdog()
                except Exception:  # noqa: BLE001 - a safety net must never break the feature it protects
                    log.exception("display watchdog failed to start")
            self._set(DisplayState.OFF if strat.real_off else DisplayState.DIMMED, f"{reason} ({r.detail})")
            restores = self._restores
        # Verify outside the lock: the stream must survive the panel going off.
        alive = self._verify_stream()
        with self._lock:
            # Restored or given up while checking: the answer says nothing about this method.
            self.interrupted = self._restores != restores or not wanted()
            if self.interrupted:
                if self._restores == restores:
                    self.restore("no longer wanted")
                return False
            if alive:
                return True
            why = unverifiable()
            if not why:
                why = f"{strat.name}: mirroring stopped when the display went off"
                self._unsupported[strat.name] = "mirroring stopped when the display went off"
            log.error("stream not confirmed after display %s; reverting (%s)", strat.name, why)
            if self.restore("stream not confirmed after screen-off"):
                self._set(DisplayState.ON, f"{why}, so it was turned back on")
            return False

    def _verify_stream(self) -> bool:
        """`verify(timeout)` must PROVE the stream is live with the panel off (it provokes a content change and waits for
        a frame). Silence is not proof of anything on its own: a static screen legitimately sends no frames."""
        try:
            return bool(self._verify(self.VERIFY_WAIT_S))
        except Exception:  # noqa: BLE001 - an unverifiable stream is treated as dead: the safe answer is "turn it back on"
            log.exception("stream verification failed")
            return False

    def _restore_with(self, strat: PowerStrategy) -> StepResult:
        r = StepResult(False, "")
        for attempt in range(3):
            r = strat.restore()
            if r.ok:
                break
            self._sleep(0.3 * (attempt + 1))
        return r

    def restore(self, reason: str = "") -> bool:
        """Idempotent. Always safe to call. Returns True when the phone display is believed to be on again (or never was off)."""
        with self._lock:
            marker = self.pending.read()
            if self.state == DisplayState.ON and self.active is None and marker is None:
                return True
            self._restores += 1
            name = self.active or (marker or {}).get("strategy")
            primary = self._strategies.get(name)
            if marker and marker.get("serial") and hasattr(primary, "off_serial"):
                primary.off_serial = str(marker["serial"])
            if primary is not None:
                # The method that turned it off first; if it cannot, every other method is tried as a last resort. A
                # powered-off panel counts as restored only by a method that can power it: asking the phone to undo a
                # dim only means the request was sent.
                order = [primary] + [s for s in self._strategies.values() if s is not primary]
                ok = False
                for strat in order:
                    if self._restore_with(strat).ok and (strat.real_off or not primary.real_off):
                        ok = True
                        break
            else:
                # Unknown method (marker without one): undo with all of them.
                done = [(s, self._restore_with(s).ok) for s in self._strategies.values()]
                need_real = any(s.real_off for s, _ok in done)
                ok = any(result and (s.real_off or not need_real) for s, result in done)
            if ok:
                self.pending.clear()
                self.active = None
                self._set(DisplayState.ON, reason or "display restored")
            else:
                log.error("COULD NOT RESTORE the phone display (%s); marker kept for retry", reason)
                self.note = "Could not restore the phone display — press its power button"
            return ok

    def recover_stale(self) -> bool:
        """At startup: a previous run died with the display off -> bring it back."""
        m = self.pending.read()
        if not m:
            return True
        log.warning("found pending display restore from an earlier run (%s); restoring", m)
        self.state = DisplayState.OFF
        self.active = m.get("strategy")
        return self.restore("recovered after an unclean exit")

    def _set(self, st: DisplayState, note: str) -> None:
        self.state, self.note = st, note
        try:
            self._on_change(st, note)
        except Exception:  # noqa: BLE001
            log.exception("display on_change failed")
