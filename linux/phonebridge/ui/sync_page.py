"""Screen mirroring dashboard: link/mirror/display status, live stats and the sync + Smart Screen Mode settings."""
from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from ..screensync.settings import SmartScreenMode  # noqa: E402

_MODES = [SmartScreenMode.AUTOMATIC, SmartScreenMode.ENABLED, SmartScreenMode.DISABLED]
_MODE_TITLES = ["Automatic", "Enabled", "Disabled"]
_MODE_HELP = {
    SmartScreenMode.AUTOMATIC: "Turns the phone display off only when a real screen-off is available and verified (needs ADB); otherwise leaves it on.",
    SmartScreenMode.ENABLED: "Turns the display off during mirroring. Without ADB it can only dim the screen (not off), and says so.",
    SmartScreenMode.DISABLED: "The phone display always stays on.",
}
_STATE_TEXT = {
    "DISCONNECTED": "Idle", "CONNECTING": "Waiting for the phone…", "AUTHENTICATING": "Authenticating…",
    "APP_LAUNCHING": "Opening PhoneBridge on the phone…", "MIRROR_STARTING": "Starting mirroring (accept the prompt on the phone)…",
    "MIRROR_ACTIVE": "Mirroring", "DISPLAY_OFF": "Mirroring · phone display off", "RECONNECTING": "Reconnecting…",
    "STOPPING": "Stopping…", "ERROR": "Error",
}
_DISPLAY_TEXT = {"on": "On", "off": "Off (powered down)", "dimmed": "Dimmed (not off)", "unknown": "Unknown"}


