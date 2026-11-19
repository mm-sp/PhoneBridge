"""Decodes the phone's H.264 stream (protocol/PROTOCOL.md section 1b) and shows it in a
Gtk.Picture, optionally also feeding a v4l2loopback device.

Frames come out of GStreamer through an appsink as raw RGBA and are wrapped in a
Gdk.MemoryTexture on the GTK thread, so this works without the gtk4paintablesink plugin
(not packaged on many distros). The decoder is whichever H.264 decoder is installed.
"""
from __future__ import annotations

import logging
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Gst", "1.0")
from gi.repository import Gdk, GLib, Gst, Gtk  # noqa: E402

log = logging.getLogger("phonebridge.video")

DECODERS = ("avdec_h264", "vah264dec", "vaapih264dec", "openh264dec", "v4l2h264dec")

# GstVideoOrientationMethod values for videoflip's video-direction property.
_ROTATION_TO_DIRECTION = {0: 0, 90: 1, 180: 2, 270: 3}


def _ensure_gst() -> None:
    if not Gst.is_initialized():
        Gst.init(None)


def available_decoder() -> str | None:
    _ensure_gst()
    for name in DECODERS:
        if Gst.ElementFactory.find(name) is not None:
            return name
    return None


def loopback_devices() -> list[tuple[str, str]]:
    """(/dev/videoN, card name) for every v4l2loopback device present."""
    found = []
    for node in sorted(Path("/sys/class/video4linux").glob("video*")):
        try:
            name = (node / "name").read_text().strip()
            driver = (node / "device" / "driver").resolve().name if (node / "device" / "driver").exists() else ""
        except OSError:
            continue
        if "loopback" in driver.lower() or "loopback" in name.lower() or "phonebridge" in name.lower():
            found.append((f"/dev/{node.name}", name))
    return found


class H264Renderer:
    """Owns one decode pipeline. push() may be called from any thread."""

    def __init__(self, picture: Gtk.Picture, v4l2_device: str | None = None, on_first_frame=None) -> None:
        _ensure_gst()
        self._picture = picture
        self._on_first_frame = on_first_frame
        self._got_frame = False
        self._pending_texture = False
        self.frame_size: tuple[int, int] = (0, 0)

        decoder = available_decoder()
        if decoder is None:
            raise RuntimeError(
                "No H.264 decoder found. Install one, e.g.: sudo apt install gstreamer1.0-libav"
            )
        loopback = ""
        if v4l2_device:
            loopback = (
                f" t. ! queue leaky=downstream max-size-buffers=2 ! videoconvert ! "
                f"video/x-raw,format=YUY2 ! v4l2sink device={v4l2_device} sync=false"
            )
        desc = (
            "appsrc name=src is-live=true format=time do-timestamp=true "
            "caps=video/x-h264,stream-format=byte-stream ! "
            f"h264parse ! {decoder} ! videoconvert ! videoflip name=flip ! tee name=t "
            "t. ! queue leaky=downstream max-size-buffers=2 ! videoconvert ! video/x-raw,format=RGBA ! "
            "appsink name=sink emit-signals=true sync=false max-buffers=1 drop=true"
            + loopback
        )
        log.info("video pipeline: %s", desc)
        self._pipeline = Gst.parse_launch(desc)
        self._src = self._pipeline.get_by_name("src")
        self._flip = self._pipeline.get_by_name("flip")
        self._pipeline.get_by_name("sink").connect("new-sample", self._on_sample)
        bus = self._pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message::error", self._on_error)
        self.on_error = None
        self._pipeline.set_state(Gst.State.PLAYING)

    def set_rotation(self, degrees: int) -> None:
        self._flip.set_property("video-direction", _ROTATION_TO_DIRECTION.get(degrees % 360, 0))

    def push(self, data: bytes) -> None:
        if self._src is not None:
            self._src.emit("push-buffer", Gst.Buffer.new_wrapped(data))

    def stop(self) -> None:
        if self._pipeline is not None:
            self._pipeline.get_bus().remove_signal_watch()
            self._pipeline.set_state(Gst.State.NULL)
        self._pipeline = None
        self._src = None

    # Runs on a GStreamer streaming thread.
    def _on_sample(self, sink) -> Gst.FlowReturn:
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.OK
        caps = sample.get_caps().get_structure(0)
        width, height = caps.get_value("width"), caps.get_value("height")
        buf = sample.get_buffer()
        ok, info = buf.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.FlowReturn.OK
        try:
            data = GLib.Bytes.new(info.data)
        finally:
            buf.unmap(info)
        stride = width * 4
        if not self._pending_texture:  # drop frames rather than queue them up on a busy UI
            self._pending_texture = True
            GLib.idle_add(self._show, data, width, height, stride)
        return Gst.FlowReturn.OK

    def _show(self, data: GLib.Bytes, width: int, height: int, stride: int) -> bool:
        self._pending_texture = False
        if self._pipeline is None:
            return False
        texture = Gdk.MemoryTexture.new(width, height, Gdk.MemoryFormat.R8G8B8A8, data, stride)
        self._picture.set_paintable(texture)
        self.frame_size = (width, height)
        if not self._got_frame:
            self._got_frame = True
            if self._on_first_frame:
                self._on_first_frame()
        return False

    def _on_error(self, _bus, message) -> None:
        err, debug = message.parse_error()
        log.warning("video pipeline error: %s (%s)", err, debug)
        if self.on_error:
            self.on_error(str(err))
