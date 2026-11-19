#!/usr/bin/env python3
"""iPhone pairing: the platform in the phone's hello is stored on the PC (and defaults to
android).
Run: `python3 tests/test_ios_platform.py`"""
import base64
import json
import shutil
import socket
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from phonebridge import crypto, pairing  # noqa: E402
from phonebridge import spake2  # noqa: E402
from phonebridge.bluetooth_transport import _recv_line, _send_line, run_pairing_handshake  # noqa: E402

PASS = FAIL = 0


def check(label, cond):
    global PASS, FAIL
    PASS, FAIL = (PASS + 1, FAIL) if cond else (PASS, FAIL + 1)
    print(f"  {'ok ' if cond else 'FAIL'} {label}")


def pair_as(platform):
    """Runs a full handshake with a fake phone that reports `platform` (None = field omitted)."""
    pending = pairing.PendingPairing(bt_addr=None, pc_name="PC")
    pc_sock, phone_sock = socket.socketpair()
    result = {}

    def pc():
        result["device"] = run_pairing_handshake(pc_sock, pending, "192.168.1.5", 28100, "ab" * 32, lambda n, c: True)

    t = threading.Thread(target=pc)
    t.start()
    offer = _recv_line(phone_sock)
    mine = crypto.Keypair.generate()
    ctx = "|".join(["phonebridge-pair-v4", offer["pub"], mine.public_b64(), offer["nonce"]]).encode()
    pake = spake2.Spake2("A", pending.token, ctx)
    hello = {"v": 4, "pub": mine.public_b64(), "name": "Test phone", "spake": base64.b64encode(pake.share).decode()}
    if platform:
        hello["platform"] = platform
    _send_line(phone_sock, hello)
    keys = pake.finish(base64.b64decode(_recv_line(phone_sock)["spake"]))
    _send_line(phone_sock, {"confirm": keys.mac_phone.hex()})
    _recv_line(phone_sock)  # the PC's confirmation
    _recv_line(phone_sock)  # ack
    # read the PC's lan_info frame, then answer with an identity key sealed under the new pairing key
    n = crypto.unpack_frame_len(phone_sock.recv(4))
    blob = b""
    while len(blob) < n:
        blob += phone_sock.recv(n - len(blob))
    from phonebridge import secure_session
    key = crypto.derive_session_key_v4(mine.private, crypto.public_from_b64(offer["pub"]), keys.ke)
    ident = secure_session.Identity.generate()
    phone_sock.sendall(crypto.pack_frame(crypto.SessionCipher(key).encrypt(json.dumps({"id_pub": ident.public_b64}).encode())))
    t.join(10)
    pc_sock.close()
    phone_sock.close()
    return result["device"]


def main():
    tmp = Path(tempfile.mkdtemp()) / "devices"
    pairing.DEVICES_DIR = tmp
    print("1. platform from the pairing hello")
    ios = pair_as("ios")
    check("iPhone is recorded as ios", ios.platform == "ios")
    check("and survives a reload from disk", pairing.PairedDevice.load(ios.fingerprint).platform == "ios")
    check("a hello without a platform is android (older phone apps)", pair_as(None).platform == "android")
    check("an unknown platform is not trusted", pair_as("symbian").platform == "android")
    check("iPhone hides tabs it can't back, keeps Device and Files",
          {"messages", "calls", "mirror"} <= pairing.IOS_HIDDEN_SECTIONS
          and not {"device", "files"} & pairing.IOS_HIDDEN_SECTIONS)

    shutil.rmtree(tmp.parent, ignore_errors=True)
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
