"""Phone-as-webcam: shows the phone's front or back camera live, and (when a
v4l2loopback device exists) also feeds it to a virtual camera so browsers and
video-call apps can pick "PhoneBridge Camera".

Set up the virtual camera once with:
    sudo apt install v4l2loopback-dkms
    sudo modprobe v4l2loopback video_nr=10 card_label="PhoneBridge Camera" exclusive_caps=1
"""
from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from .video_view import H264Renderer, loopback_devices

SETUP_CMD = ('sudo apt install v4l2loopback-dkms && sudo modprobe v4l2loopback video_nr=10 '
             'card_label="PhoneBridge Camera" exclusive_caps=1')

_ROTATIONS = ["Auto", "0°", "90°", "180°", "270°"]


class WebcamPage(Gtk.Box):
    def __init__(self, on_start, on_stop, on_switch, transport_of=None) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_top=12,
                         margin_bottom=12, margin_start=12, margin_end=12)
        self._on_start = on_start
        self._transport_of = transport_of or (lambda: None)
        self._on_stop = on_stop
        self._on_switch = on_switch
        self._renderer: H264Renderer | None = None
        self._streaming = False
        self._facing = "back"
        self._phone_rotation = 0

        toolbar = Gtk.Box(spacing=6)
        self._toggle_btn = Gtk.Button(label="Start camera", css_classes=["suggested-action"])
        self._toggle_btn.connect("clicked", self._on_toggle_clicked)
        toolbar.append(self._toggle_btn)

        facing_box = Gtk.Box(css_classes=["linked"])
        self._back_btn = Gtk.ToggleButton(label="Back camera", active=True)
        self._front_btn = Gtk.ToggleButton(label="Front camera", group=self._back_btn)
        self._back_btn.connect("toggled", self._on_facing_toggled)
        facing_box.append(self._back_btn)
        facing_box.append(self._front_btn)
        toolbar.append(facing_box)

        switch_btn = Gtk.Button(icon_name="camera-switch-symbolic", tooltip_text="Switch camera")
        switch_btn.connect("clicked", lambda _b: (self._front_btn if self._facing == "back" else self._back_btn).set_active(True))
        toolbar.append(switch_btn)

        toolbar.append(Gtk.Label(label="Rotate:", margin_start=12))
        self._rotation = Gtk.DropDown.new_from_strings(_ROTATIONS)
        self._rotation.connect("notify::selected", lambda *_a: self._apply_rotation())
        toolbar.append(self._rotation)

        self._status = Gtk.Label(xalign=1, hexpand=True, css_classes=["dim-label"], ellipsize=3)
        toolbar.append(self._status)
        self.append(toolbar)

        # virtual camera row
        vrow = Gtk.Box(spacing=8)
        self._v4l2_switch = Gtk.Switch(valign=Gtk.Align.CENTER)
        vrow.append(self._v4l2_switch)
        self._v4l2_label = Gtk.Label(xalign=0, hexpand=True, wrap=True)
        vrow.append(self._v4l2_label)
        copy_btn = Gtk.Button(label="Copy setup command", tooltip_text=SETUP_CMD)
        copy_btn.connect("clicked", lambda _b: self.get_clipboard().set(SETUP_CMD))
        self._copy_btn = copy_btn
        vrow.append(copy_btn)
        rescan = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Look for virtual camera again")
        rescan.connect("clicked", lambda _b: self._refresh_devices())
        vrow.append(rescan)
        self.append(vrow)
        self._refresh_devices()

        self._picture = Gtk.Picture(vexpand=True, hexpand=True, can_shrink=True)
        self._picture.set_content_fit(Gtk.ContentFit.CONTAIN)
        self._view = Gtk.Stack(vexpand=True)
        self._view.add_named(Adw.StatusPage(
            title="Use your phone's camera", icon_name="camera-web-symbolic",
            description="Pick the front or back camera and press Start camera.\n"
                        "The live picture shows here, and in other apps as \"PhoneBridge Camera\" once the "
                        "virtual camera is set up.",
        ), "idle")
        frame = Gtk.Frame()
        frame.set_child(self._picture)
        self._view.add_named(frame, "video")
        self.append(self._view)

    def _refresh_devices(self) -> None:
        devices = loopback_devices()
        self._device = devices[0][0] if devices else None
        if devices:
            self._v4l2_switch.set_sensitive(True)
            self._v4l2_switch.set_active(True)
            self._v4l2_label.set_label(f"Also send to virtual camera {devices[0][0]} ({devices[0][1]}) "
                                       "— pick it in Zoom/Meet/browser")
            self._copy_btn.set_visible(False)
        else:
            self._v4l2_switch.set_active(False)
            self._v4l2_switch.set_sensitive(False)
            self._v4l2_label.set_label("Virtual camera not set up — preview only. To use the phone as a webcam in "
                                       "other apps, run the setup command once in a terminal.")
            self._copy_btn.set_visible(True)

    # ---------------------------------------------------------------- state

    def push_frame(self, data: bytes) -> None:
        renderer = self._renderer
        if renderer is not None:
            renderer.push(data)

    def set_status(self, message: str) -> None:
        self._status.set_label(message)

    def on_video_started(self, payload: dict) -> None:
        self._phone_rotation = int(payload.get("rotation") or 0)
        facing = payload.get("facing", self._facing)
        self.set_status(f"{facing.title()} camera · {payload.get('width')}×{payload.get('height')}")
        self._apply_rotation()

    def on_disconnected(self) -> None:
        if self._streaming:
            self._stop_pipeline()
            self.set_status("Phone disconnected")

    def _apply_rotation(self) -> None:
        if self._renderer is None:
            return
        idx = self._rotation.get_selected()
        degrees = self._phone_rotation if idx == 0 else (idx - 1) * 90
        self._renderer.set_rotation(degrees)

    def _on_facing_toggled(self, button: Gtk.ToggleButton) -> None:
        self._facing = "back" if button.get_active() else "front"
        if self._streaming:
            self.set_status(f"Switching to {self._facing} camera…")
            self._on_switch(self._facing)

    def _on_toggle_clicked(self, _button: Gtk.Button) -> None:
        if self._streaming:
            self._on_stop()
            self._stop_pipeline()
            self.set_status("")
        else:
            self._start_pipeline()
            if self._streaming and not self._on_start(self._facing):
                self._stop_pipeline()
                self.set_status("No phone connected")

    def stop_if_running(self) -> None:
        if self._streaming:
            self._on_stop()
            self._stop_pipeline()

    def _on_first_frame(self) -> None:
        transport = self._transport_of()
        if transport:
            self.set_status(f"{self._status.get_label()} · {transport.upper()}")
        self._view.set_visible_child_name("video")

    def _start_pipeline(self) -> None:
        device = self._device if self._v4l2_switch.get_active() else None
        try:
            self._renderer = H264Renderer(self._picture, v4l2_device=device,
                                          on_first_frame=self._on_first_frame)
        except Exception as exc:  # noqa: BLE001
            self.set_status(f"Failed to start: {exc}")
            return
        self._renderer.on_error = lambda msg: GLib.idle_add(self.set_status, f"Video error: {msg}")
        self._streaming = True
        self._toggle_btn.set_label("Stop camera")
        self._toggle_btn.set_css_classes(["destructive-action"])
        self.set_status("Starting phone camera…")

    def _stop_pipeline(self) -> None:
        if self._renderer is not None:
            self._renderer.stop()
            self._renderer = None
        self._picture.set_paintable(None)
        self._view.set_visible_child_name("idle")
        self._streaming = False
        self._toggle_btn.set_label("Start camera")
        self._toggle_btn.set_css_classes(["suggested-action"])
