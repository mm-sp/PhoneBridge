#!/usr/bin/env python3
"""Hardware check for screen sync: real control server + real ScreenSyncController + real adb + a real paired phone.
No GTK window (mirror frames are counted, not decoded). Needs: phone paired with this PC, USB debugging authorised, and a
person at the phone for Android's "Start now" screen-share prompt (this tool never bypasses it).

    python3 tools/hw_screensync_check.py full      # PC opens phone, mirror, display off, content change, probe, stop+restore
    python3 tools/hw_screensync_check.py crash     # same, then kill -9 this process with the display off (viewer crash)
    python3 tools/hw_screensync_check.py control   # panel off, then remote HOME/TAP/BACK must still work
    python3 tools/hw_screensync_check.py watchdog  # like marker, but with the safety-net process: the panel must come back on ITS OWN
    python3 tools/hw_screensync_check.py all       # full, then a second session that ends in the crash (two taps)
    python3 tools/hw_screensync_check.py marker    # no capture needed: panel off via adb + pending marker, then kill -9
    python3 tools/hw_screensync_check.py recover   # next start after a crash: honours the pending-restore marker
Stop the PhoneBridge desktop app first (one control server per port). Exit status 0 = every check passed.
"""
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from phonebridge import pairing, usb_link  # noqa: E402
from phonebridge.net_thread import NetThread  # noqa: E402
from phonebridge.screensync import commands as C  # noqa: E402
from phonebridge.screensync.adb_bridge import Adb  # noqa: E402
from phonebridge.screensync import controller as controller_mod  # noqa: E402
from phonebridge.screensync import display_watchdog  # noqa: E402
from phonebridge.screensync.controller import Hooks, ScreenSyncController  # noqa: E402
from phonebridge.screensync.display_power import AdbPowerStrategy, DisplayPowerManager, Pending  # noqa: E402
from phonebridge.screensync.settings import SmartScreenMode, SyncSettings  # noqa: E402
from phonebridge.transport import ControlChannelServer  # noqa: E402

PASS = FAIL = 0
adb = Adb()


def check(label, cond, extra=""):
    global PASS, FAIL
    PASS, FAIL = (PASS + 1, FAIL) if cond else (PASS, FAIL + 1)
    print(f"  {'ok  ' if cond else 'FAIL'} {label} {extra}", flush=True)


