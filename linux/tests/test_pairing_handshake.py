#!/usr/bin/env python3
"""Pairing handshake v2 (PIN proof by HMAC, nonce, confirmation code, allow prompt) over a real
socket pair, with a fake phone that implements the phone side exactly as the Android app does.
Pins the cross-language contract: same session key / fingerprint / confirmation code on both
ends, PIN never on the wire, wrong PINs counted and locked out, decline and expiry honoured.
Run directly: `python3 tests/test_pairing_handshake.py`
"""
import base64
import json
import shutil
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from phonebridge import bluetooth_transport as bt  # noqa: E402
from phonebridge import crypto, pairing, spake2  # noqa: E402

PASS = 0
FAIL = 0


def check(label: str, cond: bool) -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {label}")
    else:
        FAIL += 1
        print(f" FAIL {label}")


def run_handshake(pending, pin_guess, confirm=lambda name, code: True, identity="valid"):
    """Runs the PC side in a thread against a fake phone. Returns (pc_result, phone_view)."""
    pc_sock, phone_sock = socket.socketpair()
    pc_sock.settimeout(10)
    phone_sock.settimeout(10)
    out = {}

    def pc_side():
        try:
            out["device"] = bt.run_pairing_handshake(pc_sock, pending, "192.168.1.5", 28100, "ab" * 32, confirm)
        except Exception as exc:  # noqa: BLE001
            out["error"] = exc
        finally:
            pc_sock.close()

    t = threading.Thread(target=pc_side)
    t.start()

    wire = bytearray()  # everything the phone sends, to prove the PIN never crosses the wire
    offer = bt._recv_line(phone_sock)
    kp = crypto.Keypair.generate()
    ctx = "|".join(["phonebridge-pair-v4", offer["pub"], kp.public_b64(), offer["nonce"]]).encode()
    pake = spake2.Spake2("A", pin_guess, ctx)
    hello = {"v": 4, "pub": kp.public_b64(), "name": "Pixel 9", "spake": base64.b64encode(pake.share).decode()}
    line = (json.dumps(hello) + "\n").encode()
    wire += line
    phone_sock.sendall(line)
    view = {"offer": offer, "keypair": kp, "wire": bytes(wire)}
    try:
        first = bt._recv_line(phone_sock)
        view["reply"] = first
        if first.get("ok") is False:
            return_early = True
        else:
            keys = pake.finish(base64.b64decode(first["spake"]))
            view["keys"] = keys
            confirm_line = (json.dumps({"confirm": keys.mac_phone.hex()}) + "\n").encode()
            wire += confirm_line
            view["wire"] = bytes(wire)
            phone_sock.sendall(confirm_line)
            second = bt._recv_line(phone_sock)   # the PC's confirmation, or a refusal for a wrong PIN
            view["pc_confirm"] = second
            view["reply"] = second
            if second.get("confirm") is not None:
                view["reply"] = bt._recv_line(phone_sock)   # allow / decline
        if view["reply"].get("ok"):
            n = crypto.unpack_frame_len(phone_sock.recv(4))
            blob = b""
            while len(blob) < n:
                blob += phone_sock.recv(n - len(blob))
            view["blob"] = blob
            # the phone answers with its Ed25519 identity key, sealed under the new pairing key
            from phonebridge import secure_session
            phone_key = crypto.derive_session_key_v4(kp.private, crypto.public_from_b64(offer["pub"]), keys.ke)
            view["phone_key"] = phone_key
            ident = secure_session.Identity.generate()
            view["identity"] = ident
            if identity == "valid":
                frame = {"id_pub": ident.public_b64}
            elif identity == "short":
                frame = {"id_pub": "AAAA"}
            else:
                frame = None
            if frame is not None:
                sealed = crypto.SessionCipher(phone_key).encrypt(json.dumps(frame).encode())
                phone_sock.sendall(crypto.pack_frame(sealed))
    except Exception as exc:  # noqa: BLE001
        view["phone_error"] = exc
    t.join(15)
    phone_sock.close()
    return out, view


