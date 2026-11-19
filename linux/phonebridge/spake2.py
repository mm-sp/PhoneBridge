"""SPAKE2 password-authenticated key exchange (RFC 9382 construction, ciphersuite P-256 / SHA-256 / HKDF / HMAC).

Used to pair a phone with the PC from the 6-digit PIN. Unlike the old "HMAC proof of the PIN", a PAKE leaks nothing
an eavesdropper or a rogue device on the LAN could test PIN guesses against offline: every guess needs one live
exchange with the real peer, and the PC counts and limits those.

Roles: A = the phone (sends X = x*G + w*M), B = the PC (sends Y = y*G + w*N). M and N are the RFC 9382 P-256 points.
Android's Spake2.kt implements the same bytes with Bouncy Castle; tests/test_spake2.py checks both against each other.

Side channels: every multiplication by a secret scalar (x, y or w) goes through `mul_secret`, which is (a) done by OpenSSL
(constant-time) whenever the base point is the generator G, and otherwise (b) a Montgomery ladder that performs the same
operations for every bit, on a scalar that is freshly blinded (k + r*n with random r) on every call, so the operation trace
does not depend on the secret. Python big integers are still not constant-time at the instruction level, so this mitigates
timing leaks rather than eliminating them; a pairing is also a one-shot, 60-second, three-attempt exchange. This is not a
general-purpose elliptic-curve library.
"""
from __future__ import annotations

import hashlib
import hmac
import os

P = 0xFFFFFFFF00000001000000000000000000000000FFFFFFFFFFFFFFFFFFFFFFFF
A_COEF = P - 3
B_COEF = 0x5AC635D8AA3A93E7B3EBBD55769886BC651D06B0CC53B0F63BCE3C3E27D2604B
ORDER = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
G = (0x6B17D1F2E12C4247F8BCE6E563A440F277037D812DEB33A0F4A13945D898C296,
     0x4FE342E2FE1A7F9B8EE7EB4A7C0F9E162BCE33576B315ECECBB6406837BF51F5)

Point = tuple[int, int] | None   # None = the point at infinity


def on_curve(pt: Point) -> bool:
    if pt is None:
        return True
    x, y = pt
    return 0 <= x < P and 0 <= y < P and (y * y - (x * x * x + A_COEF * x + B_COEF)) % P == 0


def add(p1: Point, p2: Point) -> Point:
    if p1 is None:
        return p2
    if p2 is None:
        return p1
    x1, y1 = p1
    x2, y2 = p2
    if x1 == x2:
        if (y1 + y2) % P == 0:
            return None
        lam = (3 * x1 * x1 + A_COEF) * pow(2 * y1, -1, P) % P
    else:
        lam = (y2 - y1) * pow(x2 - x1, -1, P) % P
    x3 = (lam * lam - x1 - x2) % P
    return (x3, (lam * (x1 - x3) - y1) % P)


def neg(pt: Point) -> Point:
    return None if pt is None else (pt[0], (-pt[1]) % P)


def mul(k: int, pt: Point) -> Point:
    k %= ORDER
    result: Point = None
    addend = pt
    while k:
        if k & 1:
            result = add(result, addend)
        addend = add(addend, addend)
        k >>= 1
    return result


def mul_secret(k: int, pt: Point) -> Point:
    """k*pt for a SECRET scalar k: OpenSSL for the generator, otherwise a blinded Montgomery ladder (see module docstring)."""
    k %= ORDER
    if pt == G:
        from cryptography.hazmat.primitives.asymmetric import ec

        nums = ec.derive_private_key(k, ec.SECP256R1()).public_key().public_numbers()
        return (nums.x, nums.y)
    k += ORDER * (int.from_bytes(os.urandom(8), "big") | (1 << 63))   # blind: same point, different bit pattern every call
    r0: Point = None
    r1: Point = pt
    for i in range(k.bit_length() + (-k.bit_length() % 64) - 1, -1, -1):   # fixed-length loop for the blinded size
        bit = (k >> i) & 1
        total = add(r0, r1)       # both operations always run; the bit only selects which results are kept
        r0d, r1d = add(r0, r0), add(r1, r1)
        r0, r1 = (total, r1d) if bit else (r0d, total)
    return r0


def encode(pt: Point) -> bytes:
    """Uncompressed SEC1 (65 bytes)."""
    if pt is None:
        raise ValueError("cannot encode the point at infinity")
    return b"\x04" + pt[0].to_bytes(32, "big") + pt[1].to_bytes(32, "big")