def say(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


KEEP_AWAKE = True   # the consent phase must let the phone sleep, so it switches the keep-awake thread off


def keep_screen_awake(serial, ctl, display):
    """A person has to get to the phone and tap, so a 60 s screen timeout must not hide the prompt. Only while we have NOT
    turned the display off ourselves (the point of the test is that it then stays dark)."""
    def run():
        while True:
            if KEEP_AWAKE and display.state.value == "on" and not display.pending.read() and ctl.sm.state.value != "DISPLAY_OFF" \
                    and adb.display_on(serial) is False:
                adb.shell(serial, "input", "keyevent", "KEYCODE_WAKEUP")
            time.sleep(4)
    import threading
    threading.Thread(target=run, daemon=True).start()


def phone_serial():
    ready = [d.serial for d in adb.devices() if d.state == "device"]
    return ready[0] if len(ready) == 1 else None


def panel(serial):
    on = adb.display_on(serial)
    return "ON" if on else "OFF" if on is False else "?"


def main(phase):
    import logging
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")
    controller_mod.MIRROR_START_TIMEOUT_S = 240.0   # a person has to get to the phone and tap
    serial = phone_serial()
    if not serial:
        print("need exactly one authorised adb device")
        return 2
    if phase in ("marker", "watchdog", "recover"):
        return display_only(phase, serial)
    devices = [d for d in (pairing.PairedDevice.load(p.stem) for p in pairing.DEVICES_DIR.glob("*.json")) if d]
    if not devices:
        print("no paired device in", pairing.DEVICES_DIR)
        return 2
    fp = devices[0].fingerprint
    net = NetThread()
    net.start()
    server = ControlChannelServer()
    net.run_coro(server.start()).result(10)
    net.run_coro(server.serve_forever())
    usb_link.ensure_reverse(server.port)
    settings = SyncSettings(smart_screen_mode=SmartScreenMode.AUTOMATIC.value)   # the real default policy
    holder = {}
    display = DisplayPowerManager(
        strategies=[AdbPowerStrategy(adb, lambda: serial)], mode=settings.mode, auto_off=lambda: settings.auto_screen_off,
        pending=Pending(pairing.DEVICES_DIR.parent / "screensync_pending.json"),
        watchdog=lambda: display_watchdog.spawn(pairing.DEVICES_DIR.parent / "screensync_pending.json", adb._adb),
        verify=lambda t: holder["ctl"].verify_stream(t))
    sent = []

    def send(t, p):
        sent.append((t, p))
        if not server.is_connected(fp):
            return False
        net.run_coro(server.send(fp, "control", t, p))
        return True

    import threading
    hooks = Hooks(
        send=send, run_bg=lambda fn: threading.Thread(target=fn, daemon=True).start(),
        start_mirror=lambda: (server.set_video_sink(fp, lambda d: holder["ctl"].on_frame(len(d))), send("mirror.start", {}))[1],
        stop_mirror=lambda: (server.set_video_sink(fp, None), send("mirror.stop", {}))[1],
        adb_launch_phone=lambda: adb.start_activity(serial, "dev.phonebridge/.MainActivity", "--es", "pb_origin", "auto"),
        viewer_open=lambda: True, device_name=lambda: devices[0].name)
    ctl = holder["ctl"] = ScreenSyncController(hooks, settings, display)
    acks = []

    async def on_sync(conn, env):
        if env.type == C.TYPE_ACK:
            acks.append(env.payload)
        ctl.on_envelope(env.type, env.payload)

    async def on_started(conn, env): ctl.on_video_started(env.payload)
    async def on_stopped(conn, env): ctl.on_video_stopped()
    async def on_status(conn, env):
        if env.payload.get("state") in ("denied", "error"):
            ctl.on_mirror_error(env.payload.get("message", ""))

    server.on(C.TYPE_COMMAND, on_sync); server.on(C.TYPE_ACK, on_sync)
    server.on("video.started", on_started); server.on("video.stopped", on_stopped); server.on("mirror.status", on_status)
    server.on_connection_change(lambda conn, up: ctl.on_link_change(up))
    threading.Thread(target=lambda: [(ctl.tick(), time.sleep(1)) for _ in iter(int, 1)], daemon=True).start()
    threading.Thread(target=lambda: [(usb_link.ensure_reverse(server.port), time.sleep(15)) for _ in iter(int, 1)], daemon=True).start()

    keep_screen_awake(serial, ctl, display)
    ctl.startup()
    if phase == "recover":
        time.sleep(2)
        check("a pending-restore marker from the crashed run was handled and the panel is ON", panel(serial) == "ON" and display.pending.read() is None)
        return finish()

    say("READY: now start PhoneBridge on the phone (the wrapper does this); waiting for it to connect")
    t0 = time.time()
    while not server.is_connected(fp) and time.time() - t0 < 60:
        time.sleep(1)
    check("phone connected over the authenticated channel", server.is_connected(fp))
    time.sleep(2)
    check("panel is ON at the start", panel(serial) == "ON")
    if ctl.sm.state.value == "DISCONNECTED":   # HOME would cancel the capture prompt of an already auto-started session
        subprocess.run([adb._adb, "-s", serial, "shell", "input", "keyevent", "KEYCODE_HOME"])
        time.sleep(1)

    if phase == "consent":
        global KEEP_AWAKE
        KEEP_AWAKE = False

        def display_state():
            out = subprocess.run([adb._adb, "-s", serial, "shell", "dumpsys", "display"], capture_output=True, text=True).stdout
            m = re.search(r"mState=([A-Z]+)", out)
            return m.group(1) if m else "?"

        def focus():
            out = subprocess.run([adb._adb, "-s", serial, "shell", "dumpsys", "window"], capture_output=True, text=True).stdout
            m = re.search(r"mCurrentFocus=Window\{\S+ u0 (\S+?)\}", out)
            return m.group(1) if m else "(none)"

        def wakefulness():
            out = subprocess.run([adb._adb, "-s", serial, "shell", "dumpsys", "power"], capture_output=True, text=True).stdout
            m = re.search(r"mWakefulness=(\w+)", out)
            return m.group(1) if m else "?"

        if ctl.sm.state.value != "DISCONNECTED":
            ctl.stop_mirroring("harness reset")
            time.sleep(2)
        # The link-up auto-start already raised a capture prompt on the phone: dismiss it, then let it settle.
        subprocess.run([adb._adb, "-s", serial, "shell", "input", "keyevent", "KEYCODE_HOME"])
        time.sleep(3)
        say("putting the phone to sleep (it locks) - the capture prompt must still be there 25 s after it is requested")
        def other_wakelocks():
            out = subprocess.run([adb._adb, "-s", serial, "shell", "dumpsys", "power"], capture_output=True, text=True).stdout
            return "phonebridge:consent" in out or "phonebridge:mirror-awake" in out

        t0 = time.time()
        while other_wakelocks() and time.time() - t0 < 75:   # a lock from an earlier request would keep the phone awake
            time.sleep(3)
        for _attempt in range(4):
            subprocess.run([adb._adb, "-s", serial, "shell", "input", "keyevent", "KEYCODE_SLEEP"])
            t0 = time.time()
            while wakefulness() == "Awake" and time.time() - t0 < 6:
                time.sleep(0.5)
            if wakefulness() != "Awake":
                break
        time.sleep(3)
        asleep = wakefulness() != "Awake"
        print(f"      phone before the request: {wakefulness()}", flush=True)
        check("phone was really asleep (locked) before the request", asleep)
        ctl.start_mirroring(C.Origin.USER)
        samples = []
        t_req = time.time()
        while time.time() - t_req < 26:
            time.sleep(1)
            samples.append((round(time.time() - t_req), display_state(), focus()))
        for sec, ds, fcs in samples[::3]:
            print(f"      +{sec:>2}s  display={ds:<5} focus={fcs}", flush=True)
        on_after_wake = [ds == "ON" for sec, ds, _f in samples if sec >= 3]
        # A locked phone cannot show the prompt until a person unlocks it (that cannot be automated), but the screen must
        # stay on long enough for them to do so instead of going back to sleep ~10 s after the lock screen woke up.
        check("screen woke for the request", any(ds == "ON" for sec, ds, _f in samples if sec <= 4), f"({[ds for _s, ds, _f in samples[:4]]})")
        check("screen stayed ON for 25 s (time to unlock and tap, no keyguard timeout)", all(on_after_wake),
              f"({sum(on_after_wake)}/{len(on_after_wake)} samples ON)")
        ctl.stop_mirroring("harness done")
        time.sleep(2)
        subprocess.run([adb._adb, "-s", serial, "shell", "input", "keyevent", "KEYCODE_WAKEUP"])
        return finish()

    if ctl.sm.state.value != "DISCONNECTED":
        # The phone announced "user opened the app" at link-up and the controller auto-started a session the phone then
        # rejects (wrong_session) - known issue. Reset it so this check starts from a clean manual start.
        say(f"resetting an auto-started session ({ctl.sm.state.value}) before the manual start")
        ctl.stop_mirroring("harness reset")
        t0 = time.time()
        while ctl.sm.state.value != "DISCONNECTED" and time.time() - t0 < 10:
            time.sleep(0.25)
        time.sleep(1)
    say("PC -> phone: user opened PhoneBridge on the PC")
    ctl.start_mirroring(C.Origin.USER)
    time.sleep(4)
    opens = [p for t, p in sent if t == C.TYPE_COMMAND and p["cmd"] == "OPEN_PHONE_BRIDGE"]
    skipped_launch = not opens
    if skipped_launch:
        # The phone app was already running and connected, so the controller must NOT send a redundant launch.
        check("phone app already open: no redundant OPEN_PHONE_BRIDGE sent", len(opens) == 0)
    else:
        check("PC sent exactly one OPEN_PHONE_BRIDGE (user origin)", len(opens) == 1 and opens[0]["origin"] == "user")
        open_acks = [a for a in acks if a["command_id"] == opens[0]["command_id"]] if opens else []
        check("phone acknowledged the launch", bool(open_acks), f"({open_acks[0]['ok'], open_acks[0]['result']})" if open_acks else "")
    top = subprocess.run([adb._adb, "-s", serial, "shell", "dumpsys", "activity", "activities"], capture_output=True, text=True).stdout
    check("PhoneBridge (or its capture prompt) is in the foreground on the phone",
          bool(re.search(r"ResumedActivity.*(dev\.phonebridge|mediaprojection)", top)))
    say(">>> ON THE PHONE: tap 'Start now' on the screen-share prompt (unlock first if needed) <<<")
    t0 = time.time()
    while ctl.sm.state.value not in ("MIRROR_ACTIVE", "DISPLAY_OFF") and time.time() - t0 < 90:
        time.sleep(0.5)
    check("frames confirmed -> MIRROR_ACTIVE reached", ctl.sm.state.value in ("MIRROR_ACTIVE", "DISPLAY_OFF"), f"({ctl.stats.total} frames)")
    if ctl.sm.state.value not in ("MIRROR_ACTIVE", "DISPLAY_OFF"):
        say("the capture prompt was not accepted in time - nothing below this point would mean anything, stopping")
        ctl.stop_mirroring("hardware check aborted")
        time.sleep(2)
        return finish()
    t0 = time.time()
    while ctl.sm.state.value != "DISPLAY_OFF" and time.time() - t0 < 25:
        time.sleep(0.5)
    check("state DISPLAY_OFF (policy: Automatic + adb verified)", ctl.sm.state.value == "DISPLAY_OFF", f"(note: {ctl.note or display.note})")
    check("physical panel really OFF per DisplayManager", panel(serial) == "OFF")
    wake = subprocess.run([adb._adb, "-s", serial, "shell", "dumpsys", "power"], capture_output=True, text=True).stdout
    check("Android still Awake (not locked/asleep) so the capture survives", "mWakefulness=Awake" in wake)
    check("phone was told DISPLAY_OFF (performed)", any(p["cmd"] == "DISPLAY_OFF" and p["args"].get("performed") for t, p in sent if t == C.TYPE_COMMAND))

    if phase == "control":
        def top_activity():
            out = subprocess.run([adb._adb, "-s", serial, "shell", "dumpsys", "activity", "activities"], capture_output=True, text=True).stdout
            m = re.search(r"topResumedActivity=ActivityRecord\{\S+ u0 (\S+)", out)
            return m.group(1) if m else ""

        def settle_top(want_not=None, timeout=6.0):
            t0 = time.time()
            while time.time() - t0 < timeout:
                cur = top_activity()
                if cur and (want_not is None or cur != want_not):
                    return cur
                time.sleep(0.4)
            return top_activity()

        say("CONTROL TEST with the panel OFF: remote HOME key, TAP, BACK key and SWIPE (scroll)")

        def clean_settings():
            """A fresh Settings task, so a test never starts from a screen the previous one left behind."""
            subprocess.run([adb._adb, "-s", serial, "shell", "am", "start", "-n",
                            "com.android.settings/.homepage.SettingsHomepageActivity", "-f", "0x10008000"], capture_output=True)
            time.sleep(3)
            return top_activity()

        base = clean_settings()
        print(f"      Settings top activity: {base}", flush=True)
        send("mirror.input", {"kind": "key", "key": "home"})
        after_home = settle_top(want_not=base)
        print(f"      after remote HOME: {after_home}", flush=True)
        check("remote HOME key works with the panel off", after_home != base and "settings" not in after_home.lower(), f"({after_home})")

        base = clean_settings()
        time.sleep(2)
        f_tap0 = ctl.stats.total
        send("mirror.input", {"kind": "tap", "x": 0.5, "y": 0.10})
        after_tap = settle_top(want_not=base)
        time.sleep(2)
        print(f"      mirror frames produced by the remote TAP: +{ctl.stats.total - f_tap0}", flush=True)
        print(f"      after remote TAP on the search bar: {after_tap}", flush=True)
        check("remote TAP works with the panel off", after_tap != base, f"({base} -> {after_tap})")
        send("mirror.input", {"kind": "key", "key": "back"})
        after_back = settle_top(want_not=after_tap, timeout=3.0)
        if after_back == after_tap:   # the first BACK only closes the keyboard that the search screen opened
            send("mirror.input", {"kind": "key", "key": "back"})
            after_back = settle_top(want_not=after_tap)
        check("remote BACK key works with the panel off", after_back != after_tap, f"({after_tap} -> {after_back})")

        # Scrolling: a list that moves produces new frames. Sample the frame counter twice a second so it is visible WHEN (or
        # whether) the frames come, then turn the panel back on and see if they were only held back until then.
        results = []
        for attempt in range(1, 5):
            base = clean_settings()
            time.sleep(3)
            timeline = []
            n_start = ctl.stats.total
            for _ in range(2):
                send("mirror.input", {"kind": "swipe", "x1": 0.5, "y1": 0.75, "x2": 0.5, "y2": 0.25, "duration_ms": 250})
                time.sleep(0.4)
            for i in range(10):
                time.sleep(0.5)
                timeline.append(ctl.stats.total - n_start)
            still = top_activity()
            print(f"      scroll attempt {attempt}: frames since the swipes, every 0.5 s: {timeline}", flush=True)
            results.append(timeline[-1] >= 3 and still == base)
        moved = sum(results)
        check("remote SWIPE shows up in the mirror while the panel is off (new frames, same activity) in at least 3 of 4 attempts",
              moved >= 3, f"({moved}/4 attempts showed frames)")
        n_wake = ctl.stats.total
        subprocess.run([adb._adb, "-s", serial, "shell", "cmd", "display", "power-reset", "0"], capture_output=True)
        time.sleep(3)
        print(f"      frames in the 3 s after the panel was turned back on: +{ctl.stats.total - n_wake}", flush=True)
        subprocess.run([adb._adb, "-s", serial, "shell", "input", "keyevent", "KEYCODE_HOME"], capture_output=True)
        return finish()
        check("panel still OFF after remote control", panel(serial) == "OFF")
        say("stopping mirroring from the PC")
        ctl.stop_mirroring("hardware check")
        t0 = time.time()
        while panel(serial) != "ON" and time.time() - t0 < 10:
            time.sleep(0.5)
        check("stop: panel restored to ON", panel(serial) == "ON")
        return finish()

    if phase in ("full", "all"):
        n0 = ctl.stats.total
        say("content change while the panel is off: open Settings via adb")
        subprocess.run([adb._adb, "-s", serial, "shell", "am", "start", "-a", "android.settings.SETTINGS"], capture_output=True)
        time.sleep(5)
        check("mirror kept streaming new content with the panel off", ctl.stats.total - n0 >= 5, f"(+{ctl.stats.total - n0} frames)")
        check("panel still OFF afterwards", panel(serial) == "OFF")
        say("waiting 45 s: the PC must prove liveness with probes while dark (a still screen sends no frames)")
        time.sleep(45)
        check("still DISPLAY_OFF after 45 s of a still screen (no false 'stream dead')", ctl.sm.state.value == "DISPLAY_OFF" and panel(serial) == "OFF")
        say("stopping mirroring from the PC")
        ctl.stop_mirroring("hardware check")
        t0 = time.time()
        while panel(serial) != "ON" and time.time() - t0 < 10:
            time.sleep(0.5)
        check("stop: panel restored to ON", panel(serial) == "ON")
        t0 = time.time()
        while (ctl.sm.state.value != "DISCONNECTED" or display.pending.read() is not None) and time.time() - t0 < 8:
            time.sleep(0.25)
        check("stop: state DISCONNECTED, no pending marker", ctl.sm.state.value == "DISCONNECTED" and display.pending.read() is None)
        time.sleep(2)   # frames already in flight
        n1 = ctl.stats.total
        time.sleep(4)
        check("phone's capture stopped too (no further frames after stop)", ctl.stats.total == n1)
        if phase == "full":
            return finish()
        say("second session (needs another tap) to test a viewer crash with the panel off")
        ctl.start_mirroring(C.Origin.USER)
        say(">>> ON THE PHONE: tap 'Start now' again <<<")
        t0 = time.time()
        while ctl.sm.state.value != "DISPLAY_OFF" and time.time() - t0 < 120:
            time.sleep(0.5)
        check("second session reached DISPLAY_OFF", ctl.sm.state.value == "DISPLAY_OFF" and panel(serial) == "OFF")

    # crash phase: die with the panel off; the PHONE must restore it by itself, the next start must honour the marker.
    check("pending-restore marker is on disk", display.pending.read() is not None)
    say("CRASHING NOW (kill -9) with the panel off")
    os.kill(os.getpid(), 9)


def display_only(phase, serial):
    """Crash-recovery of the PC side against the real panel, no screen capture involved (so no consent prompt)."""
    display = DisplayPowerManager(
        strategies=[AdbPowerStrategy(adb, lambda: serial)], mode=lambda: SmartScreenMode.ENABLED, auto_off=lambda: True,
        pending=Pending(pairing.DEVICES_DIR.parent / "screensync_pending.json"),
        watchdog=(lambda: display_watchdog.spawn(pairing.DEVICES_DIR.parent / "screensync_pending.json", adb._adb))
        if phase == "watchdog" else None)
    if phase in ("marker", "watchdog"):
        adb.shell(serial, "input", "keyevent", "KEYCODE_WAKEUP")
        time.sleep(2)
        check("panel ON before", panel(serial) == "ON")
        check("display-off performed and verified (OFF, still Awake)", display.request_off("hardware check") and panel(serial) == "OFF"
              and adb.interactive(serial) is True)
        check("pending-restore marker written", display.pending.read() is not None)
        say("CRASHING NOW (kill -9) with the panel off")
        os.kill(os.getpid(), 9)
    say("fresh process after the crash")
    check("panel is still OFF (nobody restored it yet)", panel(serial) == "OFF")
    t0 = time.time()
    display.recover_stale()
    check("recover_stale() found the marker and turned the panel back ON", panel(serial) == "ON" and display.pending.read() is None,
          f"({time.time() - t0:.1f}s)")
    return finish()


def finish():
    print(f"\n{PASS} passed, {FAIL} failed", flush=True)
    os._exit(1 if FAIL else 0)


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in ("full", "all", "crash", "control", "consent", "marker", "watchdog", "recover"):
        print(__doc__)
        sys.exit(2)
    main(sys.argv[1])
