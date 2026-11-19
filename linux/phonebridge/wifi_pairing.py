"""Pairing over Wi-Fi: the same handshake as Bluetooth (bluetooth_transport.run_pairing_handshake),
carried over a plain TCP connection to PAIR_PORT while the "Pair a device" dialog is open.

Plain TCP is fine here because the handshake itself is the security: the PIN never crosses
the wire (each side proves it with an HMAC over the transcript), with the QR code the phone
also checks the PC's public key against the one it scanned, the PC user must allow the phone
(both screens show the same confirmation code), and the session key comes from X25519 + the PIN.

While a pairing is in progress, transport's LAN discovery responder also answers
{"phonebridge":"pair_discover"} broadcasts (see `active_pairing`) so a phone that only
has the PIN can find this PC without typing any address.
"""
from __future__ import annotations

import socket
import subprocess
import threading

from .bluetooth_transport import ConfirmFn, accept_until_cancelled, run_pairing_handshake
from .pairing import PairedDevice, PendingPairing

PAIR_PORT = 28102

# Set while the pairing dialog is open: {"name": pc_name, "port": PAIR_PORT}.
active_pairing: dict | None = None

_SKIP_IFACE_PREFIXES = ("lo", "docker", "br-", "veth", "virbr", "vmnet", "tun", "tap", "wg", "zt")


def lan_ipv4_addresses() -> list[str]:
    """IPv4 addresses of real network interfaces (Wi-Fi, Ethernet, USB tethering)."""
    try:
        out = subprocess.run(["ip", "-4", "-o", "addr", "show", "scope", "global"],
                             capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    addrs = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 4 or parts[1].startswith(_SKIP_IFACE_PREFIXES):
            continue
        addrs.append(parts[3].split("/")[0])
    return addrs


def accept_one_pairing_wifi(
    pending: PendingPairing,
    lan_port: int,
    cert_fingerprint: str,
    confirm: ConfirmFn,
    cancel: threading.Event | None = None,
    port: int = PAIR_PORT,
) -> PairedDevice:
    """Blocks until one phone completes the handshake over TCP (raises PairingError on a
    bad token, PairingCancelled when `cancel` is set)."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        server.bind(("0.0.0.0", port))  # nosec B104 - pairing runs on the LAN while the dialog is open
        server.listen(1)
        conn = accept_until_cancelled(server, cancel)
        try:
            from . import peer_policy
            from .pairing import PairingError

            peer = conn.getpeername()[0]
            if not peer_policy.is_allowed(peer):   # the pairing port is a LAN service like the control port
                raise PairingError("a pairing attempt from outside the local network was refused")
            # The address the phone actually reached us on is the right one to hand back
            # for the control channel (better than guessing among several interfaces).
            lan_ip = conn.getsockname()[0]
            if lan_ip.startswith("127."):
                # Paired over the USB tunnel (adb reverse): the phone saw us on loopback, which is useless to it over Wi-Fi.
                # Hand back a real LAN address for later (it still tries the USB tunnel first when a cable is attached).
                from . import netutil

                lan_ip = netutil.local_lan_ip() or lan_ip
            return run_pairing_handshake(conn, pending, lan_ip, lan_port, cert_fingerprint, confirm)
        finally:
            conn.close()
    finally:
        server.close()
