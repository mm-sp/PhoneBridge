"""Who may even talk to the PC's ports. PhoneBridge listens on all interfaces (the phone's address changes with DHCP), but it
is a LAN/USB service and must not be reachable from the internet by default: a connection is accepted only from

  * loopback (the USB tunnel arrives here), link-local and private ranges (RFC 1918, ULA fc00::/7), or
  * an address on a network directly attached to one of this PC's interfaces (that includes a phone's global IPv6 address
    on the same Wi-Fi).

Everything else is dropped before any protocol runs. Set PHONEBRIDGE_ALLOW_PUBLIC=1 to disable the check (e.g. a deliberate
port-forward). Relay connections are outbound from this PC and are not subject to it.
"""
from __future__ import annotations

import ipaddress
import json
import os
import subprocess  # nosec B404 - runs the fixed command `ip -j addr`
import time

_cache: tuple[float, list] = (0.0, [])
_TTL = 30.0


def local_networks() -> list:
    """Networks directly attached to this machine's interfaces (cached for 30 s)."""
    global _cache
    now = time.monotonic()
    if now - _cache[0] < _TTL and _cache[1]:
        return _cache[1]
    nets = []
    try:
        out = subprocess.run(["ip", "-j", "addr"], capture_output=True, text=True, timeout=5).stdout  # nosec B603 B607
        for iface in json.loads(out):
            for a in iface.get("addr_info", []):
                try:
                    nets.append(ipaddress.ip_interface(f"{a['local']}/{a['prefixlen']}").network)
                except (KeyError, ValueError):
                    continue
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    _cache = (now, nets)
    return nets


def is_allowed(ip: str, networks: list | None = None) -> bool:
    if os.environ.get("PHONEBRIDGE_ALLOW_PUBLIC") == "1":
        return True
    try:
        addr = ipaddress.ip_address(ip.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    if addr.is_loopback or addr.is_link_local or addr.is_private:
        return True
    return any(addr in net for net in (networks if networks is not None else local_networks()) if net.version == addr.version)
