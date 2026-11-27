"""Device tab: one card per paired phone with live status from `device.info`."""
from __future__ import annotations

import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from .. import pairing


def human_bytes(n: float | int | None) -> str:
    if n is None:
        return "—"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _battery_icon(pct: int | None, charging: bool) -> str:
    if pct is None:
        return "battery-missing-symbolic"
    level = "full" if pct >= 90 else "good" if pct >= 50 else "low" if pct >= 20 else "caution" if pct >= 8 else "empty"
    return f"battery-{level}{'-charging' if charging else ''}-symbolic"


def _signal_icon(level: int | None) -> str:
    names = ["none", "weak", "ok", "good", "excellent"]
    if level is None:
        return "network-cellular-offline-symbolic"
    return f"network-cellular-signal-{names[max(0, min(4, level))]}-symbolic"


def _wifi_icon(pct: int | None) -> str:
    if pct is None:
        return "network-wireless-offline-symbolic"
    level = "excellent" if pct >= 75 else "good" if pct >= 50 else "ok" if pct >= 25 else "weak"
    return f"network-wireless-signal-{level}-symbolic"


class _DeviceCard:
    _syncing = False

    def sync_pc_control(self, stored: pairing.PairedDevice) -> None:
        """Shows the stored state, which also changes elsewhere (the banner's "Revoke now", the Security page, a phone's
        request approved in a dialog). Showing it records nothing."""
        self.device.allow_pc_control, self.device.pc_control_until = stored.allow_pc_control, stored.pc_control_until
        active = self.device.pc_control_active()
        # A grant that has merely run out is left to _update_pc_control_label, which records the switch-off.
        if self._pc_control.get_active() != active and (active or not stored.allow_pc_control):
            self._syncing = True
            try:
                self._pc_control.set_active(active)
            finally:
                self._syncing = False
        self._update_pc_control_label()

    def _on_pc_control_toggled(self, button: Gtk.CheckButton) -> None:
        from ..security import permissions

        if self._syncing:
            return
        fresh = pairing.PairedDevice.load(self.device.fingerprint) or self.device
        permissions.set_mode(fresh, "remote_control", permissions.ALLOW if button.get_active() else permissions.DENY)
        self.device.allow_pc_control, self.device.pc_control_until = fresh.allow_pc_control, fresh.pc_control_until
        self._update_pc_control_label()

    def _update_pc_control_label(self) -> None:
        """Shows how long remote control stays on, and unticks the box once it has timed out."""
        import time

        d = self.device
        if d.pc_control_active():
            left = max(1, int((d.pc_control_until - time.time()) // 60) + 1)
            self._pc_control.set_label(f"Allow PC control ({left} min left)")
        else:
            self._pc_control.set_label("Allow PC control")
            if self._pc_control.get_active():
                self._pc_control.set_active(False)   # toggled handler records the switch-off
        widget = getattr(self, "widget", None)
        return widget is None or widget.get_root() is not None   # stop the timer once the card has been removed

    def __init__(self, device: pairing.PairedDevice, on_forget, on_control, on_disconnect, on_hotspot=None, on_mount=None) -> None:
        self.device = device
        self._on_hotspot = on_hotspot
        self.group = Adw.PreferencesGroup()
        self._status = Gtk.Label(css_classes=["status-pill"])
        self._disconnect_btn = Gtk.Button(label="Disconnect", css_classes=["flat"], valign=Gtk.Align.CENTER,
                                          visible=False, tooltip_text="Let another paired phone connect")
        self._disconnect_btn.connect("clicked", lambda _b: on_disconnect(device.fingerprint))
        forget = Gtk.Button(label="Forget", css_classes=["flat"], valign=Gtk.Align.CENTER)
        forget.connect("clicked", lambda _b: on_forget(device))
        self.connected = False
        self._link = "lan"
        self._address = ""
        self._info: dict = {}

        # Hero card: phone name, model, quick-glance chips, actions.
        hero = Gtk.Box(spacing=16, css_classes=["hero-card"])
        icon = Gtk.Image.new_from_icon_name("phone-symbolic")
        icon.set_pixel_size(48)
        icon.add_css_class("hero-icon")
        hero.append(icon)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, hexpand=True, valign=Gtk.Align.CENTER)
        top = Gtk.Box(spacing=10)
        self._name = Gtk.Label(label=device.name, xalign=0, css_classes=["title-2"])
        top.append(self._name)
        top.append(self._status)
        text.append(top)
        self._sub = Gtk.Label(label="", xalign=0, css_classes=["dim-label"])
        text.append(self._sub)
        self._chips = Gtk.Box(spacing=6, margin_top=6)
        text.append(self._chips)
        hero.append(text)
        actions = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, valign=Gtk.Align.CENTER)
        actions.append(self._disconnect_btn)
        self._pc_control = Gtk.CheckButton(label="Allow PC control", active=device.pc_control_active(),
                                           tooltip_text="Let this phone use this PC's shell, files, screen view "
                                                        "and keyboard/mouse. Off by default and switches itself off after "
                                                        "an hour (tick again to extend).")
        self._pc_control.connect("toggled", self._on_pc_control_toggled)
        actions.append(self._pc_control)
        self._update_pc_control_label()
        GLib.timeout_add_seconds(15, self._update_pc_control_label)
        actions.append(forget)
        hero.append(actions)

        self.widget = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.widget.append(hero)
        self.widget.append(self.group)

        # Hotspot: a local-only hotspot for PC<->phone traffic without a router, or a nudge to
        # open the phone's own hotspot screen (apps can't switch the real hotspot on).
        hs_row = Adw.ActionRow(title="Hotspot", subtitle="Phone-hosted Wi-Fi for the PC")
        hs_row.add_prefix(Gtk.Image.new_from_icon_name("network-wireless-hotspot-symbolic"))
        for label, action, tip in (("Start local", "start_local", "Private Wi-Fi (no internet) the PC can join"),
                                   ("Stop", "stop_local", "Stop the local hotspot"),
                                   ("Open settings", "open_settings", "Notify the phone to open its hotspot screen")):
            b = Gtk.Button(label=label, valign=Gtk.Align.CENTER, css_classes=["flat"], tooltip_text=tip)
            b.connect("clicked", lambda _b, a=action: self._on_hotspot and self._on_hotspot(a))
            hs_row.add_suffix(b)
        self.group.add(hs_row)
        self._hotspot_row = hs_row

        # Phone storage as a read-only folder in the file manager (FUSE).
        fm_row = Adw.ActionRow(title="Phone files in your file manager",
                               subtitle="Browse the phone's storage like a folder (opened files are downloaded on demand)")
        fm_row.add_prefix(Gtk.Image.new_from_icon_name("folder-remote-symbolic"))
        for label, action in (("Mount", "mount"), ("Unmount", "unmount")):
            b = Gtk.Button(label=label, valign=Gtk.Align.CENTER, css_classes=["flat"])
            b.connect("clicked", lambda _b, a=action: on_mount and on_mount(device.fingerprint, a))
            fm_row.add_suffix(b)
        self.group.add(fm_row)

        self._rows: dict[str, Adw.ActionRow] = {}
        self._icons: dict[str, Gtk.Image] = {}
        self._bars: dict[str, Gtk.ProgressBar] = {}
        for key, title, icon, with_bar in (
            ("battery", "Battery", "battery-missing-symbolic", True),
            ("signal", "Mobile signal", "network-cellular-offline-symbolic", True),
            ("wifi", "Wi-Fi", "network-wireless-offline-symbolic", False),
            ("storage", "Storage", "drive-harddisk-symbolic", True),
            ("ram", "Memory", "memory-symbolic", True),
            ("system", "System", "phone-symbolic", False),
            ("link", "Connection", "network-transmit-receive-symbolic", False),
        ):
            row = Adw.ActionRow(title=title, subtitle="—")
            img = Gtk.Image.new_from_icon_name(icon)
            row.add_prefix(img)
            if with_bar:
                bar = Gtk.ProgressBar(valign=Gtk.Align.CENTER, width_request=140)
                row.add_suffix(bar)
                self._bars[key] = bar
            self.group.add(row)
            self._rows[key] = row
            self._icons[key] = img
        self.set_connected(False)

    def set_active(self, active: bool) -> None:
        pass  # only one phone can be connected, so it is always the one being controlled

    def link_label(self) -> str:
        """How the phone is connected right now: USB, Wi-Fi, mobile data or the phone's own hotspot. A direct connection
        is always on the local network; one through the relay may be on any network, so the phone's own report decides."""
        info, link = self._info, self._link
        if link == "usb":
            return "USB cable"
        on_wifi = bool(info.get("on_wifi") or info.get("wifi_ssid"))
        if link == "relay":
            if on_wifi:
                return "Wi-Fi (remote, via relay)"
            if info.get("network_type") or info.get("signal_level") is not None:
                return f"Mobile data{' ' + info['network_type'] if info.get('network_type') else ''} (remote, via relay)"
            return "Internet (remote, via relay)"
        if info.get("hotspot_on") and not on_wifi:
            return "Phone hotspot"
        return "Wi-Fi"

    def _refresh_link(self) -> None:
        if not self.connected:
            return
        label = self.link_label()
        self._status.set_label(f"● Connected · {label}")
        where = f", {self._address}" if self._address and self._link != "relay" else ""
        self._rows["link"].set_subtitle(f"{label}{where} · encrypted")

    def set_connected(self, connected: bool, address: str | None = None, link: str = "lan") -> None:
        self.connected = connected
        self._link = link
        self._address = address or ""
        self._disconnect_btn.set_visible(connected)
        self._status.set_label("● Connected" if connected else "○ Offline")
        self._status.set_css_classes(["status-pill", "online" if connected else "offline"])
        if connected:
            self._refresh_link()
        else:
            seen = self.device.last_lan_ip or "never connected"
            self._rows["link"].set_subtitle(f"Not connected (last address {seen}) — open PhoneBridge on the phone")

    def update(self, info: dict) -> None:
        self._info = info
        self._refresh_link()
        if info.get("name"):
            self._name.set_label(info.get("device_name") or info["name"])
        self._sub.set_label(" · ".join(x for x in (info.get("model"), info.get("os"), info.get("carrier")) if x))
        while (child := self._chips.get_first_child()) is not None:
            self._chips.remove(child)
        chips = []
        if info.get("battery_pct") is not None:
            chips.append((f"🔋 {info['battery_pct']}%{' ⚡' if info.get('charging') else ''}", "chip"))
        if info.get("signal_level") is not None:
            chips.append((f"📶 {info.get('network_type') or ''} {info['signal_level']}/4".replace("  ", " "), "chip"))
        if info.get("wifi_ssid"):
            chips.append((f"Wi-Fi {info['wifi_ssid']}", "chip"))
        if info.get("hotspot_on"):
            chips.append(("Hotspot on", "chip"))
        for label, css in chips:
            self._chips.append(Gtk.Label(label=label, css_classes=[css]))

        pct = info.get("battery_pct")
        charging = bool(info.get("charging"))
        bits = [f"{pct}%" if pct is not None else "—"]
        if charging:
            bits.append("charging")
        if info.get("battery_temp_c"):
            bits.append(f"{info['battery_temp_c']:.0f} °C")
        if info.get("battery_health") and info["battery_health"] != "Good":
            bits.append(info["battery_health"])
        self._rows["battery"].set_subtitle(" · ".join(bits))
        self._icons["battery"].set_from_icon_name(_battery_icon(pct, charging))
        self._bars["battery"].set_fraction((pct or 0) / 100)

        level = info.get("signal_level")
        carrier = info.get("carrier") or "No carrier"
        net = info.get("network_type") or ""
        sig_bits = [carrier]
        if net and net != "None":
            sig_bits.append(net)
        if level is not None:
            sig_bits.append(f"{level}/4 bars")
        if info.get("signal_dbm") not in (None, 2147483647):
            sig_bits.append(f"{info['signal_dbm']} dBm")
        if info.get("sim_state") == "No SIM":
            sig_bits = ["No SIM"]
        self._rows["signal"].set_subtitle(" · ".join(sig_bits))
        self._icons["signal"].set_from_icon_name(_signal_icon(level))
        self._bars["signal"].set_fraction((info.get("signal_pct") or 0) / 100)

        if info.get("hotspot_on"):
            self._rows["wifi"].set_subtitle("Sharing this phone's hotspot" +
                                            (" · PC connected through it" if not info.get("on_wifi") else ""))
            self._icons["wifi"].set_from_icon_name("network-wireless-hotspot-symbolic")
        elif info.get("on_wifi") or info.get("wifi_ssid"):
            ssid = info.get("wifi_ssid") or "Connected (allow Location on the phone to show the network name)"
            w_bits = [ssid]
            if info.get("wifi_signal_pct") is not None:
                w_bits.append(f"{info['wifi_signal_pct']}% signal")
            if info.get("wifi_link_mbps"):
                w_bits.append(f"{info['wifi_link_mbps']} Mbps")
            self._rows["wifi"].set_subtitle(" · ".join(w_bits))
            self._icons["wifi"].set_from_icon_name(_wifi_icon(info.get("wifi_signal_pct")))
        else:
            self._rows["wifi"].set_subtitle("Not on Wi-Fi")
            self._icons["wifi"].set_from_icon_name("network-wireless-offline-symbolic")

        total, free = info.get("storage_total"), info.get("storage_free")
        if total:
            used = total - (free or 0)
            self._rows["storage"].set_subtitle(f"{human_bytes(used)} used of {human_bytes(total)} · {human_bytes(free)} free")
            self._bars["storage"].set_fraction(used / total)
        rt, rf = info.get("ram_total"), info.get("ram_free")
        if rt:
            self._rows["ram"].set_subtitle(f"{human_bytes(rt - (rf or 0))} used of {human_bytes(rt)}")
            self._bars["ram"].set_fraction((rt - (rf or 0)) / rt)

        model = info.get("model") or self.device.name
        os_name = info.get("os") or ""
        self._rows["system"].set_subtitle(f"{model} · {os_name} · updated {time.strftime('%H:%M:%S')}")


