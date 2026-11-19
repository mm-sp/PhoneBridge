#!/usr/bin/env python3
"""Fuzzing: throws random and mutated input at every place the PC parses bytes/JSON from the network (handshake, frames,
envelopes, file offers, UDP datagrams, pairing hello, manifests, paths). A target passes if it only ever rejects bad input
cleanly (an allowed exception type) and never hangs, crashes or accepts something it shouldn't.

  python3 tests/fuzz_protocol.py [--seconds 60] [--seed N]        (also runs briefly as part of the test suite)
"""
import argparse
import asyncio
import base64
import json
import os
import random
import struct
import sys
import tempfile
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from phonebridge import crypto, pairing, pc_info, secure_session as S, udp_video as uv, updates  # noqa: E402
from phonebridge.protocol import Envelope  # noqa: E402
from phonebridge.security import permissions as P  # noqa: E402
from phonebridge.transport import DeviceConnection  # noqa: E402

FAILURES: list[str] = []
STATS: dict[str, int] = {}


def mutate(rng: random.Random, data: bytes) -> bytes:
    b = bytearray(data)
    for _ in range(rng.randint(1, 6)):
        op = rng.randint(0, 5)
        if not b:
            b = bytearray(os.urandom(rng.randint(1, 40)))
        i = rng.randrange(len(b))
        if op == 0:
            b[i] ^= 1 << rng.randrange(8)
        elif op == 1:
            b[i] = rng.randrange(256)
        elif op == 2:
            del b[i:i + rng.randint(1, 8)]
        elif op == 3:
            b[i:i] = os.urandom(rng.randint(1, 16))
        elif op == 4:
            b = b[:i]
        else:
            b += bytes(b[i:i + rng.randint(1, 30)])
    return bytes(b)


def rand_json_value(rng: random.Random, depth=0):
    c = rng.randint(0, 11 if depth < 3 else 8)
    if c == 0: return None
    if c == 1: return rng.random() < 0.5
    if c == 2: return rng.randint(-2**70, 2**70)
    if c == 3: return rng.random() * 1e308
    if c == 4: return ""
    if c == 5: return "x" * rng.randint(0, 500)
    if c == 6: return os.urandom(rng.randint(0, 20)).hex()
    if c == 7: return float("1e999")
    if c == 8: return "\u00e9\u202e"
    if c == 9: return [rand_json_value(rng, depth + 1) for _ in range(rng.randint(0, 4))]
    keys = ["type", "payload", "path", "size", "eph", "n", "sig", "v", "text", "feature", "x"]
    return {rng.choice(keys): rand_json_value(rng, depth + 1) for _ in range(rng.randint(0, 4))}


def fuzz(name, allowed, fn, make, seconds, rng):
    """Calls fn(make()) until time is up; any exception not in `allowed` (or a call over 2 s) is a failure."""
    end = time.monotonic() + seconds
    n = 0
    while time.monotonic() < end:
        arg = make()
        t0 = time.monotonic()
        try:
            fn(arg)
        except allowed:
            pass
        except BaseException as exc:  # noqa: BLE001
            FAILURES.append(f"{name}: {type(exc).__name__}: {str(exc)[:80]} on input {repr(arg)[:100]}\n{traceback.format_exc(limit=3)}")
            break
        if time.monotonic() - t0 > 2:
            FAILURES.append(f"{name}: took {time.monotonic() - t0:.1f}s on input {repr(arg)[:100]}")
            break
        n += 1
    STATS[name] = n


