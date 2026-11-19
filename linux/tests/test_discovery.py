#!/usr/bin/env python3
"""LAN discovery: the PC answers a UDP broadcast only when it carries a valid rotating HMAC tag from
a paired phone; the old plain-fingerprint form and wrong tags get no answer (nothing leaks)."""
import asyncio
import json
import os
import shutil
import socket
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from phonebridge import crypto, mdns, pairing, transport  # noqa: E402

PASS = FAIL = 0
mdns.advertise = lambda *a, **k: False  # don't touch the system's Avahi from a unit test


def check(label, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {label}")
    else:
        FAIL += 1
        print(f" FAIL {label}")


async def ask(port, msg, wait=0.6):
    loop = asyncio.get_running_loop()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setblocking(False)
    sock.sendto(json.dumps(msg).encode(), ("127.0.0.1", port))
    try:
        data = await asyncio.wait_for(loop.sock_recv(sock, 1024), wait)
        return json.loads(data)
    except asyncio.TimeoutError:
        return None
    finally:
        sock.close()


async def main_async():
    tmp = Path(tempfile.mkdtemp())
    pairing.DEVICES_DIR = tmp / "devices"
    from phonebridge import tls_identity
    tls_identity.CONFIG_DIR, tls_identity.CERT_PATH, tls_identity.KEY_PATH = tmp, tmp / "c.pem", tmp / "k.pem"
    transport.DISCOVERY_PORT = 28198
    dev = pairing.PairedDevice("feed", "Phone", os.urandom(32))
    dev.save()
    server = transport.ControlChannelServer(port=0)
    await server.start()
    bucket = int(time.time() // crypto.DISCOVERY_BUCKET_SECONDS)
    good = await ask(28198, {"phonebridge": "discover", "tag": crypto.discovery_tag(dev.session_key, bucket)})
    check("valid tag gets the PC's port", good is not None and good.get("phonebridge") == "here")
    prev = await ask(28198, {"phonebridge": "discover", "tag": crypto.discovery_tag(dev.session_key, bucket - 1)})
    check("previous time bucket still accepted (clock skew)", prev is not None)
    old = await ask(28198, {"phonebridge": "discover", "tag": crypto.discovery_tag(dev.session_key, bucket - 50)})
    check("stale tag ignored", old is None)
    wrong = await ask(28198, {"phonebridge": "discover", "tag": crypto.discovery_tag(os.urandom(32), bucket)})
    check("tag from an unknown key ignored", wrong is None)
    legacy = await ask(28198, {"phonebridge": "discover", "fp": "feed"})
    check("old plain-fingerprint request ignored", legacy is None)
    shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main_async()))
