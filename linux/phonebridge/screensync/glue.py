"""Builds the Ubuntu-side screen-sync stack for the GTK app: one place that knows about both GTK and the screensync core."""
from __future__ import annotations

import logging
import threading

from gi.repository import GLib

from .. import pairing
from . import commands as C
from . import display_watchdog as watchdog_mod
from . import settings as S
from .adb_bridge import Adb
from .controller import Hooks, ScreenSyncController
from .display_power import AdbPowerStrategy, DisplayPowerManager, Pending, PhoneDimStrategy

log = logging.getLogger("phonebridge.sync")
ADB_FACTORY = Adb   # tests substitute a fake so they never touch a real phone


def build(win, server, net_thread) -> ScreenSyncController:
    """`win` is the MainWindow (GTK thread). Everything called from the net thread hops via GLib.idle_add."""
    settings = S.load()
    adb = ADB_FACTORY()
    cfg_dir = pairing.DEVICES_DIR.parent

    def phone_hints() -> list[str]:
        fp = win._fingerprint()  # noqa: SLF001
        hints = []
        if settings.adb_address:
            hints.append(settings.adb_address)
        if fp and win._connected.get(fp):  # noqa: SLF001
            hints.append(win._connected[fp])  # noqa: SLF001
        hints += list(getattr(server, "usb_serials", []) or [])
        return hints

    def serial() -> str | None:
        if settings.adb_address and adb.available:
            adb.connect(settings.adb_address)
        return adb.pick_serial(phone_hints())

    def send(type_: str, payload: dict) -> bool:
        fp = win._fingerprint()  # noqa: SLF001
        if fp is None:
            return False
        net_thread.run_coro(server.send(fp, "control", type_, payload))
        return True

    holder: dict = {}

    def dim_send(restore: bool) -> bool:
        ctl = holder["ctl"]
        cmd = C.Cmd.DISPLAY_ON if restore else C.Cmd.DISPLAY_OFF
        return send(C.TYPE_COMMAND, C.make(cmd, ctl.session_id or C.new_id(), C.Origin.AUTO,
                                           {"performed": False, "mode": "dim"}).to_payload())

    display = DisplayPowerManager(
        strategies=[AdbPowerStrategy(adb, serial), PhoneDimStrategy(dim_send, lambda: holder["ctl"].phone_supports_dim)],
        mode=lambda: S.SmartScreenMode.DISABLED if holder["ctl"].peer.get("smart") == "disabled" else settings.mode(),
        auto_off=lambda: settings.auto_screen_off,
        pending=Pending(cfg_dir / "screensync_pending.json"),
        watchdog=lambda: watchdog_mod.spawn(cfg_dir / "screensync_pending.json", adb._adb),
        verify=lambda timeout: holder["ctl"].verify_stream(timeout),
        on_change=lambda st, why: GLib.idle_add(win.sync_refresh))

    def open_viewer() -> bool:
        GLib.idle_add(win.sync_present_viewer)
        return True

    def start_mirror() -> bool:
        GLib.idle_add(win.sync_start_mirror_page)
        return True

    def adb_launch() -> bool:
        s = serial()
        return bool(s) and adb.start_activity(s, "dev.phonebridge/.MainActivity", "--es", "pb_origin", "auto")

    def run_bg(fn) -> None:
        threading.Thread(target=fn, daemon=True, name="pb-sync").start()

    hooks = Hooks(
        send=send, open_viewer=open_viewer, viewer_open=lambda: win.get_visible(),
        start_mirror=start_mirror, stop_mirror=lambda: GLib.idle_add(win.sync_stop_mirror_page),
        adb_launch_phone=adb_launch, run_bg=run_bg, resolution=win.sync_resolution,
        device_name=lambda: win._phone_name(win._fingerprint()) if win._fingerprint() else "",  # noqa: SLF001
        notify=lambda m: GLib.idle_add(win.toast, m), on_update=lambda: GLib.idle_add(win.sync_refresh))
    ctl = ScreenSyncController(hooks, settings, display)
    holder["ctl"] = ctl

    def probe(cb) -> None:
        def work() -> None:
            strat, why = display.choose(force=True)
            cb(f"{strat.name.upper()} — {why}" if strat else why, strat is not None)
        run_bg(work)
    ctl.probe_display = probe

    async def on_sync(conn, envelope) -> None:
        if conn.fingerprint == win._fingerprint():  # noqa: SLF001
            ctl.on_envelope(envelope.type, envelope.payload)

    server.on(C.TYPE_COMMAND, on_sync)
    server.on(C.TYPE_ACK, on_sync)
    ctl.startup()
    GLib.timeout_add_seconds(1, lambda: (ctl.tick(), True)[1])
    return ctl
