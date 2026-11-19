"""X25519 ECDH handshake + AES-GCM framing shared by every transport.

Mirrors android/app/src/main/kotlin/.../crypto/Crypto.kt message-for-message —
if you change the wire format here, change it there too.
"""
from __future__ import annotations

import base64
import os
import secrets
import struct
from dataclasses import dataclass

from hmac import compare_digest as hmac_compare

from cryptography.hazmat.primitives import hashes, hmac
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

NONCE_LEN = 12
TAG_LEN = 16
FRAME_LEN_BYTES = 4


@dataclass
class Keypair:
    private: X25519PrivateKey
    public: X25519PublicKey

    @staticmethod
    def generate() -> "Keypair":
        priv = X25519PrivateKey.generate()
        return Keypair(priv, priv.public_key())

    def public_b64(self) -> str:
        raw = self.public.public_bytes_raw()
        return base64.b64encode(raw).decode("ascii")


def public_from_b64(b64: str) -> X25519PublicKey:
    raw = base64.b64decode(b64)
    return X25519PublicKey.from_public_bytes(raw)


def derive_session_key_v4(my_priv: X25519PrivateKey, their_pub: X25519PublicKey, spake_key: bytes) -> bytes:
    """Pairing v4: HKDF-SHA256(ECDH shared secret, salt=SPAKE2 key Ke) -> 32-byte AES key. Ke only exists if both sides
    knew the PIN, and it is fresh for every exchange."""
    shared = my_priv.exchange(their_pub)
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=spake_key, info=b"phonebridge-v4|pairing").derive(shared)


def fingerprint(pc_pub_b64: str, phone_pub_b64: str) -> str:
    """Short human-verifiable hex fingerprint, order-independent."""
    a = base64.b64decode(pc_pub_b64)
    b = base64.b64decode(phone_pub_b64)
    digest = hashes.Hash(hashes.SHA256())
    # sort so both sides compute the same value regardless of role
    first, second = sorted([a, b])
    digest.update(first)
    digest.update(second)
    return digest.finalize()[:8].hex()


def gen_token(n_digits: int = 6) -> str:
    return "".join(str(secrets.randbelow(10)) for _ in range(n_digits))   # uniform digits (b % 10 on a byte is biased)


def gen_nonce() -> str:
    return base64.b64encode(os.urandom(16)).decode("ascii")


def discovery_tag(session_key: bytes, bucket: int) -> str:
    """Rotating proof that a LAN broadcast comes from a phone paired with this PC, without
    putting the pairing fingerprint on the wire. `bucket` = unix time // DISCOVERY_BUCKET_SECONDS."""
    h = hmac.HMAC(session_key, hashes.SHA256())
    h.update(f"phonebridge-discover-v2|{bucket}".encode())
    return h.finalize().hex()[:32]


def relay_room(session_key: bytes) -> str:
    """Name of this pairing's meeting point on a relay server: HMAC(session_key, label)[:32 hex]. Only the
    two paired devices can compute it, so the relay needs no accounts and can't link rooms to people."""
    h = hmac.HMAC(session_key, hashes.SHA256())
    h.update(b"phonebridge-relay-v1")
    return h.finalize().hex()[:32]


DISCOVERY_BUCKET_SECONDS = 300


def proof_matches(expected_hex: str, given: object) -> bool:
    return isinstance(given, str) and hmac_compare(expected_hex.encode("ascii"), given.encode("utf-8", "replace"))


def confirmation_code(pc_pub_b64: str, phone_pub_b64: str) -> str:
    """6-digit code both screens show so the user can spot a device in the middle
    (each side computes it from the two public keys it actually used)."""
    first, second = sorted([base64.b64decode(pc_pub_b64), base64.b64decode(phone_pub_b64)])
    digest = hashes.Hash(hashes.SHA256())
    digest.update(b"phonebridge-confirm-v2")
    digest.update(first)
    digest.update(second)
    n = int.from_bytes(digest.finalize()[:4], "big") % 1_000_000
    return f"{n:06d}"


# Each direction authenticates its own label as AES-GCM associated data (PROTOCOL.md section 1, v3), so a
# frame the PC sent can't be reflected back at the PC as if the phone had sent it, or vice versa.
DIRECTION_AAD = {"pc": b"phonebridge-v3|pc>phone", "phone": b"phonebridge-v3|phone>pc"}


class SessionCipher:
    """Encrypt/decrypt frames with a fixed session key; random nonce per message.

    `role` is who *we* are: the PC passes "pc"; the phone (and the test peers that play it) use the
    default "phone". We seal with our own direction label and only open frames carrying the peer's."""

    def __init__(self, session_key: bytes, role: str = "phone", recv_key: bytes | None = None):
        """`recv_key` (v4 sessions) gives each direction its own key; without it both directions share one."""
        if role not in DIRECTION_AAD:
            raise ValueError(f"role must be 'pc' or 'phone', not {role!r}")
        self._role = role
        self._send = AESGCM(session_key)
        self._recv = AESGCM(recv_key if recv_key is not None else session_key)
        self._send_aad = DIRECTION_AAD[role]
        self._recv_aad = DIRECTION_AAD["phone" if role == "pc" else "pc"]

    def set_send_key(self, key: bytes) -> None:
        self._send = AESGCM(key)

    def set_recv_key(self, key: bytes) -> None:
        self._recv = AESGCM(key)

    def encrypt(self, plaintext: bytes) -> bytes:
        nonce = os.urandom(NONCE_LEN)
        ct = self._send.encrypt(nonce, plaintext, self._send_aad)
        return nonce + ct

    def decrypt(self, blob: bytes) -> bytes:
        nonce, ct = blob[:NONCE_LEN], blob[NONCE_LEN:]
        return self._recv.decrypt(nonce, ct, self._recv_aad)


def pack_frame(ciphertext: bytes) -> bytes:
    return struct.pack(">I", len(ciphertext)) + ciphertext


# Largest frame we will ever allocate for (video access units and 64 KiB-1 MiB file chunks
# are far smaller). Stops a peer from making us buffer gigabytes with one length prefix.
MAX_FRAME_BYTES = 8 * 1024 * 1024


def unpack_frame_len(header: bytes) -> int:
    (n,) = struct.unpack(">I", header)
    if n > MAX_FRAME_BYTES:
        raise ValueError(f"frame too large ({n} bytes)")
    return n