class DevicePage(Gtk.ScrolledWindow):
    def __init__(self, on_pair, on_forget, on_control, on_disconnect, on_hotspot=None, on_mount=None,
                 on_relay=None) -> None:
        super().__init__(vexpand=True)
        self._on_hotspot = on_hotspot
        self._on_mount = on_mount
        self._on_disconnect = on_disconnect
        self._on_forget = on_forget
        self._on_control = on_control
        self._active_fp: str | None = None
        clamp = Adw.Clamp(maximum_size=720, margin_top=24, margin_bottom=24, margin_start=16, margin_end=16)
        self._box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24)
        clamp.set_child(self._box)
        self.set_child(clamp)

        self._empty = Adw.StatusPage(
            title="No phone paired yet",
            description="Install PhoneBridge on your Android phone, then pair it here.",
            icon_name="phone-symbolic",
        )
        self._box.append(self._empty)

        self._box.append(Gtk.Label(
            label="One phone at a time — while a phone is connected, others wait. "
                  "Click Disconnect to let another paired phone connect.",
            css_classes=["caption", "dim-label"]))
        pair_button = Gtk.Button(label="Pair a device", halign=Gtk.Align.CENTER, css_classes=["suggested-action", "pill"])
        pair_button.connect("clicked", lambda _b: on_pair())
        self._box.append(pair_button)

        if on_relay is not None:
            from .. import relay_client

            group = Adw.PreferencesGroup(
                title="Use away from home (optional)",
                description="Run your own relay server (python3 -m phonebridge.relay_server) on a machine both "
                            "devices can reach, and enter its address. The phone then connects through it when it "
                            "isn't on this network. The relay only sees encrypted bytes.")
            cfg = relay_client.load_config()
            self._relay_row = Adw.EntryRow(title="Relay address, e.g. relay.example.com:28200 (empty = off)",
                                           show_apply_button=True, text=f"{cfg[0]}:{cfg[1]}" if cfg else "")
            self._relay_row.connect("apply", lambda row: on_relay(row.get_text().strip()))
            group.add(self._relay_row)
            self._box.append(group)

        self._cards: dict[str, _DeviceCard] = {}
        for device in pairing.PairedDevice.list_all():
            self.add_device(device)

    def add_device(self, device: pairing.PairedDevice) -> None:
        if device.fingerprint in self._cards:
            return
        card = _DeviceCard(device, self._on_forget, self._on_control, self._on_disconnect, self._on_hotspot, self._on_mount)
        self._cards[device.fingerprint] = card
        # newest first, above the Pair button
        self._box.insert_child_after(card.widget, self._empty)
        self._empty.set_visible(False)

    def sync_pc_control(self, devices: list[pairing.PairedDevice]) -> None:
        for device in devices:
            card = self._cards.get(device.fingerprint)
            if card is not None:
                card.sync_pc_control(device)

    def set_hotspot_text(self, fingerprint: str, text: str) -> None:
        card = self._cards.get(fingerprint)
        if card is not None:
            card._hotspot_row.set_subtitle(text)

    def sync_with_disk(self) -> None:
        """Drop cards whose pairing record is gone (e.g. replaced by a re-pair)."""
        for fingerprint in list(self._cards):
            if pairing.PairedDevice.load(fingerprint) is None:
                self.remove_device(fingerprint)

    def remove_device(self, fingerprint: str) -> None:
        card = self._cards.pop(fingerprint, None)
        if card is not None:
            self._box.remove(card.widget)
        self._empty.set_visible(not self._cards)

    def set_connected(self, fingerprint: str, connected: bool, address: str | None = None, link: str = "lan") -> None:
        card = self._cards.get(fingerprint)
        if card is None:
            device = pairing.PairedDevice.load(fingerprint)
            if device is None:
                return
            self.add_device(device)
            card = self._cards[fingerprint]
        card.set_connected(connected, address, link)
        card.set_active(fingerprint == self._active_fp)

    def set_active(self, fingerprint: str) -> None:
        self._active_fp = fingerprint
        for fp, card in self._cards.items():
            card.set_active(fp == fingerprint)

    def update_info(self, fingerprint: str, info: dict) -> None:
        card = self._cards.get(fingerprint)
        if card is not None:
            card.update(info)

    def fingerprints(self) -> list[str]:
        return list(self._cards)
