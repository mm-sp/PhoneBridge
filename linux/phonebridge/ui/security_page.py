"""Security dashboard: status, devices + permissions, emergency disconnect, recent security events."""
from __future__ import annotations

import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from .. import pairing, privacy, secure_session
from ..security import keystore, ledger as L, permissions as P

_EVENT_LABELS = {
    L.PAIRED: "Device paired", L.REVOKED: "Device revoked", L.AUTH_OK: "Authenticated", L.AUTH_FAIL: "Authentication FAILED",
    L.AUTH_THROTTLED: "Address locked out", L.SESSION_START: "Session started", L.SESSION_END: "Session ended",
    L.DISCONNECT_ALL: "All devices disconnected", L.PERMISSION: "Permission changed", L.FILE_TRANSFER: "File transfer",
    L.SECURITY_ERROR: "Security error", L.DENIED: "Access denied",
}
_MODE_LABELS = ["Allow", "Ask", "Deny"]
_MODES = [P.ALLOW, P.ASK, P.DENY]


def _pretty_fp(fp: str) -> str:
    return " ".join(fp[i:i + 4] for i in range(0, len(fp), 4))


class SecurityPage(Gtk.ScrolledWindow):
    def __init__(self, server, net_thread, on_toast=None, on_revoked=None) -> None:
        super().__init__(hscrollbar_policy=Gtk.PolicyType.NEVER)
        self._server, self._net, self._toast, self._on_revoked = server, net_thread, on_toast or (lambda _m: None), on_revoked
        self._box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18, margin_top=18, margin_bottom=18,
                            margin_start=18, margin_end=18)
        self.set_child(Adw.Clamp(maximum_size=760, child=self._box))
        self._building = False
        self.refresh()

    def on_shown(self) -> None:
        self.refresh()

    # ------------------------------------------------------------------ building

    def _clear(self) -> None:
        child = self._box.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self._box.remove(child)
            child = nxt

    def refresh(self) -> None:
        self._building = True
        self._clear()
        self._box.append(self._status_group())
        self._box.append(self._emergency_group())
        self._box.append(self._privacy_group())
        self._box.append(self._devices_group())
        self._box.append(self._events_group())
        self._building = False

    def _row(self, title: str, subtitle: str = "", icon: str | None = None) -> Adw.ActionRow:
        row = Adw.ActionRow(title=GLib.markup_escape_text(title), subtitle=GLib.markup_escape_text(subtitle))
        if icon:
            row.add_prefix(Gtk.Image.new_from_icon_name(icon))
        return row

    def _status_group(self) -> Adw.PreferencesGroup:
        g = Adw.PreferencesGroup(title="Security status")
        ks = keystore.for_config_dir(keystore.DEFAULT_CONFIG_DIR)
        if ks.protected:
            g.add(self._row("Secret storage", f"Keys are held by the {ks.backend.replace('-', ' ')}", "security-high-symbolic"))
        else:
            g.add(self._row("Secret storage: NOT PROTECTED",
                            "No desktop keyring found — keys are in a private file only. Start GNOME Keyring/KDE Wallet "
                            "or set PHONEBRIDGE_PASSPHRASE.", "dialog-warning-symbolic"))
        ident = pairing.pc_identity()
        idrow = self._row("This PC's identity key (Ed25519)", _pretty_fp(secure_session.fingerprint_of(ident.public_b64)),
                          "dialog-password-symbolic")
        rot = Gtk.Button(label="Rotate", valign=Gtk.Align.CENTER,
                         tooltip_text="Create a new identity key; paired phones are told automatically")
        rot.connect("clicked", self._on_rotate_identity)
        idrow.add_suffix(rot)
        g.add(idrow)
        usb = getattr(self._server, "usb_serials", [])
        from .. import usb_link
        if usb:
            g.add(self._row("USB link active", f"{len(usb)} phone(s) can connect through the cable (adb reverse; nothing exposed on a network)",
                            "drive-removable-media-usb-symbolic"))
        else:
            g.add(self._row("USB link", "Optional: connect the phone with a USB cable and enable USB debugging"
                            if usb_link.adb_path() else "Optional: install adb and enable USB debugging on the phone to link over a cable",
                            "drive-removable-media-usb-symbolic"))
        g.add(self._row("Encryption", "TLS 1.3 (pinned certificate) + AES-256-GCM per frame, direction-bound, replay-checked",
                        "channel-secure-symbolic"))
        led = self._server.ledger
        if led is None:
            g.add(self._row("Audit ledger", "Not running", "dialog-warning-symbolic"))
        else:
            res = led.verify()
            if res.ok:
                g.add(self._row("Audit ledger verified", f"{res.records} signed, hash-chained records, no tampering found",
                                "emblem-ok-symbolic"))
            else:
                g.add(self._row("AUDIT LEDGER FAILED VERIFICATION",
                                f"{res.error} (record {res.bad_index}). The log may have been tampered with.",
                                "dialog-error-symbolic"))
            from ..security import anchor

            if isinstance(led, anchor.PrivateBlockchain):
                note = ("Anchored on your private chain" + (f" — {led.pending} anchor(s) waiting (service unreachable)" if led.pending else ""))
                g.add(self._row("Private blockchain anchoring", note + ". Only hashes are stored there.", "network-transmit-receive-symbolic"))
            else:
                g.add(self._row("Private blockchain anchoring", "Off (optional): configure anchor.json to anchor the log on a machine you run",
                                "network-offline-symbolic"))
        return g

    def _emergency_group(self) -> Adw.PreferencesGroup:
        g = Adw.PreferencesGroup(title="Emergency")
        active = len(self._server.connected_fingerprints())
        if self._server.locked_down:
            row = self._row("Connections are blocked", "No phone can connect until you resume.", "dialog-warning-symbolic")
            btn = Gtk.Button(label="Resume connections", valign=Gtk.Align.CENTER)
            btn.connect("clicked", self._on_resume)
        else:
            row = self._row("DISCONNECT ALL DEVICES",
                            f"{active} active session(s). Ends every session now and blocks new ones until you resume. "
                            "Pairings are kept — use Revoke to remove a phone.", "process-stop-symbolic")
            btn = Gtk.Button(label="Disconnect all", css_classes=["destructive-action"], valign=Gtk.Align.CENTER)
            btn.connect("clicked", self._on_disconnect_all)
        row.add_suffix(btn)
        g.add(row)
        return g

    _CLIP_EXPIRY = [("Never", 0), ("30 seconds", 30), ("2 minutes", 120), ("10 minutes", 600)]
    _HISTORY = [("Keep forever", 0), ("1 hour", 1), ("24 hours", 24), ("7 days", 168)]

    def _privacy_group(self) -> Adw.PreferencesGroup:
        g = Adw.PreferencesGroup(title="Privacy",
                                 description="Clipboard text from a password manager is never forwarded or stored. "
                                             "Messages are not saved on this PC's disk.")
        for title, subtitle, options, key in (
            ("Clear text received from the phone", "Removes synced text from the clipboard after this long", self._CLIP_EXPIRY, "clipboard_expiry_s"),
            ("Forget clipboard history after", "Older history entries are deleted", self._HISTORY, "clipboard_history_hours"),
        ):
            combo = Adw.ComboRow(title=title, subtitle=subtitle, model=Gtk.StringList.new([o[0] for o in options]))
            current = privacy.get(key)
            combo.set_selected(next((i for i, o in enumerate(options) if o[1] == current), 0))
            combo.connect("notify::selected", self._on_privacy_changed, key, options)
            g.add(combo)
        return g

    def _on_privacy_changed(self, combo: Adw.ComboRow, _p, key: str, options) -> None:
        if self._building:
            return
        privacy.set_value(key, options[combo.get_selected()][1])
        if self._server.ledger is not None:
            self._server.audit("privacy.changed", None, setting=key, value=options[combo.get_selected()][1])

    def _devices_group(self) -> Adw.PreferencesGroup:
        g = Adw.PreferencesGroup(title="Trusted devices and permissions",
                                 description="Pairing a phone does not grant it access. Each feature is set per phone: "
                                             "Allow, Ask (confirm once per connection) or Deny.")
        devices = pairing.PairedDevice.list_all()
        if not devices:
            g.add(self._row("No paired devices"))
        connected = set(self._server.connected_fingerprints())
        for dev in devices:
            exp = Adw.ExpanderRow(title=GLib.markup_escape_text(dev.name),
                                  subtitle=("Connected — " if dev.fingerprint in connected else "Offline — ")
                                  + _pretty_fp(dev.fingerprint))
            exp.add_prefix(Gtk.Image.new_from_icon_name("phone-symbolic"))
            conn = self._server.get_connection(dev.fingerprint)
            if dev.identity_pub:
                exp.add_row(self._row("Device ID (identity-key fingerprint)", _pretty_fp(secure_session.fingerprint_of(dev.identity_pub)),
                                      "dialog-password-symbolic"))
            if conn is not None:
                exp.add_row(self._row("Active session", f"{conn.session_id[:12]}…  ·  encrypted", "network-transmit-receive-symbolic"))
            for feature, label in P.FEATURES.items():
                combo = Adw.ComboRow(title=label, model=Gtk.StringList.new(
                    _MODE_LABELS if feature != "remote_control" else ["Allow", "Deny"]))
                mode = P.mode_of(dev, feature)
                if feature == "remote_control":
                    combo.set_selected(0 if mode == P.ALLOW else 1)
                else:
                    combo.set_selected(_MODES.index(mode))
                combo.connect("notify::selected", self._on_mode_changed, dev.fingerprint, feature)
                exp.add_row(combo)
            revoke = Gtk.Button(label="Revoke this device", css_classes=["destructive-action"], valign=Gtk.Align.CENTER)
            revoke.connect("clicked", self._on_revoke, dev)
            rrow = self._row("Revoke", "Deletes its pairing and key. It must be paired again to reconnect.", "user-trash-symbolic")
            rrow.add_suffix(revoke)
            exp.add_row(rrow)
            g.add(exp)
        return g

    def _events_group(self) -> Adw.PreferencesGroup:
        g = Adw.PreferencesGroup(title="Recent security events")
        led = self._server.ledger
        records = led.records() if led else []
        hour_ago = time.time() - 3600
        failed = sum(1 for r in records if r["type"] == L.AUTH_FAIL and r["ts"] > hour_ago)
        g.add(self._row("Failed authentication attempts (last hour)", str(failed),
                        "dialog-warning-symbolic" if failed else "emblem-ok-symbolic"))
        for r in reversed(records[-15:]):
            when = time.strftime("%H:%M:%S", time.localtime(r["ts"]))
            who = r.get("device") or "—"
            detail = ", ".join(f"{k}={v}" for k, v in (r.get("meta") or {}).items())
            g.add(self._row(_EVENT_LABELS.get(r["type"], r["type"]), f"{when}  ·  {who}  ·  {detail}".strip(" ·")))
        return g

    # ------------------------------------------------------------------ actions

    def _run(self, coro):
        return self._net.run_coro(coro)

    def _on_rotate_identity(self, _b) -> None:
        n = self._run(self._server.rotate_pc_identity()).result(timeout=10)
        self._toast(f"New identity key created; {n} connected phone(s) updated, the rest on their next connection")
        self.refresh()

    def _on_disconnect_all(self, _b) -> None:
        n = self._run(self._server.disconnect_all(True)).result(timeout=10)
        self._toast(f"Disconnected {n} device(s); connections are blocked until you resume")
        self.refresh()

    def _on_resume(self, _b) -> None:
        self._net.loop.call_soon_threadsafe(self._server.lift_lockdown)
        self._toast("Connections allowed again")
        GLib.timeout_add(300, lambda: (self.refresh(), False)[1])

    def _on_mode_changed(self, combo: Adw.ComboRow, _p, fingerprint: str, feature: str) -> None:
        if self._building:
            return
        dev = pairing.PairedDevice.load(fingerprint)
        if dev is None:
            return
        idx = combo.get_selected()
        mode = (P.ALLOW if idx == 0 else P.DENY) if feature == "remote_control" else _MODES[idx]
        P.set_mode(dev, feature, mode)

    def _on_revoke(self, _b, dev) -> None:
        dialog = Adw.MessageDialog(transient_for=self.get_root(), heading=f"Revoke {dev.name}?",
                                   body="Its pairing and key are deleted and it is disconnected now. "
                                        "To use it again you must pair it from scratch.")
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("revoke", "Revoke")
        dialog.set_response_appearance("revoke", Adw.ResponseAppearance.DESTRUCTIVE)

        def done(_d, response: str) -> None:
            if response != "revoke":
                return
            self._run(self._server.revoke(dev.fingerprint, "user")).result(timeout=10)
            if self._on_revoked:
                self._on_revoked(dev.fingerprint)
            self._toast(f"{dev.name} was revoked")
            self.refresh()

        dialog.connect("response", done)
        dialog.present()
