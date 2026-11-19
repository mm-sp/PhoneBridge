"""The "Pair a device" dialog: shows a QR code + PIN, waits for a phone to connect.

Developed without a display server available (Termux dev shell) — written against the
standard GTK4/libadwaita API but not yet visually run. Run it yourself on a real Linux
desktop with `gtk4`/`libadwaita`/`python3-gobject` installed to confirm layout/rendering.
"""
from __future__ import annotations

import tempfile
import threading
import time
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from .. import bluetooth_transport, netutil, pairing, qr, wifi_pairing


class PairingWindow(Adw.Window):
    def __init__(self, parent: Gtk.Window, on_paired, server) -> None:
        super().__init__(transient_for=parent, modal=True, default_width=360, default_height=480)
        self._on_paired = on_paired
        self._server = server
        self._pending: pairing.PendingPairing | None = None
        self._qr_tmp_path: Path | None = None
        self._cancel = threading.Event()
        self._confirm_lock = threading.Lock()

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_title_widget(Gtk.Label(label="Pair a device"))
        toolbar.add_top_bar(header)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16, margin_top=24,
                       margin_bottom=24, margin_start=24, margin_end=24)
        box.set_halign(Gtk.Align.CENTER)

        self._qr_picture = Gtk.Picture(width_request=220, height_request=220)
        box.append(self._qr_picture)

        self._pin_label = Gtk.Label()
        self._pin_label.add_css_class("title-1")
        box.append(self._pin_label)

        hint = Gtk.Label(label="Open PhoneBridge on your phone and scan this code,\nor tap \"Pair with PIN\" and enter this number.\nPhone and PC should be on the same Wi-Fi.\nYou'll be asked to allow the phone here.")
        hint.set_justify(Gtk.Justification.CENTER)
        hint.add_css_class("dim-label")
        box.append(hint)

        self._status_label = Gtk.Label(label="Waiting for your phone…")
        box.append(self._status_label)
        self._timer_label = Gtk.Label(css_classes=["dim-label"])
        box.append(self._timer_label)

        self._spinner = Gtk.Spinner(spinning=True)
        box.append(self._spinner)

        toolbar.set_content(box)
        self.set_content(toolbar)

        self._busy = False   # a phone is mid-handshake / the user is deciding: don't swap the code under them
        self._finished = False
        self.connect("close-request", self._on_close)
        GLib.idle_add(self._start_pairing)
        GLib.timeout_add_seconds(1, self._tick)

    def _tick(self) -> bool:
        """Counts down the code's one-minute life and swaps in a fresh one when it is spent."""
        if self._cancel.is_set() and self._finished:
            return False
        pending = self._pending
        if pending is None:
            return True
        if pending.is_spent() and not self._busy and not self._finished:
            self._start_pairing()   # new code, new listeners; the old ones stop via their own cancel event
        elif not pending.is_spent():
            self._timer_label.set_label(f"This code refreshes in {pending.seconds_left()} s")
        return True

    def _start_pairing(self) -> bool:
        self._cancel.set()                 # stop any listeners of the previous code …
        self._cancel = threading.Event()   # … and give the new ones their own switch
        try:
            bt_addr: str | None = netutil.local_bt_addr()
        except RuntimeError:
            bt_addr = None  # Wi-Fi pairing still works

        self._pending = pairing.PendingPairing(
            bt_addr=bt_addr, pc_name=GLib.get_host_name(),
            lan_ips=wifi_pairing.lan_ipv4_addresses(), pair_port=wifi_pairing.PAIR_PORT,
        )
        payload = self._pending.qr_payload()

        tmp_dir = Path(tempfile.mkdtemp(prefix="phonebridge-qr-"))
        self._qr_tmp_path = tmp_dir / "pair.png"
        qr.render_qr(payload, self._qr_tmp_path)
        self._qr_picture.set_filename(str(self._qr_tmp_path))
        self._pin_label.set_label(qr.pin_text(payload))
        self._timer_label.set_label(f"This code refreshes in {self._pending.seconds_left()} s")
        wifi_pairing.active_pairing = {"name": self._pending.pc_name, "port": wifi_pairing.PAIR_PORT}

        routes = ["Wi-Fi"] + (["Bluetooth"] if bt_addr else [])
        self._status_label.set_label(f"Waiting for your phone ({' or '.join(routes)})…")
        self._done = threading.Lock()
        self._finished = False
        self._busy = False
        pending, cancel = self._pending, self._cancel
        threading.Thread(target=self._listen, args=("wifi", pending, cancel), daemon=True).start()
        threading.Thread(target=self._open_usb_route, args=(cancel,), daemon=True).start()
        if bt_addr:
            threading.Thread(target=self._listen, args=("bt", pending, cancel), daemon=True).start()
        return False

    def _open_usb_route(self, cancel: threading.Event) -> None:
        """If a phone is plugged in with USB debugging on, forwards its 127.0.0.1:<pairing port> to this PC's pairing port
        (`adb reverse`), so the phone can pair over the cable only (the same SPAKE2 exchange, nothing on the network)."""
        from .. import usb_link

        if not usb_link.adb_path():
            return
        serials = usb_link.ensure_reverse(wifi_pairing.PAIR_PORT)
        if serials and not cancel.is_set():
            routes = ["USB", "Wi-Fi"] + (["Bluetooth"] if self._pending and self._pending.bt_addr else [])
            GLib.idle_add(self._status_label.set_label, f"Waiting for your phone ({' or '.join(routes)})…")

    def _listen(self, route: str, pending, cancel: threading.Event) -> None:
        """Accepts pairing attempts on one route until one succeeds (on any route), the code is replaced, or the
        dialog closes. A wrong/expired PIN is reported but the dialog keeps waiting."""
        while not cancel.is_set():
            try:
                if route == "wifi":
                    device = wifi_pairing.accept_one_pairing_wifi(
                        pending, lan_port=self._server.port,
                        cert_fingerprint=self._server.cert_fingerprint, confirm=self._confirm,
                        cancel=cancel,
                    )
                else:
                    device = bluetooth_transport.accept_one_pairing(
                        pending, lan_ip=netutil.local_lan_ip(), lan_port=self._server.port,
                        cert_fingerprint=self._server.cert_fingerprint, confirm=self._confirm,
                        cancel=cancel,
                    )
            except bluetooth_transport.PairingCancelled:
                return
            except OSError as exc:
                if route == "bt":
                    return  # Bluetooth unusable right now — Wi-Fi keeps going
                GLib.idle_add(self._on_failed, f"Wi-Fi pairing unavailable: {exc}")
                return
            except Exception as exc:  # noqa: BLE001 - bad PIN, phone hung up… keep waiting
                if pending.is_spent():
                    GLib.idle_add(self._on_failed, f"{exc}")   # the timer swaps in a fresh code
                    return
                GLib.idle_add(self._on_failed, f"{exc} — try again")
                continue
            with self._done:
                if self._finished:
                    return
                self._finished = True
            cancel.set()  # stop the other route's listener
            GLib.idle_add(self._on_success, device)
            return

    def _confirm(self, phone_name: str, code: str) -> bool:
        """Runs on a listener thread: asks the user on the GTK thread whether to allow this
        phone, and blocks until they answer (declines after 2 minutes or if the dialog closes)."""
        with self._confirm_lock:  # one question at a time if Wi-Fi and Bluetooth both get a phone
            if self._cancel.is_set():
                return False
            answered = threading.Event()
            result = {"allow": False}

            def ask() -> bool:
                dialog = Adw.MessageDialog(
                    transient_for=self, heading=f"Allow {phone_name} to pair?",
                    body=(f"Check that your phone shows the same code:\n\n{code[:3]} {code[3:]}\n\n"
                          "A paired phone can see this PC's files and screen and control it. "
                          "Only allow it if the codes match and it is your phone."),
                )
                dialog.add_response("deny", "Don't allow")
                dialog.add_response("allow", "Allow")
                dialog.set_response_appearance("allow", Adw.ResponseAppearance.SUGGESTED)
                dialog.set_default_response("deny")
                dialog.set_close_response("deny")

                def on_response(_d, response: str) -> None:
                    result["allow"] = response == "allow"
                    answered.set()

                dialog.connect("response", on_response)
                self._status_label.set_label(f"{phone_name} wants to pair — check the codes")
                dialog.present()
                return False

            self._busy = True   # the code was just consumed; keep this dialog's session until the user answers
            try:
                GLib.idle_add(ask)
                deadline = time.monotonic() + 120   # the one-minute code life covers the phone's proof, not the user's decision
                while not answered.wait(0.5):
                    if self._cancel.is_set() or self._pending is None or time.monotonic() > deadline:
                        return False
                return result["allow"]
            finally:
                self._busy = False

    def _on_success(self, device: pairing.PairedDevice) -> bool:
        self._spinner.set_visible(False)
        self._status_label.set_label(f"Paired with {device.name}!")
        wifi_pairing.active_pairing = None
        self._on_paired(device)
        GLib.timeout_add(900, lambda: (self.close(), False)[1])
        return False

    def _on_failed(self, message: str) -> bool:
        self._status_label.set_label(f"Pairing failed: {message}")
        return False

    def _on_close(self, *_args) -> bool:
        self._cancel.set()  # stop the listeners so the ports are free for the next attempt
        from .. import usb_link
        if usb_link.adb_path():
            threading.Thread(target=usb_link.remove_reverse, args=(wifi_pairing.PAIR_PORT,), daemon=True).start()
        wifi_pairing.active_pairing = None
        if self._qr_tmp_path and self._qr_tmp_path.exists():
            self._qr_tmp_path.unlink(missing_ok=True)
        return False
