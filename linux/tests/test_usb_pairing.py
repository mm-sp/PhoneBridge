#!/usr/bin/env python3
"""Pairing over the USB cable: with `adb reverse` the phone reaches the PC's pairing port as 127.0.0.1, so the PC's real
Wi-Fi pairing listener (accept_one_pairing_wifi) serves it unchanged. Checks: the tunnel is set up for the PAIRING port, the
SPAKE2 pairing completes through a loopback connection with a fake phone, and a wrong PIN over USB is refused and counted.
Run: `python3 tests/test_usb_pairing.py`"""
import base64
import json
import os
import socket
import stat
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _isolate  # noqa: E402,F401  (keeps the real keyring and config untouched)
from phonebridge import crypto, pairing, spake2, usb_link, wifi_pairing  # noqa: E402
from phonebridge.bluetooth_transport import _recv_line, _send_line  # noqa: E402

PASS = FAIL = 0


def check(label, cond):
    global PASS, FAIL
    PASS, FAIL = (PASS + 1, FAIL) if cond else (PASS, FAIL + 1)
    print(f"  {'ok ' if cond else 'FAIL'} {label}")


def phone_over_loopback(port, pin):
    """The phone side, connecting to 127.0.0.1 exactly as the Android app does through the adb tunnel."""
    sock = socket.create_connection(("127.0.0.1", port), timeout=10)
    offer = _recv_line(sock)
    mine = crypto.Keypair.generate()
    ctx = "|".join(["phonebridge-pair-v4", offer["pub"], mine.public_b64(), offer["nonce"]]).encode()
    pake = spake2.Spake2("A", pin, ctx)
    _send_line(sock, {"v": 4, "pub": mine.public_b64(), "name": "USB phone", "spake": base64.b64encode(pake.share).decode()})
    first = _recv_line(sock)
    if first.get("ok") is False:
        return sock, first
    keys = pake.finish(base64.b64decode(first["spake"]))
    _send_line(sock, {"confirm": keys.mac_phone.hex()})
    second = _recv_line(sock)
    if second.get("ok") is False:
        return sock, second
    assert second["confirm"] == keys.mac_pc.hex()
    ack = _recv_line(sock)
    n = crypto.unpack_frame_len(sock.recv(4))
    blob = b""
    while len(blob) < n:
        blob += sock.recv(n - len(blob))
    key = crypto.derive_session_key_v4(mine.private, crypto.public_from_b64(offer["pub"]), keys.ke)
    lan = json.loads(crypto.SessionCipher(key).decrypt(blob))
    from phonebridge import secure_session
    ident = secure_session.Identity.generate()
    sock.sendall(crypto.pack_frame(crypto.SessionCipher(key).encrypt(json.dumps({"id_pub": ident.public_b64}).encode())))
    return sock, {"ack": ack, "lan": lan, "key": key}


def main():
    tmp = Path(tempfile.mkdtemp())
    pairing.DEVICES_DIR = tmp / "devices"

    print("1. the pairing port is tunnelled over USB")
    log = tmp / "calls.log"
    fake = tmp / "adb"
    fake.write_text(f"#!/bin/sh\necho \"$@\" >> {log}\ncase \"$1\" in devices) printf 'List of devices attached\\nAAA111\\tdevice\\n' ;; esac\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    check("adb reverse is set up for the pairing port", usb_link.ensure_reverse(wifi_pairing.PAIR_PORT, str(fake)) == ["AAA111"]
          and f"-s AAA111 reverse tcp:{wifi_pairing.PAIR_PORT} tcp:{wifi_pairing.PAIR_PORT}" in log.read_text())

    print("2. a phone pairs through the loopback (USB) route")
    port = 28193
    pending = pairing.PendingPairing(bt_addr=None, pc_name="PC", pair_port=port)
    result = {}

    def pc():
        try:
            result["device"] = wifi_pairing.accept_one_pairing_wifi(pending, 28100, "cd" * 32, lambda n, c: True, port=port)
        except Exception as exc:  # noqa: BLE001
            result["error"] = exc

    t = threading.Thread(target=pc)
    t.start()
    import time
    time.sleep(0.4)
    sock, out = phone_over_loopback(port, pending.token)
    t.join(15)
    sock.close()
    dev = result.get("device")
    check("pairing completes over 127.0.0.1", dev is not None and out["ack"].get("ok") is True)
    check("both sides hold the same pairing key", dev is not None and dev.session_key == out["key"])
    from phonebridge import netutil
    check("over USB the PC hands back a real LAN address, not loopback", out["lan"]["lan_ip"] == (netutil.local_lan_ip() or "127.0.0.1"))

    print("3. a wrong PIN over USB is refused and counted")
    pending2 = pairing.PendingPairing(bt_addr=None, pc_name="PC", pair_port=port)
    result.clear()
    t = threading.Thread(target=pc.__wrapped__ if hasattr(pc, "__wrapped__") else lambda: result.update(
        error=_try(lambda: wifi_pairing.accept_one_pairing_wifi(pending2, 28100, "cd" * 32, lambda n, c: True, port=port))))
    t.start()
    time.sleep(0.4)
    sock, out = phone_over_loopback(port, "000000" if pending2.token != "000000" else "111111")
    t.join(15)
    sock.close()
    check("the phone is told no", out.get("ok") is False)
    check("the attempt is counted and nothing was paired", pending2.failed_attempts == 1 and "device" not in result)
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


def _try(fn):
    try:
        fn()
    except Exception as exc:  # noqa: BLE001
        return exc


if __name__ == "__main__":
    sys.exit(main())