def main() -> int:
    tmp = Path(tempfile.mkdtemp()) / "devices"
    pairing.DEVICES_DIR = tmp

    print("1. happy path")
    pc = pairing.PendingPairing(bt_addr="AA:BB:CC:DD:EE:FF", pc_name="My Linux PC")
    qr = pc.qr_payload()
    check("QR carries v/bt_addr/token/pub", {"v", "bt_addr", "token", "pub"} <= qr.keys())
    seen_codes = []
    out, view = run_handshake(pc, pc.token, confirm=lambda name, code: seen_codes.append((name, code)) or True)
    dev = out.get("device")
    check("PC finished pairing", dev is not None)
    check("offer never contains the PIN", pc.token not in json.dumps(view["offer"]))
    check("the PIN never crosses the wire from the phone", pc.token.encode() not in view["wire"])
    phone_key = view["phone_key"]
    check("PC and phone derive the IDENTICAL session key", dev is not None and phone_key == dev.session_key)
    check("PC and phone compute the IDENTICAL fingerprint",
          dev is not None and dev.fingerprint == crypto.fingerprint(view["offer"]["pub"], view["keypair"].public_b64()))
    code_phone = crypto.confirmation_code(view["offer"]["pub"], view["keypair"].public_b64())
    check("both screens show the same confirmation code", seen_codes and seen_codes[0][1] == code_phone)
    check("PC proves the PIN back to the phone (SPAKE2 confirmation)",
          view["pc_confirm"].get("confirm") == view["keys"].mac_pc.hex())
    check("the offer announces SPAKE2", view["offer"].get("pake") == "spake2-p256" and view["offer"]["v"] == 4)
    check("the PIN-derived value w never appears on the wire either",
          spake2.password_to_w(pc.token).to_bytes(32, "big") not in view["wire"])
    lan = json.loads(crypto.SessionCipher(phone_key).decrypt(view["blob"]))
    check("LAN info arrives encrypted with the session key",
          lan["lan_ip"] == "192.168.1.5" and lan["lan_port"] == 28100 and lan["cert_fingerprint"] == "ab" * 32)
    check("LAN info carries the PC's Ed25519 identity key", lan.get("id_pub") == pairing.pc_identity().public_b64)
    check("the PC stored the phone's Ed25519 identity key", dev is not None and dev.identity_pub == view["identity"].public_b64)
    reloaded = pairing.PairedDevice.load(dev.fingerprint) if dev else None
    check("the stored identity key survives a reload", reloaded is not None and reloaded.identity_pub == view["identity"].public_b64)
    check("pairing persisted (PC control off by default)",
          reloaded is not None and reloaded.session_key == dev.session_key and reloaded.allow_pc_control is False)

    print("1b. identity key is mandatory")
    confirm_wait, bt.PHONE_CONFIRM_DEADLINE = bt.PHONE_CONFIRM_DEADLINE, 2   # the real wait covers the phone's user (4d)
    for mode, why in ((None, "a phone that sends no identity key"), ("short", "an invalid identity key")):
        pcx = pairing.PendingPairing(bt_addr=None, pc_name="PC")
        before = {d.fingerprint for d in pairing.PairedDevice.list_all()}
        outx, _viewx = run_handshake(pcx, pcx.token, identity=mode)
        check(f"{why} aborts pairing and leaves nothing behind",
              isinstance(outx.get("error"), pairing.PairingError)
              and not ({d.fingerprint for d in pairing.PairedDevice.list_all()} - before))
        check("…and the working pairing of the same phone is untouched",
              {d.fingerprint for d in pairing.PairedDevice.list_all()} == before and dev.fingerprint in before
              and pairing.PairedDevice.load(dev.fingerprint).identity_pub == dev.identity_pub)
    bt.PHONE_CONFIRM_DEADLINE = confirm_wait
    again = pairing.PendingPairing(bt_addr=None, pc_name="PC")
    outr, _viewr = run_handshake(again, again.token)
    check("a pairing that completes replaces the phone's earlier one",
          [d.fingerprint for d in pairing.PairedDevice.list_all() if d.name == dev.name] == [outr["device"].fingerprint])

    print("2. wrong PIN is refused, counted, and locks the dialog out")
    pc2 = pairing.PendingPairing(bt_addr=None, pc_name="PC")
    for i in range(pairing.MAX_FAILED_ATTEMPTS):
        out, view = run_handshake(pc2, "000000" if pc2.token != "000000" else "111111")
        check(f"wrong PIN #{i + 1} raises PairingError and tells the phone",
              isinstance(out.get("error"), pairing.PairingError) and view["reply"].get("ok") is False
              and "confirm" not in view.get("pc_confirm", {}))
    out, view = run_handshake(pc2, pc2.token)  # even the right PIN is refused after lockout
    check("right PIN refused after too many wrong ones", isinstance(out.get("error"), pairing.PairingError))

    print("3. declined on the PC")
    pc3 = pairing.PendingPairing(bt_addr=None, pc_name="PC")
    before = len(pairing.PairedDevice.list_all())
    out, view = run_handshake(pc3, pc3.token, confirm=lambda *_: False)
    check("decline raises and nothing is saved",
          isinstance(out.get("error"), pairing.PairingError) and len(pairing.PairedDevice.list_all()) == before)

    print("4. expired PIN")
    pc4 = pairing.PendingPairing(bt_addr=None, pc_name="PC")
    pc4.created_at -= pairing.PAIRING_TTL_SECONDS + 1
    out, _ = run_handshake(pc4, pc4.token)
    check("expired PIN refused", isinstance(out.get("error"), pairing.PairingError))

    print("4b. a code is single-use and lives one minute")
    check("the code lifetime is 60 seconds", pairing.PAIRING_TTL_SECONDS == 60)
    pc5 = pairing.PendingPairing(bt_addr=None, pc_name="PC")
    first, _ = run_handshake(pc5, pc5.token)
    check("the first phone to prove the PIN pairs", first.get("device") is not None)
    check("…and that consumes the code", pc5.used and pc5.is_spent())
    second, view2 = run_handshake(pc5, pc5.token)
    check("a second phone (or a replay of the same exchange) with the right PIN is refused",
          isinstance(second.get("error"), pairing.PairingError) and "already used" in str(second["error"]))
    pc6 = pairing.PendingPairing(bt_addr=None, pc_name="PC")
    declined, _ = run_handshake(pc6, pc6.token, confirm=lambda *_: False)
    check("a declined pairing still consumes the code", pc6.used)
    pc7 = pairing.PendingPairing(bt_addr=None, pc_name="PC")
    check("a fresh code reports its remaining life", 58 <= pc7.seconds_left() <= 60 and not pc7.is_spent())
    pc7.created_at -= 61
    check("after 60 s the code is spent", pc7.is_spent() and pc7.seconds_left() == 0)

    print("4c. a broken connection is a failed attempt, not a broken pairing route")

    class Broken:
        """A socket whose peer goes away: fails at the `fail_at`-th send, or at the first read."""

        def __init__(self, real, fail_at=None, error=ConnectionResetError):
            self.real, self.fail_at, self.error, self.sends = real, fail_at, error, 0

        def sendall(self, data):
            self.sends += 1
            if self.sends == self.fail_at:
                raise self.error("gone")
            return self.real.sendall(data)

        def recv(self, n):
            if self.fail_at is None:
                raise self.error("gone")
            return self.real.recv(n)

    for error in (ConnectionResetError, socket.timeout):
        a, b = socket.socketpair()
        try:
            bt.run_pairing_handshake(Broken(a, error=error), pairing.PendingPairing(bt_addr=None, pc_name="PC"),
                                     "192.168.1.5", 28100, "ab" * 32, lambda *_: True)
            caught = None
        except Exception as exc:  # noqa: BLE001
            caught = exc
        a.close()
        b.close()
        check(f"{error.__name__} before the phone answers raises PairingError, not OSError",
              isinstance(caught, pairing.PairingError) and not isinstance(caught, OSError))

    # Drop the connection exactly when the PC sends "ok" (4th send) and the LAN info (5th), after the pairing was saved.
    real_exchange = bt.run_pairing_handshake
    for fail_at, what in ((4, "the 'allowed' reply"), (5, "the connection details")):
        pcd = pairing.PendingPairing(bt_addr=None, pc_name="PC")
        before = {d.fingerprint for d in pairing.PairedDevice.list_all()}
        bt.run_pairing_handshake = lambda conn, *a: real_exchange(Broken(conn, fail_at=fail_at, error=BrokenPipeError), *a)
        try:
            outd, _viewd = run_handshake(pcd, pcd.token)
        finally:
            bt.run_pairing_handshake = real_exchange
        check(f"a connection lost while sending {what} leaves no half-made pairing",
              isinstance(outd.get("error"), pairing.PairingError) and "connection" in str(outd["error"])
              and not ({d.fingerprint for d in pairing.PairedDevice.list_all()} - before))

    print("4d. the phone's user takes a while to compare the code")
    real_deadline = bt.PHONE_CONFIRM_DEADLINE
    check("the PC waits longer than the phone app's 3-minute confirmation", bt.PHONE_CONFIRM_DEADLINE > 180)

    def exchange(confirm_after: float | None, cancel=None, read_limit=10.0):
        """Pairs over a socket pair with a phone that sends its identity key `confirm_after` seconds late (None: never)."""
        pend = pairing.PendingPairing(bt_addr=None, pc_name="PC")
        before = {d.fingerprint for d in pairing.PairedDevice.list_all()}
        a, b = socket.socketpair()
        a.settimeout(read_limit)
        b.settimeout(10)
        res = {}

        def pc_side():
            try:
                res["device"] = bt.run_pairing_handshake(a, pend, "192.168.1.5", 28100, "ab" * 32, lambda *_: True, cancel)
            except Exception as exc:  # noqa: BLE001
                res["error"] = exc

        t = threading.Thread(target=pc_side, daemon=True)
        t.start()
        offer = bt._recv_line(b)
        kp = crypto.Keypair.generate()
        ctx = "|".join(["phonebridge-pair-v4", offer["pub"], kp.public_b64(), offer["nonce"]]).encode()
        pake = spake2.Spake2("A", pend.token, ctx)
        bt._send_line(b, {"v": 4, "pub": kp.public_b64(), "name": "Slow", "spake": base64.b64encode(pake.share).decode()})
        keys = pake.finish(base64.b64decode(bt._recv_line(b)["spake"]))
        bt._send_line(b, {"confirm": keys.mac_phone.hex()})
        bt._recv_line(b)
        bt._recv_line(b)
        cipher = crypto.SessionCipher(crypto.derive_session_key_v4(kp.private, crypto.public_from_b64(offer["pub"]), keys.ke))
        n = crypto.unpack_frame_len(b.recv(4))
        got = b""
        while len(got) < n:
            got += b.recv(n - len(got))
        if confirm_after is not None:
            time.sleep(confirm_after)
            from phonebridge import secure_session
            ident = secure_session.Identity.generate()
            b.sendall(crypto.pack_frame(cipher.encrypt(json.dumps({"id_pub": ident.public_b64}).encode())))
        t.join(8)
        a.close()
        b.close()
        res["running"] = t.is_alive()
        res["new"] = {d.fingerprint for d in pairing.PairedDevice.list_all()} - before
        return res

    bt.PHONE_CONFIRM_DEADLINE = 6
    res = exchange(1.5, read_limit=0.7)   # longer than a single message may take
    check("a confirmation that takes longer than an ordinary message still pairs",
          res.get("device") is not None and len(res["new"]) == 1)
    bt.PHONE_CONFIRM_DEADLINE = 0.5
    res = exchange(None)
    check("no confirmation: the pairing is abandoned and nothing is left",
          "not confirmed" in str(res.get("error")) and not res["running"] and not res["new"])
    bt.PHONE_CONFIRM_DEADLINE = 60
    closed = threading.Event()
    threading.Timer(1.0, closed.set).start()
    res = exchange(None, cancel=closed)
    check("closing the Pair dialog ends the wait at once",
          "closed on the PC" in str(res.get("error")) and not res["running"] and not res["new"])
    bt.PHONE_CONFIRM_DEADLINE = real_deadline

    print("5. frames")
    c = crypto.SessionCipher(phone_key)
    blob = c.encrypt(b"hello")
    check("tampered ciphertext is rejected", _raises(lambda: c.decrypt(blob[:-1] + bytes([blob[-1] ^ 0xFF]))))
    check("oversized frame length is refused", _raises(lambda: crypto.unpack_frame_len(b"\xff\xff\xff\xff")))
    tag_a = crypto.discovery_tag(phone_key, 5)
    check("discovery tag is stable per bucket and differs across buckets/keys",
          tag_a == crypto.discovery_tag(phone_key, 5) and tag_a != crypto.discovery_tag(phone_key, 6)
          and tag_a != crypto.discovery_tag(b"\x01" * 32, 5))

    shutil.rmtree(tmp.parent, ignore_errors=True)
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


def _raises(fn) -> bool:
    try:
        fn()
    except Exception:  # noqa: BLE001
        return True
    return False


if __name__ == "__main__":
    raise SystemExit(main())