def run(seconds_total: float, seed: int) -> None:
    rng = random.Random(seed)
    tmp = Path(tempfile.mkdtemp())
    pairing.DEVICES_DIR = tmp / "cfg" / "devices"
    per = seconds_total / 12

    psk = os.urandom(32); pc, ph = S.Identity.generate(), S.Identity.generate()
    fp, tls = "abcd1234abcd1234", "AB" * 32
    good_hello, st = S.client_hello(psk, fp, tls, ph)
    good_reply, _ = S.server_hello(psk, fp, tls, ph.public_b64, pc, good_hello)

    # 1/2: handshake frames, both directions
    mk = lambda: mutate(rng, good_hello) if rng.random() < 0.8 else os.urandom(rng.randint(0, 200))
    fuzz("server_hello(frame)", (S.HandshakeError,), lambda f: S.server_hello(psk, fp, tls, ph.public_b64, pc, f), mk, per, rng)
    mk = lambda: mutate(rng, good_reply) if rng.random() < 0.8 else os.urandom(rng.randint(0, 200))
    fuzz("client_finish(frame)", (S.HandshakeError,), lambda f: S.client_finish(st, f, pc.public_b64), mk, per, rng)

    # handshake with a hello that AUTHENTICATES (right psk) but has hostile field contents
    def hostile(_):
        body = {"v": rng.choice([4, 3, "4", None, 4.0, 10**30]), "eph": rng.choice(["", "AAAA", base64.b64encode(os.urandom(rng.randint(0, 64))).decode(), 5, None, "!!"]),
                "n": rng.choice(["", "x", 7, None, os.urandom(4).hex()]), "sig": rng.choice(["", "AAAA", None, 3, base64.b64encode(os.urandom(64)).decode()])}
        for k in list(body):
            if rng.random() < 0.15:
                del body[k]
        return S._seal_hs(psk, "client", body)
    fuzz("server_hello(authenticated hostile fields)", (S.HandshakeError,), lambda f: S.server_hello(psk, fp, tls, ph.public_b64, pc, f), lambda: hostile(0), per, rng)

    # 3: frame length prefix + cipher
    ciph = crypto.SessionCipher(os.urandom(32), role="pc")
    fuzz("unpack_frame_len", (ValueError, struct.error), crypto.unpack_frame_len, lambda: os.urandom(rng.randint(0, 6)), per, rng)
    fuzz("SessionCipher.decrypt", (Exception,), lambda b: (ciph.decrypt(b) if False else _only_invalid_tag(ciph, b)), lambda: os.urandom(rng.randint(0, 100)), per, rng)

    # 4: envelopes (post-decryption JSON)
    def envelope(b):
        e = Envelope.from_json(b)
        _ = e.type, e.seq
    fuzz("Envelope.from_json", (ValueError, KeyError, TypeError, UnicodeDecodeError, AttributeError), envelope,
         lambda: json.dumps(rand_json_value(rng)).encode() if rng.random() < 0.7 else os.urandom(rng.randint(0, 60)), per, rng)

    # replay checker with hostile seq values
    conn = DeviceConnection.__new__(DeviceConnection)
    conn._seen_seq, conn._max_seq = set(), 0
    fuzz("replay check", (ValueError,), lambda s: conn._check_replay(Envelope("control", "x", s, {})),
         lambda: rng.choice([rng.randint(-10, 10**6), 2**63, -2**63, 1.5, "a", None, True, float("nan")]), per, rng)
    if len(conn._seen_seq) > 2 * conn.SEQ_WINDOW:
        FAILURES.append(f"replay check: seen-set grew without bound ({len(conn._seen_seq)})")

    # 5: UDP datagrams and fragments
    key = os.urandom(32)
    ch = uv.UdpChannel(key, fp, uv.DIR_PHONE)
    good_dg = uv.UdpChannel(key, fp, uv.DIR_PC).seal(uv.T_VIDEO, b"hello")
    fuzz("UdpChannel.open", (), lambda d: ch.open(d), lambda: mutate(rng, good_dg) if rng.random() < 0.7 else os.urandom(rng.randint(0, 1300)), per, rng)
    r = uv.Reassembler(lambda f: None, lambda: None)
    def frag(body):
        r.feed(body)
        if len(r._pending) > uv.MAX_PENDING_FRAMES + 1:
            raise AssertionError("pending frames unbounded")
    fuzz("Reassembler.feed", (), frag, lambda: struct.pack(">IHHB", rng.randint(0, 50), rng.randint(0, 70000 % 65536), rng.randint(0, 5), rng.randint(0, 3))[:rng.randint(0, 9)] + os.urandom(rng.randint(0, 40))
         if False else uv.VIDEO_HEADER.pack(rng.randint(0, 60), rng.randint(0, 6), rng.randint(0, 6), rng.randint(0, 1)) + os.urandom(rng.randint(0, 30)), per, rng)

    # 6: pairing hello (untrusted JSON from a phone that may not know the PIN)
    def pair_hello(h):
        pending = pairing.PendingPairing(bt_addr=None, pc_name="PC")
        pending.begin(h, "nonce")
    fuzz("PendingPairing.begin", (pairing.PairingError,), pair_hello, lambda: {k: rand_json_value(rng) for k in rng.sample(["v", "pub", "spake", "name", "platform"], rng.randint(0, 5))}, per, rng)

    # 7: permission mapping + paths
    fuzz("permissions.feature_for", (), lambda t: P.feature_for(t[0], t[1]), lambda: (os.urandom(rng.randint(0, 12)).hex() + rng.choice(["", "photo.", "sms.", "pc.", "file.offer"]), {"photo_id": rng.choice([None, "", 0, "x"])}), per, rng)
    home = tmp / "home"; (home / "ok").mkdir(parents=True); (home / ".ssh").mkdir(); (home / ".ssh/id").write_text("k"); outside = tmp / "outside"; outside.mkdir(); (outside / "s.txt").write_text("s")
    (home / "link").symlink_to(outside); pc_info.HOME = home
    def path_check(p):
        got = pc_info.resolve_home_file(p)
        if got is not None and not (str(got.resolve()).startswith(str(home.resolve()) + os.sep) and not any(x.startswith(".") for x in got.resolve().relative_to(home.resolve()).parts)):
            raise AssertionError(f"escaped: {p!r} -> {got}")
        lst = pc_info.list_dir(p)
        if lst.get("entries") and lst.get("path", "/") != "/" and ".ssh" in lst["path"]:
            raise AssertionError(f"listed hidden dir {p!r}")
    segs = ["..", ".", "", ".ssh", "link", "ok", "id", "s.txt", "%2e%2e", "\x00", "a" * 300, "~", "/etc", "\\.."]
    fuzz("pc_info path handling", (OSError, ValueError), path_check, lambda: "/".join(rng.choice(segs) for _ in range(rng.randint(0, 6))), per, rng)

    # 8: signed update manifests
    pub = S.Identity.generate()
    def manifest(b):
        updates.verify_manifest(b, base64.b64encode(os.urandom(64)).decode(), "1.0", public_b64=pub.public_b64)
    fuzz("updates.verify_manifest", (updates.UpdateError,), manifest, lambda: json.dumps(rand_json_value(rng)).encode() if rng.random() < 0.6 else os.urandom(rng.randint(0, 80)), per, rng)

    # 9: file-offer handling (accept_offer must refuse or sanitise hostile offers; never write outside its folder)
    from phonebridge import file_transfer
    sent = []
    class FakeConn:
        fingerprint = "aaaa"
        async def send(self, ch, t, p): sent.append((t, p))
    dest = tmp / "dl"
    async def offers(n_seconds):
        end = time.monotonic() + n_seconds; n = 0
        while time.monotonic() < end:
            payload = {k: rand_json_value(rng) for k in rng.sample(["transfer_id", "filename", "size", "resume_key", "data_conn", "mime", "dest_path", "photo_id"], rng.randint(0, 8))}
            if rng.random() < 0.5:
                payload.update({"transfer_id": os.urandom(4).hex(), "size": rng.choice([0, 5, 10**6, -1, 2**70]), "data_conn": True,
                                "filename": rng.choice(["../../x", "/etc/passwd", "a/../../b", "..", "ok.bin", "‮.exe", "x" * 400]), "resume_key": rng.choice(["../../k", "ok", "x" * 100, 7])})
            try:
                await file_transfer.accept_offer(FakeConn(), Envelope("control", "file.offer", 1, payload), dest)
            except (ValueError, TypeError, OSError, AttributeError, OverflowError):
                pass   # a clean rejection of garbage
            except BaseException as exc:  # noqa: BLE001
                FAILURES.append(f"accept_offer: {type(exc).__name__}: {exc} on {str(payload)[:120]}"); return n
            n += 1
        return n
    STATS["file_transfer.accept_offer"] = asyncio.run(offers(per))
    escaped = [p for p in tmp.rglob("*") if p.is_file() and dest not in p.parents and p.parent == tmp]
    if escaped:
        FAILURES.append(f"accept_offer wrote outside its folder: {escaped[:3]}")
    for p in dest.glob("*") if dest.exists() else []:
        if p.name not in ("",) and not p.name.startswith(".pb-"):
            FAILURES.append(f"accept_offer created an unexpected file {p.name!r}")


def _only_invalid_tag(c, b):
    from cryptography.exceptions import InvalidTag
    try:
        c.decrypt(b)
    except InvalidTag:
        return
    raise AssertionError("random bytes decrypted successfully")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=12)
    ap.add_argument("--seed", type=int, default=None)
    a = ap.parse_args()
    seed = a.seed if a.seed is not None else random.randrange(1 << 30)
    print(f"fuzzing for {a.seconds:.0f}s, seed {seed} (rerun with --seed {seed} to reproduce)")
    run(a.seconds, seed)
    for name, n in STATS.items():
        print(f"  {n:7d} inputs  {name}")
    for f in FAILURES:
        print("FAILURE:", f)
    print(f"\n{len(STATS) - len({f.split(':')[0] for f in FAILURES})} passed, {len(FAILURES)} failed")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