def decode(data: bytes) -> tuple[int, int]:
    """Parses a SEC1 point (uncompressed or compressed) and insists it is a valid, non-identity curve point."""
    if len(data) == 65 and data[0] == 4:
        pt = (int.from_bytes(data[1:33], "big"), int.from_bytes(data[33:], "big"))
    elif len(data) == 33 and data[0] in (2, 3):
        x = int.from_bytes(data[1:], "big")
        if x >= P:
            raise ValueError("bad point")
        y2 = (x * x * x + A_COEF * x + B_COEF) % P
        y = pow(y2, (P + 1) // 4, P)   # P = 3 mod 4
        if y * y % P != y2:
            raise ValueError("bad point")
        pt = (x, y if (y & 1) == (data[0] & 1) else P - y)
    else:
        raise ValueError("bad point encoding")
    if not on_curve(pt):
        raise ValueError("point is not on the curve")
    return pt


# RFC 9382 section 4: the P-256 points M and N (nobody knows their discrete logs).
M = decode(bytes.fromhex("02886e2f97ace46e55ba9dd7242579f2993b64e16ef3dcab95afd497333d8fa12f"))
N = decode(bytes.fromhex("03d8bbd6c639c62937b04d997f38c3770719c629d7014d49a24b4f98baa1292b49"))

ID_PHONE = b"phonebridge-phone"
ID_PC = b"phonebridge-pc"
SALT = b"phonebridge-spake2-v1"


def password_to_w(pin: str) -> int:
    """Stretches the PIN into the SPAKE2 scalar w (scrypt, then reduced mod the group order). The stretching only slows
    down guesses the exchange already limits to a handful of live attempts."""
    raw = hashlib.scrypt(pin.encode("utf-8"), salt=SALT, n=2 ** 14, r=8, p=1, dklen=48, maxmem=64 * 1024 * 1024)
    return int.from_bytes(raw, "big") % (ORDER - 1) + 1


def _len8(b: bytes) -> bytes:
    return len(b).to_bytes(8, "little") + b


def _hkdf(ikm: bytes, info: bytes, length: int) -> bytes:
    prk = hmac.new(b"\x00" * 32, ikm, hashlib.sha256).digest()
    out, block, i = b"", b"", 1
    while len(out) < length:
        block = hmac.new(prk, block + info + bytes([i]), hashlib.sha256).digest()
        out += block
        i += 1
    return out[:length]


class Spake2:
    """One side of one exchange. Use a fresh instance per attempt.

    >>> phone = Spake2("A", "123456", context); pc = Spake2("B", "123456", context)
    >>> pc_keys = pc.finish(phone.share); phone_keys = phone.finish(pc.share)   # then compare/exchange the confirmations
    """

    def __init__(self, role: str, pin: str, context: bytes, scalar: int | None = None) -> None:
        if role not in ("A", "B"):
            raise ValueError("role must be 'A' (phone) or 'B' (PC)")
        self.role = role
        self.context = context
        self.w = password_to_w(pin)
        self._mine = M if role == "A" else N     # the blinding point for my own share
        self._theirs = N if role == "A" else M   # the blinding point for the peer's share
        self._x = scalar if scalar is not None else int.from_bytes(os.urandom(40), "big") % (ORDER - 1) + 1
        self.share: bytes = encode(add(mul_secret(self._x, G), mul_secret(self.w, self._mine)))

    def finish(self, peer_share: bytes) -> "Keys":
        """Combines with the peer's share. Raises ValueError for an invalid point. Never fails on a wrong PIN: that is
        only detectable through the confirmation MACs."""
        peer = decode(peer_share)
        unblinded = add(peer, neg(mul_secret(self.w, self._theirs)))
        z = mul_secret(self._x, unblinded)
        v = mul_secret(self.w, unblinded) if self.role == "A" else mul_secret(self._x, mul_secret(self.w, G))
        if z is None or v is None:
            raise ValueError("degenerate share")
        x_share, y_share = (self.share, peer_share) if self.role == "A" else (peer_share, self.share)
        x_share, y_share = encode(decode(x_share)), encode(decode(y_share))   # canonical (uncompressed) form in the transcript
        tt = (_len8(self.context) + _len8(ID_PHONE) + _len8(ID_PC) + _len8(x_share) + _len8(y_share)
              + _len8(encode(z)) + _len8(encode(v)) + _len8(self.w.to_bytes(32, "big")))
        digest = hashlib.sha256(tt).digest()
        ke, ka = digest[:16], digest[16:]
        kc = _hkdf(ka, b"ConfirmationKeys", 32)
        kc_a, kc_b = kc[:16], kc[16:]
        return Keys(ke=ke,
                    mac_phone=hmac.new(kc_a, y_share, hashlib.sha256).digest(),   # sent by the phone (A), covers the PC's share
                    mac_pc=hmac.new(kc_b, x_share, hashlib.sha256).digest())      # sent by the PC (B), covers the phone's share


class Keys:
    def __init__(self, ke: bytes, mac_phone: bytes, mac_pc: bytes) -> None:
        self.ke = ke
        self.mac_phone = mac_phone
        self.mac_pc = mac_pc


def macs_equal(expected: bytes, given: object) -> bool:
    return isinstance(given, (bytes, bytearray)) and hmac.compare_digest(expected, bytes(given))
