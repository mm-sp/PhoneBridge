"""BLE wake-up beacon: advertises PhoneBridge's service UUID over Bluetooth Low Energy so a
paired phone that is scanning for it knows the PC just came online and can connect straight
away, instead of waiting out its retry back-off. It carries no identity or data — only the
public service UUID; everything that matters is still authenticated over TLS + the session key.

Uses BlueZ's LEAdvertisingManager1 over D-Bus. Must be started from the GLib main thread
(the GTK app's), because BlueZ calls back into the exported advertisement object.
Best effort: no adapter / no BLE advertising support -> silently does nothing.
"""
from __future__ import annotations

import logging

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

from .pairing import SERVICE_UUID

log = logging.getLogger("phonebridge.ble_wake")

ADV_PATH = "/dev/phonebridge/advertisement0"
_XML = """
<node>
  <interface name="org.bluez.LEAdvertisement1">
    <method name="Release"/>
    <property name="Type" type="s" access="read"/>
    <property name="ServiceUUIDs" type="as" access="read"/>
    <property name="LocalName" type="s" access="read"/>
  </interface>
</node>
"""

_state: dict = {}


def _props() -> dict:
    return {"Type": GLib.Variant("s", "peripheral"),
            "ServiceUUIDs": GLib.Variant("as", [SERVICE_UUID]),
            "LocalName": GLib.Variant("s", "PhoneBridge")}


def start() -> bool:
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        adapter = None
        objs = bus.call_sync("org.bluez", "/", "org.freedesktop.DBus.ObjectManager", "GetManagedObjects",
                             None, GLib.VariantType("(a{oa{sa{sv}}})"), Gio.DBusCallFlags.NONE, 3000, None).unpack()[0]
        for path, ifaces in objs.items():
            if "org.bluez.LEAdvertisingManager1" in ifaces:
                adapter = path
                break
        if adapter is None:
            log.info("no BLE advertising support on this PC's Bluetooth adapter")
            return False
        node = Gio.DBusNodeInfo.new_for_xml(_XML)

        def on_method(_c, _sender, _path, _iface, method, _params, invocation):
            if method == "Release":
                _state.clear()
            invocation.return_value(None)

        def on_get(_c, _sender, _path, _iface, prop):
            return _props()[prop]

        reg_id = bus.register_object(ADV_PATH, node.interfaces[0], on_method, on_get, None)
        _state.update(bus=bus, reg_id=reg_id, adapter=adapter)

        def done(source, result, _data=None):
            try:
                source.call_finish(result)
                log.info("BLE wake beacon advertising on %s", adapter)
            except GLib.Error as exc:
                log.info("BLE advertising refused: %s", exc.message)

        bus.call("org.bluez", adapter, "org.bluez.LEAdvertisingManager1", "RegisterAdvertisement",
                 GLib.Variant("(oa{sv})", (ADV_PATH, {})), None, Gio.DBusCallFlags.NONE, 5000, None, done)
        return True
    except Exception as exc:  # noqa: BLE001 - no BlueZ / no system bus
        log.info("BLE wake beacon unavailable: %s", exc)
        return False


def stop() -> None:
    bus, adapter = _state.get("bus"), _state.get("adapter")
    if bus is None:
        return
    try:
        bus.call_sync("org.bluez", adapter, "org.bluez.LEAdvertisingManager1", "UnregisterAdvertisement",
                      GLib.Variant("(o)", (ADV_PATH,)), None, Gio.DBusCallFlags.NONE, 2000, None)
        bus.unregister_object(_state["reg_id"])
    except GLib.Error:
        pass
    _state.clear()