class SyncPage(Gtk.ScrolledWindow):
    def __init__(self, controller, on_save_settings, probe_display=None) -> None:
        super().__init__(hscrollbar_policy=Gtk.PolicyType.NEVER)
        self._ctl, self._save, self._probe = controller, on_save_settings, probe_display
        self._building = False
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18, margin_top=18, margin_bottom=18,
                      margin_start=18, margin_end=18)
        self.set_child(Adw.Clamp(maximum_size=760, child=box))

        status = Adw.PreferencesGroup(title="Screen mirroring")
        self._rows = {}
        for key, title in (("device", "Connected device"), ("conn", "Connection"), ("mirror", "Mirroring"),
                           ("display", "Phone display"), ("fps", "Frame rate"), ("res", "Resolution"),
                           ("lat", "Network latency")):
            row = Adw.ActionRow(title=title, subtitle="—", subtitle_selectable=True)
            row.add_css_class("property")
            status.add(row)
            self._rows[key] = row
        box.append(status)

        buttons = Gtk.Box(spacing=8, halign=Gtk.Align.START)
        self._start_btn = Gtk.Button(label="Start mirroring", css_classes=["suggested-action"])
        self._start_btn.connect("clicked", self._on_start_stop)
        self._display_btn = Gtk.Button(label="Turn phone screen off")
        self._display_btn.connect("clicked", self._on_display)
        buttons.append(self._start_btn)
        buttons.append(self._display_btn)
        box.append(buttons)

        self._note = Gtk.Label(xalign=0, wrap=True, css_classes=["dim-label"], selectable=True)
        box.append(self._note)

        prefs = Adw.PreferencesGroup(title="Synchronisation")
        self._sync_sw = Adw.SwitchRow(title="Automatic app synchronisation",
                                      subtitle="Opening PhoneBridge on one device opens it on the other and starts mirroring")
        self._off_sw = Adw.SwitchRow(title="Automatic screen-off",
                                     subtitle="Turn the phone display off once mirroring is confirmed")
        self._reconnect_sw = Adw.SwitchRow(title="Reconnect automatically",
                                           subtitle="Resume mirroring if the connection drops (the display is restored first)")
        self._mode_row = Adw.ComboRow(title="Smart Screen Mode", model=Gtk.StringList.new(_MODE_TITLES))
        for w in (self._sync_sw, self._off_sw, self._reconnect_sw):
            w.connect("notify::active", self._on_changed)
            prefs.add(w)
        self._mode_row.connect("notify::selected", self._on_changed)
        prefs.add(self._mode_row)
        box.append(prefs)

        self._method = Adw.PreferencesGroup(title="Screen-off method")
        self._method_row = Adw.ActionRow(title="Available on this phone", subtitle="Checking…")
        self._method.add(self._method_row)
        box.append(self._method)
        self._probe_tick = 0
        self._can_off = True
        self.load_settings()
        self.refresh()

    # ---------------------------------------------------------------- settings
    def load_settings(self) -> None:
        s = self._ctl.settings
        self._building = True
        self._sync_sw.set_active(s.auto_app_sync)
        self._off_sw.set_active(s.auto_screen_off)
        self._reconnect_sw.set_active(s.auto_reconnect)
        self._mode_row.set_selected(_MODES.index(s.mode()))
        self._mode_row.set_subtitle(_MODE_HELP[s.mode()])
        self._building = False

    def _on_changed(self, *_a) -> None:
        if self._building:
            return
        s = self._ctl.settings
        s.auto_app_sync = self._sync_sw.get_active()
        s.auto_screen_off = self._off_sw.get_active()
        s.auto_reconnect = self._reconnect_sw.get_active()
        s.smart_screen_mode = _MODES[self._mode_row.get_selected()].value
        self._mode_row.set_subtitle(_MODE_HELP[s.mode()])
        self._save(s)
        self._ctl.apply_settings()

    # ---------------------------------------------------------------- actions
    def _on_start_stop(self, _b) -> None:
        if self._ctl.snapshot()["mirroring"] or self._ctl.sm.state.value in ("CONNECTING", "APP_LAUNCHING", "AUTHENTICATING", "RECONNECTING"):
            self._ctl.stop_mirroring("user")
        else:
            from ..screensync.commands import Origin
            self._ctl.start_mirroring(Origin.USER)

    def _method_found(self, text: str, ok: bool) -> None:
        self._method_row.set_subtitle(text)
        self._can_off = ok
        self.refresh()

    def _on_display(self, _b) -> None:
        self._ctl.set_display(self._ctl.display.state.value != "on")

    # ---------------------------------------------------------------- view
    def on_shown(self) -> None:
        self._probe_tick = 0
        self.refresh()

    def refresh(self) -> None:
        snap = self._ctl.snapshot()
        r = self._rows
        r["device"].set_subtitle(snap["device"] or "No phone connected")
        link = "Connected (authenticated, encrypted)" if snap["link_up"] else "Not connected"
        r["conn"].set_subtitle(f"{link} · session {snap['session']}" if snap["session"] else link)
        r["mirror"].set_subtitle(_STATE_TEXT.get(snap["state"], snap["state"]))
        r["display"].set_subtitle(_DISPLAY_TEXT.get(snap["display"], snap["display"]))
        r["fps"].set_subtitle(f"{snap['fps']:.0f} fps" if snap["mirroring"] else "—")
        r["res"].set_subtitle(snap["resolution"] if snap["mirroring"] else "—")
        r["lat"].set_subtitle(f"{snap['latency_ms']} ms" if snap["latency_ms"] is not None else "—")
        active = snap["mirroring"] or snap["state"] in ("CONNECTING", "APP_LAUNCHING", "AUTHENTICATING", "RECONNECTING")
        self._start_btn.set_label("Stop mirroring" if active else "Start mirroring")
        self._start_btn.set_css_classes(["destructive-action"] if active else ["suggested-action"])
        mirroring = snap["state"] in ("MIRROR_ACTIVE", "DISPLAY_OFF")
        can = snap["display"] != "on" or (self._can_off and self._ctl.link_kind == "usb")     # turning it back on must always stay possible
        self._display_btn.set_sensitive(mirroring and can)
        self._display_btn.set_visible(mirroring and can)   # only shown while mirroring a phone connected by USB
        self._display_btn.set_tooltip_text(None if can else "Connect the phone by USB cable to turn its screen off")
        self._display_btn.set_label("Turn phone screen on" if snap["display"] != "on" else "Turn phone screen off")
        self._note.set_label(snap["note"] or snap["display_note"])
        if self._probe is not None and self.get_mapped() and self._probe_tick % 3 == 0:
            self._probe_tick += 1
            self._probe(lambda text, ok=True: GLib.idle_add(self._method_found, text, ok))
        else:
            self._probe_tick += 1
