"""Screen mirroring: renders the phone's H.264 stream (protocol/PROTOCOL.md section 1b)
and sends mouse/keyboard input back as mirror.input.

Mouse: click = tap, drag = swipe, press-and-hold = long press, scroll wheel = swipe.
Keyboard (while the video has focus): typed text goes into the phone's focused text
field; Backspace/Enter work; Esc = Back.
"""
from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
from gi.repository import Adw, Gdk, GLib, Gtk  # noqa: E402

from .keyboard_page import LiveTypeBar
from .video_view import H264Renderer


class MirrorPage(Gtk.Box):
    def __init__(self, on_start, on_stop, on_input, on_send_clipboard, on_copy_to_pc, pc_clipboard,
                 transport_of=None) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_top=12,
                         margin_bottom=12, margin_start=12, margin_end=12)
        self._on_start = on_start
        self._transport_of = transport_of or (lambda: None)
        self._on_stop = on_stop
        self._on_input = on_input
        self._renderer: H264Renderer | None = None
        self._mirroring = False
        self._app: str | None = None
        self._app_label = ""
        self._drag_start: tuple[float, float] | None = None
        self._long_pressed = False

        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._toggle_btn = Gtk.Button(label="Start mirroring", css_classes=["suggested-action"])
        self._toggle_btn.connect("clicked", self._on_toggle_clicked)
        toolbar.append(self._toggle_btn)
        toolbar.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        for icon, tip, key in (
            ("go-previous-symbolic", "Back (Esc)", "back"),
            ("go-home-symbolic", "Home", "home"),
            ("view-app-grid-symbolic", "Recent apps", "recents"),
            ("preferences-system-notifications-symbolic", "Notification shade", "notifications"),
            ("emblem-system-symbolic", "Quick settings", "quick_settings"),
            ("camera-photo-symbolic", "Screenshot (saved on phone)", "screenshot"),
            ("edit-copy-symbolic", "Copy the text selected on the phone to the PC clipboard "
                                   "(hold or right-click on text first to select it)", "copy"),
        ):
            btn = Gtk.Button(icon_name=icon, tooltip_text=tip)
            btn.connect("clicked", lambda _b, k=key: self._key(k))
            toolbar.append(btn)

        # Lock / Unlock toggle — label follows the phone's real screen state (screen.state).
        self._screen_on = True
        self._lock_btn = Gtk.Button()
        self._lock_btn.connect("clicked", self._on_lock_clicked)
        toolbar.append(self._lock_btn)
        self._update_lock_button()

        toolbar.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        volume = Gtk.Box(css_classes=["linked"])
        for label, tip, key in (("−", "Volume down (Ctrl+Page Down)", "volume_down"),
                                ("+", "Volume up (Ctrl+Page Up)", "volume_up")):
            btn = Gtk.Button(label=label, tooltip_text=tip, css_classes=["volume-btn"])
            btn.connect("clicked", lambda _b, k=key: self._key(k))
            volume.append(btn)
        toolbar.append(volume)
        self._mute_btn = Gtk.Button(tooltip_text="Mute / unmute media")
        self._mute_btn.set_child(Adw.ButtonContent(icon_name="audio-volume-muted-symbolic", label="Mute"))
        self._mute_btn.connect("clicked", lambda _b: self._key("volume_mute"))
        toolbar.append(self._mute_btn)
        self._volume_label = Gtk.Label(css_classes=["caption", "dim-label"], width_chars=4)
        toolbar.append(self._volume_label)
        self._status = Gtk.Label(xalign=1, hexpand=True, css_classes=["dim-label"], ellipsize=3)
        toolbar.append(self._status)
        self.append(toolbar)

        self._picture = Gtk.Picture(vexpand=True, hexpand=True, focusable=True, can_shrink=True)
        self._picture.set_content_fit(Gtk.ContentFit.CONTAIN)
        self._placeholder = Adw.StatusPage(
            title="Screen mirror", icon_name="video-display-symbolic",
            description="Click Start mirroring, then tap \"Start now\" on the phone the first time.\n"
                        "To control the phone with your mouse and keyboard, turn on "
                        "\"PhoneBridge remote control\" in the phone's Accessibility settings.",
        )
        self._view = Gtk.Stack(vexpand=True)
        self._view.add_named(self._placeholder, "idle")
        frame = Gtk.Frame(css_classes=["mirror-frame"])
        frame.set_child(self._picture)
        self._view.add_named(frame, "video")
        self.append(self._view)

        # Live keyboard: every key goes straight into the phone's selected text box.
        self._typer = LiveTypeBar(on_input)
        self.append(self._typer)

        # Cross-device clipboard (it also syncs automatically in the background).
        clip = Gtk.Box(spacing=8, css_classes=["clip-strip"])
        clip.append(Gtk.Image.new_from_icon_name("edit-paste-symbolic"))
        self._clip_label = Gtk.Label(xalign=0, hexpand=True, ellipsize=3, css_classes=["dim-label"],
                                     label="Clipboard: nothing copied yet")
        clip.append(self._clip_label)
        paste = Gtk.Button(label="Paste on phone", tooltip_text="Type the PC's clipboard into the phone's selected box")
        paste.connect("clicked", lambda _b: self._paste_on_phone())
        clip.append(paste)
        to_phone = Gtk.Button(label="Send to phone clipboard")
        to_phone.connect("clicked", lambda _b: self._clip_text and on_send_clipboard(self._clip_text))
        clip.append(to_phone)
        to_pc = Gtk.Button(label="Copy to PC")
        to_pc.connect("clicked", lambda _b: self._clip_text and on_copy_to_pc(self._clip_text))
        clip.append(to_pc)
        self.append(clip)
        self._clip_text = ""
        self._pc_clipboard = pc_clipboard

        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._on_drag_begin)
        drag.connect("drag-end", self._on_drag_end)
        self._picture.add_controller(drag)
        # Right-click = hold on the phone (selects a word and opens its Copy/Paste bar).
        right = Gtk.GestureClick(button=Gdk.BUTTON_SECONDARY)
        right.connect("pressed", lambda _g, _n, x, y: self._on_long_press(None, x, y))
        self._picture.add_controller(right)
        long_press = Gtk.GestureLongPress()
        long_press.connect("pressed", self._on_long_press)
        self._picture.add_controller(long_press)
        scroll = Gtk.EventControllerScroll.new(Gtk.EventControllerScrollFlags.VERTICAL | Gtk.EventControllerScrollFlags.DISCRETE)
        scroll.connect("scroll", self._on_scroll)
        self._picture.add_controller(scroll)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key)
        self._picture.add_controller(keys)
        self._pointer = (0.5, 0.5)
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", lambda _c, x, y: setattr(self, "_pointer", (x, y)))
        self._picture.add_controller(motion)

    # ---------------------------------------------------------------- state

    def is_mirroring(self) -> bool:
        return self._mirroring

    @property
    def app(self) -> str | None:
        """Package being streamed on its own (per-app streaming), or None for the whole screen."""
        return self._app

    def start_app(self, pkg: str, label: str) -> None:
        """Stream just this app (PROTOCOL.md section 4f) instead of the whole screen."""
        if self._mirroring:
            self._on_stop()
            self._stop_rendering()
        self._app, self._app_label = pkg, label
        self._on_toggle_clicked(None)
        if not self._mirroring:
            self._app = None

    def on_screen_state(self, payload: dict) -> None:
        self._screen_on = bool(payload.get("on", True)) and not payload.get("locked", False)
        self._update_lock_button()
        if not payload.get("on", True):
            self.set_status("Phone screen is off — click Unlock to wake it")
        elif payload.get("locked") and payload.get("secure"):
            self.set_status("Phone is locked — click Unlock, then enter your PIN/pattern here")

    def on_volume_state(self, payload: dict) -> None:
        muted = payload.get("muted") or payload.get("level") == 0
        self._volume_label.set_label("muted" if muted else f"{payload.get('pct', 0)}%")
        self._mute_btn.set_child(Adw.ButtonContent(
            icon_name="audio-volume-high-symbolic" if muted else "audio-volume-muted-symbolic",
            label="Unmute" if muted else "Mute"))

    def _update_lock_button(self) -> None:
        if self._screen_on:
            content = Adw.ButtonContent(icon_name="system-lock-screen-symbolic", label="Lock")
            self._lock_btn.set_tooltip_text("Lock the phone and turn its screen off")
        else:
            content = Adw.ButtonContent(icon_name="changes-allow-symbolic", label="Unlock")
            self._lock_btn.set_tooltip_text("Wake the phone and open its lock screen")
        self._lock_btn.set_child(content)

    def _on_lock_clicked(self, _button) -> None:
        if self._screen_on:
            self._key("lock")
            self._screen_on = False
        else:
            self._key("unlock")
            self._screen_on = True
        self._update_lock_button()

    def push_frame(self, data: bytes) -> None:
        """Called from the net thread (transport video sink)."""
        renderer = self._renderer
        if renderer is not None:
            renderer.push(data)

    def set_status(self, message: str) -> None:
        self._status.set_label(message)

    def on_video_started(self, payload: dict) -> None:
        note = "" if payload.get("input_available") else " · enable Accessibility on phone to control it"
        what = f"Streaming {self._app_label or payload['app']}" if payload.get("app") else "Mirroring"
        self.set_status(f"{what} {payload.get('width')}×{payload.get('height')}{note}")

    def on_disconnected(self) -> None:
        if self._mirroring:
            self._stop_rendering()
            self.set_status("Phone disconnected")

    def _start_rendering(self) -> None:
        try:
            self._renderer = H264Renderer(self._picture, on_first_frame=self._on_first_frame)
        except Exception as exc:  # noqa: BLE001 - missing decoder etc.
            self.set_status(str(exc))
            return
        self._renderer.on_error = lambda msg: GLib.idle_add(self.set_status, f"Video error: {msg}")
        self._mirroring = True
        self._toggle_btn.set_label("Stop mirroring")
        self._toggle_btn.set_css_classes(["destructive-action"])
        self.set_status("Waiting for the phone…")

    def _stop_rendering(self) -> None:
        if self._renderer is not None:
            self._renderer.stop()
            self._renderer = None
        self._picture.set_paintable(None)
        self._view.set_visible_child_name("idle")
        self._mirroring = False
        self._app, self._app_label = None, ""
        self._toggle_btn.set_label("Start mirroring")
        self._toggle_btn.set_css_classes(["suggested-action"])

    def _on_first_frame(self) -> None:
        transport = self._transport_of()
        if transport:
            self.set_status(f"{self._status.get_label()} · {transport.upper()}")
        self._view.set_visible_child_name("video")
        self._picture.grab_focus()

    def _on_toggle_clicked(self, _button: Gtk.Button) -> None:
        if self._mirroring:
            self._on_stop()
            self._stop_rendering()
            self.set_status("")
        else:
            self._start_rendering()
            if self._mirroring and not self._on_start():
                self._stop_rendering()
                self.set_status("No phone connected")

    def stop_if_running(self) -> None:
        if self._mirroring:
            self._on_stop()
            self._stop_rendering()

    # ---------------------------------------------------------------- input

    def _to_phone(self, x: float, y: float) -> tuple[float, float] | None:
        """Widget coords -> normalized [0,1] phone coords, accounting for letterboxing."""
        fw, fh = self._renderer.frame_size if self._renderer else (0, 0)
        ww, wh = self._picture.get_width(), self._picture.get_height()
        if not (fw and fh and ww and wh):
            return None
        scale = min(ww / fw, wh / fh)
        dw, dh = fw * scale, fh * scale
        ox, oy = (ww - dw) / 2, (wh - dh) / 2
        nx, ny = (x - ox) / dw, (y - oy) / dh
        if not (0 <= nx <= 1 and 0 <= ny <= 1):
            return None
        return nx, ny

    def _key(self, key: str) -> None:
        self._on_input({"kind": "key", "key": key})

    # ---------------------------------------------------------------- clipboard

    def set_clipboard(self, text: str, source: str) -> None:
        """Latest clipboard text from either device ("phone" or "pc")."""
        self._clip_text = text
        preview = " ".join(text.split())
        self._clip_label.set_label(f"Clipboard ({'from phone' if source == 'phone' else 'from PC'}): {preview}")
        self._clip_label.remove_css_class("dim-label")

    def _paste_on_phone(self) -> None:
        text = self._pc_clipboard() or self._clip_text
        if text:
            self._typer.type_into_phone(text)
        else:
            self.set_status("The clipboard is empty")

    def _on_drag_begin(self, _gesture, x: float, y: float) -> None:
        self._drag_start = (x, y)
        self._long_pressed = False
        self._picture.grab_focus()

    def _on_drag_end(self, _gesture, offset_x: float, offset_y: float) -> None:
        if self._drag_start is None or self._long_pressed:
            self._drag_start = None
            return
        x1, y1 = self._drag_start
        self._drag_start = None
        start = self._to_phone(x1, y1)
        if start is None:
            return
        if abs(offset_x) < 6 and abs(offset_y) < 6:
            self._on_input({"kind": "tap", "x": start[0], "y": start[1]})
            return
        end = self._to_phone(x1 + offset_x, y1 + offset_y) or start
        self._on_input({"kind": "swipe", "x1": start[0], "y1": start[1], "x2": end[0], "y2": end[1],
                        "duration_ms": 250})

    def _on_long_press(self, _gesture, x: float, y: float) -> None:
        pos = self._to_phone(x, y)
        if pos is not None:
            self._long_pressed = True
            self._on_input({"kind": "long_press", "x": pos[0], "y": pos[1]})

    def _on_scroll(self, _controller, _dx: float, dy: float) -> bool:
        pos = self._to_phone(*self._pointer)
        if pos is None:
            return False
        # Wheel down = content moves up = finger swipes upward.
        dist = 0.25 if dy > 0 else -0.25
        y2 = min(0.95, max(0.05, pos[1] - dist))
        self._on_input({"kind": "swipe", "x1": pos[0], "y1": pos[1], "x2": pos[0], "y2": y2, "duration_ms": 150})
        return True

    def _on_key(self, _controller, keyval: int, _keycode: int, state: Gdk.ModifierType) -> bool:
        if not self._mirroring:
            return False
        if keyval == Gdk.KEY_Escape:
            self._key("back")
        elif keyval in (Gdk.KEY_AudioRaiseVolume, Gdk.KEY_Page_Up) and state & Gdk.ModifierType.CONTROL_MASK:
            self._key("volume_up")
        elif keyval in (Gdk.KEY_AudioLowerVolume, Gdk.KEY_Page_Down) and state & Gdk.ModifierType.CONTROL_MASK:
            self._key("volume_down")
        elif keyval == Gdk.KEY_BackSpace:
            self._key("backspace")
        elif keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            self._key("enter")
        elif state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK):
            return False
        else:
            ch = chr(Gdk.keyval_to_unicode(keyval)) if Gdk.keyval_to_unicode(keyval) else ""
            if not ch:
                return False
            self._on_input({"kind": "text", "text": ch})
        return True
