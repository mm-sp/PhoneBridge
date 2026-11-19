"""Small local-network helpers shared by the pairing and transport code."""
from __future__ import annotations

import asyncio
import logging
import socket

log = logging.getLogger("phonebridge.net")
_BACKGROUND: set = set()


def spawn(coro) -> "asyncio.Future":
    """Fire-and-forget on the running loop. The loop only keeps a weak reference to a task, so an unreferenced one can be
    garbage-collected mid-flight; this holds it until it finishes and logs an exception instead of losing it silently."""
    task = asyncio.ensure_future(coro)
    _BACKGROUND.add(task)

    def _done(t) -> None:
        _BACKGROUND.discard(t)
        if not t.cancelled() and t.exception() is not None:
            log.warning("background task failed: %r", t.exception())

    task.add_done_callback(_done)
    return task


def local_lan_ip() -> str:
    """Best-effort LAN IP: open a UDP socket toward a public address (no packets actually sent
    for a connect() on UDP) and read back the local address the kernel would have used."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def local_bt_addr() -> str:
    """Returns the first local Bluetooth adapter's address, asking BlueZ over D-Bus.

    Modern kernels don't expose an `address` attribute under /sys/class/bluetooth/hciN, so
    sysfs alone can't answer this; it's kept only as a fallback for systems without BlueZ."""
    import glob
    import os

    adapters = sorted(os.path.basename(p) for p in glob.glob("/sys/class/bluetooth/hci*"))
    if not adapters:
        raise RuntimeError("no local Bluetooth adapter found under /sys/class/bluetooth")

    try:
        from gi.repository import Gio, GLib

        bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        for name in adapters:
            try:
                reply = bus.call_sync(
                    "org.bluez", f"/org/bluez/{name}", "org.freedesktop.DBus.Properties", "Get",
                    GLib.Variant("(ss)", ("org.bluez.Adapter1", "Address")),
                    GLib.VariantType("(v)"), Gio.DBusCallFlags.NONE, 2000, None,
                )
                return reply.unpack()[0].upper()
            except GLib.Error:
                continue
    except Exception:  # noqa: BLE001 - no GI / no system bus: fall through to sysfs
        pass

    for name in adapters:
        try:
            with open(f"/sys/class/bluetooth/{name}/address") as f:
                return f.read().strip().upper()
        except OSError:
            continue
    raise RuntimeError(
        f"found {', '.join(adapters)} but couldn't read its address — is the bluetooth service running?"
    )
