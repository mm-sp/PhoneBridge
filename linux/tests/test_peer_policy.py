#!/usr/bin/env python3
"""The PC's ports only answer LAN/USB sources, not the internet. Run: `python3 tests/test_peer_policy.py`"""
import asyncio
import os
import shutil
import ssl
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent)); sys.path.insert(0, str(HERE))
import ipaddress  # noqa: E402

from _isolate import isolate_tls  # noqa: E402
from phonebridge import pairing, peer_policy as pp, transport  # noqa: E402
from phonebridge.security import keystore, ledger as L  # noqa: E402

PASS = FAIL = 0


def check(label, cond):
    global PASS, FAIL
    PASS, FAIL = (PASS + 1, FAIL) if cond else (PASS, FAIL + 1)
    print(f"  {'ok ' if cond else 'FAIL'} {label}")


async def live(tmp):
    pairing.DEVICES_DIR = tmp / "cfg" / "devices"
    isolate_tls(tmp / "cfg")
    transport.DISCOVERY_PORT = 28186
    server = transport.ControlChannelServer(port=0)
    server.ledger = L.LocalLedger(tmp / "a.jsonl", keystore.for_config_dir(tmp / "cfg"))
    await server.start()
    asyncio.ensure_future(server.serve_forever())
    port = server._server.sockets[0].getsockname()[1]  # noqa: SLF001
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT); ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE

    async def closed_quickly():
        r, w = await asyncio.open_connection("127.0.0.1", port, ssl=ctx)
        w.write(b"somefp\n"); await w.drain()
        try:
            await asyncio.wait_for(r.read(100), 2)
            return True
        except asyncio.TimeoutError:
            return False
        finally:
            w.close()
    real = pp.is_allowed
    pp.is_allowed = lambda ip, networks=None: False       # pretend the peer is on the internet
    check("a connection from a non-LAN address is dropped before any protocol runs", await closed_quickly())
    await closed_quickly(); await closed_quickly()
    off = [r for r in server.ledger.records() if r["meta"].get("reason") == "off_network"]
    check("it is recorded once (a scanner can't fill the ledger)", len(off) == 1)
    pp.is_allowed = real
    check("loopback (the USB tunnel) is accepted", pp.is_allowed("127.0.0.1"))


def main():
    nets = [ipaddress.ip_network("192.168.7.0/24"), ipaddress.ip_network("2401:4900:4e12:d332::/64")]
    allowed = lambda ip: pp.is_allowed(ip, nets)
    for ip in ("127.0.0.1", "::1", "10.1.2.3", "172.16.0.9", "192.168.1.50", "169.254.3.4", "fe80::1%wlan0", "fd00::5", "::ffff:192.168.1.5"):
        check(f"{ip} is allowed (loopback / private / link-local)", allowed(ip))
    check("a phone's global IPv6 on the PC's own Wi-Fi prefix is allowed", allowed("2401:4900:4e12:d332:258e:ca8e:ab2c:ce0f"))
    for ip in ("8.8.8.8", "1.1.1.1", "203.0.113.9" if False else "93.184.216.34", "2606:4700:4700::1111", "2401:4900:4e12:ffff::1", "100.64.0.1", "garbage", ""):
        check(f"{ip or '(empty)'} is refused (internet / off-link / invalid)", not allowed(ip))
    os.environ["PHONEBRIDGE_ALLOW_PUBLIC"] = "1"
    check("PHONEBRIDGE_ALLOW_PUBLIC=1 switches the check off on purpose", pp.is_allowed("8.8.8.8", nets))
    del os.environ["PHONEBRIDGE_ALLOW_PUBLIC"]
    check("this machine's own networks are discovered", len(pp.local_networks()) > 0)
    tmp = Path(tempfile.mkdtemp())
    try:
        asyncio.run(live(tmp))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
