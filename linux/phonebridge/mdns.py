"""Advertises the control channel as `_phonebridge._tcp` over Avahi (mDNS / DNS-SD) so the
phone can find this PC without a remembered IP. Best effort: no Avahi, no advertisement,
and the UDP broadcast discovery in transport.py keeps working either way.

The TXT record carries only the first 16 hex chars of the (public) TLS certificate
fingerprint, so a phone can skip other PhoneBridge PCs on the same network; the TLS pin
and the session key are what actually authenticate.
"""
from __future__ import annotations

import logging
import socket

log = logging.getLogger("phonebridge.mdns")

SERVICE_TYPE = "_phonebridge._tcp"

_keepalive: list = []  # the bus connection and entry group must outlive start()


def advertise(port: int, cert_fingerprint: str) -> bool:
    try:
        import gi

        gi.require_version("Gio", "2.0")
        from gi.repository import Gio, GLib

        bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        reply = bus.call_sync("org.freedesktop.Avahi", "/", "org.freedesktop.Avahi.Server", "EntryGroupNew",
                              None, GLib.VariantType("(o)"), Gio.DBusCallFlags.NONE, 2000, None)
        group_path = reply.unpack()[0]
        txt = [list(f"cert={cert_fingerprint[:16]}".encode()), list(b"v=2")]
        name = f"PhoneBridge on {socket.gethostname()}"
        bus.call_sync(
            "org.freedesktop.Avahi", group_path, "org.freedesktop.Avahi.EntryGroup", "AddService",
            GLib.Variant("(iiussssqaay)", (-1, -1, 0, name, SERVICE_TYPE, "", "", port, txt)),
            None, Gio.DBusCallFlags.NONE, 2000, None,
        )
        bus.call_sync("org.freedesktop.Avahi", group_path, "org.freedesktop.Avahi.EntryGroup", "Commit",
                      None, None, Gio.DBusCallFlags.NONE, 2000, None)
        _keepalive.extend([bus, group_path])
        log.info("advertising %s on :%d over mDNS", SERVICE_TYPE, port)
        return True
    except Exception as exc:  # noqa: BLE001 - no GI, no Avahi, no system bus
        log.info("mDNS advertisement unavailable (%s); UDP discovery still works", exc)
        return False
