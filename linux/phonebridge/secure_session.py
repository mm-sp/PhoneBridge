"""Protocol v4 secure session: Ed25519 device identities + a per-connection ephemeral X25519 handshake.

Why: the pairing key alone (PROTOCOL.md section 1) is long-lived and shared by both devices, so
  (a) whoever later steals it can decrypt recorded traffic, and
  (b) either device could forge the other's messages.
v4 fixes both:
  * Every connection runs a fresh X25519 exchange, so past sessions stay secret even if the pairing key and the
    identity keys leak later (forward secrecy). The pairing key still gates the handshake and is mixed into the
    key schedule, so an attacker needs it *and* the ephemeral secret.
  * Each side signs the handshake with its own Ed25519 identity key (public key exchanged during pairing, inside the
    PIN-authenticated channel). The private keys never leave their device, so the PC cannot pose as the phone nor the
    reverse. Signatures also bind the PC's TLS certificate fingerprint (channel binding).
  * Separate keys per direction; the control channel rekeys (new ephemeral exchange, ratcheted from the old master)
    every REKEY_FRAMES frames or REKEY_SECONDS seconds.
  * File/video data connections and UDP video use keys derived from this session's exporter secret, so they only work
    alongside a live, authenticated control session.

Mirrors android/.../SecureSession.kt — change one, change the other.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import time
from dataclasses import dataclass

from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat

from . import crypto

VERSION = 4
REKEY_FRAMES = 1_000_000
REKEY_SECONDS = 3600
IDENTITY_KEY_NAME = "identity-ed25519"
_HS_AAD = {"client": b"phonebridge-v4|hs|phone>pc", "server": b"phonebridge-v4|hs|pc>phone"}


class HandshakeError(Exception):
    pass


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def _unb64(s: str) -> bytes:
    return base64.b64decode(s)


def _hkdf(ikm: bytes, salt: bytes | None, info: bytes, n: int = 32) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=n, salt=salt, info=info).derive(ikm)


# ----------------------------------------------------------------------------------------------- identity

class Identity:
    """A device's long-term Ed25519 signing key. The private half lives only in the keystore (PC) /
    Android Keystore-wrapped storage (phone) and is never transmitted."""

    def __init__(self, private: Ed25519PrivateKey) -> None:
        self._private = private
        self.public_raw = private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        self.public_b64 = _b64(self.public_raw)

    @staticmethod
    def generate() -> "Identity":
        return Identity(Ed25519PrivateKey.generate())

    @staticmethod
    def load_or_create(store, name: str = IDENTITY_KEY_NAME) -> "Identity":
        raw = store.get(name)
        if raw is None:
            ident = Identity.generate()
            store.set(name, ident._private.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption()))
            return ident
        return Identity(Ed25519PrivateKey.from_private_bytes(raw))

    def save(self, store, name: str = IDENTITY_KEY_NAME) -> None:
        store.set(name, self._private.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption()))

    def sign(self, message: bytes) -> bytes:
        return self._private.sign(message)


def verify_signature(public_b64: str, signature: bytes, message: bytes) -> bool:
    try:
        Ed25519PublicKey.from_public_bytes(_unb64(public_b64)).verify(signature, message)
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


def rotation_message(fp: str, new_pub_b64: str) -> bytes:
    return f"phonebridge-v4|rotate|{fp}|{new_pub_b64}".encode()


def sign_rotation(old_identity: "Identity", fp: str, new_pub_b64: str) -> str:
    """Identity rotation: the OLD key vouches for the NEW one (sent over an authenticated session)."""
    return _b64(old_identity.sign(rotation_message(fp, new_pub_b64)))


def verify_rotation(old_pub_b64: str, fp: str, new_pub_b64: str, sig_b64: str) -> bool:
    try:
        Ed25519PublicKey.from_public_bytes(_unb64(new_pub_b64))   # must be a valid key
        return verify_signature(old_pub_b64, _unb64(sig_b64), rotation_message(fp, new_pub_b64))
    except (ValueError, TypeError):
        return False


def fingerprint_of(public_b64: str) -> str:
    """Human-checkable fingerprint of an identity public key (SHA-256, first 16 bytes, hex)."""
    return hashlib.sha256(_unb64(public_b64)).digest()[:16].hex()


# --------------------------------------------------------------------------------------------- key schedule

@dataclass
class _Keys:
    c2s: bytes
    s2c: bytes


def _split(master: bytes) -> _Keys:
    return _Keys(_hkdf(master, None, b"phonebridge-v4|c2s"), _hkdf(master, None, b"phonebridge-v4|s2c"))


class Session:
    """The keys of one authenticated connection. `cipher` is what DeviceConnection uses for control frames."""

    def __init__(self, role: str, master: bytes, session_id: str) -> None:
        self.role = role                      # "pc" (server) or "phone" (client)
        self.session_id = session_id
        self._master = master
        self.exporter = _hkdf(master, None, b"phonebridge-v4|exporter")   # fixed for the session: data keys come from it
        keys = _split(master)
        send, recv = (keys.s2c, keys.c2s) if role == "pc" else (keys.c2s, keys.s2c)
        self.cipher = crypto.SessionCipher(send, role=role, recv_key=recv)
        self.peer_identity: str | None = None   # PC side: which of the phone's identity keys signed the handshake
        self.generation = 0
        self.frames_sent = 0
        self.started = time.monotonic()
        self._rekey_eph: X25519PrivateKey | None = None
        self._pending: _Keys | None = None

    # -- keys for other connections that belong to this session
    def data_cipher(self, label: str) -> crypto.SessionCipher:
        """Cipher for a file/video data connection ("file|<tid>", "video"): same key both ways, direction AAD."""
        return crypto.SessionCipher(_hkdf(self.exporter, None, b"phonebridge-v4|data|" + label.encode()), role=self.role)

    @property
    def udp_key(self) -> bytes:
        return _hkdf(self.exporter, None, b"phonebridge-v4|udp")

    # -- rekeying (PC initiates; see transport.DeviceConnection)
    def needs_rekey(self) -> bool:
        return self.frames_sent >= REKEY_FRAMES or time.monotonic() - self.started >= REKEY_SECONDS

    def start_rekey(self, _eph: X25519PrivateKey | None = None) -> str:
        self._rekey_eph = _eph or X25519PrivateKey.generate()
        return _b64(self._rekey_eph.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw))

    def respond_rekey(self, peer_eph_b64: str, _eph: X25519PrivateKey | None = None) -> str:
        """Responder: compute the next keys from the initiator's ephemeral key; returns our ephemeral key for the ack.
        The caller sends the ack under the OLD keys, then commit_send()."""
        mine = _eph or X25519PrivateKey.generate()
        self._derive_pending(mine, peer_eph_b64)
        return _b64(mine.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw))

    def finish_rekey(self, peer_eph_b64: str) -> None:
        """Initiator, on the ack: derive the next keys, switch receiving now; caller sends rekey.done then commit_send()."""
        if self._rekey_eph is None:
            raise HandshakeError("unexpected rekey acknowledgement")
        self._derive_pending(self._rekey_eph, peer_eph_b64)
        self._rekey_eph = None
        self.commit_recv()

    def _derive_pending(self, mine: X25519PrivateKey, peer_eph_b64: str) -> None:
        shared = mine.exchange(X25519PublicKey.from_public_bytes(_unb64(peer_eph_b64)))
        new_master = _hkdf(shared, self._master, b"phonebridge-v4|rekey|" + str(self.generation + 1).encode())
        self._pending_master = new_master
        self._pending = _split(new_master)

    def commit_send(self) -> None:
        if self._pending is None:
            raise HandshakeError("no rekey in progress")
        self.cipher.set_send_key(self._pending.s2c if self.role == "pc" else self._pending.c2s)
        self._sent_switched = True
        self._maybe_finish()

    def commit_recv(self) -> None:
        if self._pending is None:
            raise HandshakeError("no rekey in progress")
        self.cipher.set_recv_key(self._pending.c2s if self.role == "pc" else self._pending.s2c)
        self._recv_switched = True
        self._maybe_finish()

    _sent_switched = False
    _recv_switched = False

    def _maybe_finish(self) -> None:
        if self._sent_switched and self._recv_switched:
            self._master = self._pending_master
            self._pending = None
            self._sent_switched = self._recv_switched = False
            self.generation += 1
            self.frames_sent = 0
            self.started = time.monotonic()


# ------------------------------------------------------------------------------------------------ handshake

def _hs_key(psk: bytes) -> bytes:
    return _hkdf(psk, None, b"phonebridge-v4|handshake")


def _seal_hs(psk: bytes, who: str, body: dict, _nonce: bytes | None = None) -> bytes:
    nonce = _nonce or os.urandom(crypto.NONCE_LEN)   # _nonce: fixed value for interoperability test vectors only
    return nonce + AESGCM(_hs_key(psk)).encrypt(nonce, json.dumps(body, separators=(",", ":")).encode(), _HS_AAD[who])


def _open_hs(psk: bytes, who: str, blob: bytes) -> dict:
    try:
        body = json.loads(AESGCM(_hs_key(psk)).decrypt(blob[:crypto.NONCE_LEN], blob[crypto.NONCE_LEN:], _HS_AAD[who]))
    except (InvalidTag, ValueError) as exc:
        raise HandshakeError("handshake did not authenticate") from exc
    if not isinstance(body, dict):
        raise HandshakeError("malformed handshake")
    return body


def _string_fields(body: dict, *names: str) -> list[str]:
    """The named fields of an (authenticated but possibly hostile) handshake message, all required to be strings."""
    values = [body.get(n) for n in names]
    if not all(isinstance(v, str) for v in values):
        raise HandshakeError("malformed handshake")
    return values


def _client_msg(fp: str, tls_fp: str, eph: str, nonce: str) -> bytes:
    return "|".join(["phonebridge-v4|client", fp, tls_fp.lower(), eph, nonce]).encode()


def _server_msg(client_body: dict, fp: str, tls_fp: str, eph: str, nonce: str) -> bytes:
    return "|".join(["phonebridge-v4|server", fp, tls_fp.lower(), client_body["eph"], client_body["n"], eph, nonce]).encode()


def _master(shared: bytes, psk: bytes, server_signed: bytes) -> bytes:
    return _hkdf(shared, psk, b"phonebridge-v4|master|" + hashlib.sha256(server_signed).digest())


@dataclass
class ClientState:
    psk: bytes
    fp: str
    tls_fp: str
    eph: X25519PrivateKey
    body: dict


def client_hello(psk: bytes, fp: str, tls_fp: str, identity: Identity, *, _eph: X25519PrivateKey | None = None,
                _n: bytes | None = None, _aes_nonce: bytes | None = None) -> tuple[bytes, ClientState]:
    """Phone side, step 1: the first frame of a control connection. (Underscore arguments: fixed values for test vectors.)"""
    eph = _eph or X25519PrivateKey.generate()
    eph_b64 = _b64(eph.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw))
    nonce = _b64(_n or os.urandom(16))
    body = {"v": VERSION, "eph": eph_b64, "n": nonce, "sig": _b64(identity.sign(_client_msg(fp, tls_fp, eph_b64, nonce)))}
    return _seal_hs(psk, "client", body, _aes_nonce), ClientState(psk, fp, tls_fp, eph, body)


def server_hello(psk: bytes, fp: str, tls_fp: str, phone_identity_pub_b64, identity: Identity,
                 frame: bytes, *, _eph: X25519PrivateKey | None = None, _n: bytes | None = None,
                 _aes_nonce: bytes | None = None) -> tuple[bytes, Session]:
    """PC side: verifies the phone's hello (pairing key + its Ed25519 signature) and answers; returns (reply, session).
    `phone_identity_pub_b64` is the phone's identity key, or a list of acceptable keys (current + previous, during an
    identity rotation); `session.peer_identity` says which one signed."""
    body = _open_hs(psk, "client", frame)
    if body.get("v") != VERSION:
        raise HandshakeError(f"unsupported protocol version {body.get('v')!r} (need {VERSION}); update the phone app")
    candidates = [k for k in ([phone_identity_pub_b64] if isinstance(phone_identity_pub_b64, str) else phone_identity_pub_b64 or []) if k]
    if not candidates:
        raise HandshakeError("this pairing has no device identity key; pair the phone again")
    _string_fields(body, "eph", "n", "sig")
    try:
        sig = _unb64(body["sig"])
        _unb64(body["eph"]), _unb64(body["n"])
    except (ValueError, TypeError) as exc:
        raise HandshakeError("malformed handshake") from exc
    signer = next((k for k in candidates if verify_signature(k, sig, _client_msg(fp, tls_fp, body["eph"], body["n"]))), None)
    if signer is None:
        raise HandshakeError("invalid device signature")
    eph = _eph or X25519PrivateKey.generate()
    eph_b64 = _b64(eph.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw))
    nonce = _b64(_n or os.urandom(16))
    signed = _server_msg(body, fp, tls_fp, eph_b64, nonce)
    reply = {"v": VERSION, "eph": eph_b64, "n": nonce, "sig": _b64(identity.sign(signed))}
    try:
        shared = eph.exchange(X25519PublicKey.from_public_bytes(_unb64(body["eph"])))
    except (ValueError, TypeError) as exc:
        raise HandshakeError("bad ephemeral key") from exc
    session = Session("pc", _master(shared, psk, signed), session_id=hashlib.sha256(signed).hexdigest()[:16])
    session.peer_identity = signer
    return _seal_hs(psk, "server", reply, _aes_nonce), session


def client_finish(state: ClientState, frame: bytes, pc_identity_pub_b64: str) -> Session:
    """Phone side, step 2: verifies the PC (pairing key + its Ed25519 signature) and derives the session."""
    body = _open_hs(state.psk, "server", frame)
    if body.get("v") != VERSION:
        raise HandshakeError("unsupported protocol version")
    _string_fields(body, "eph", "n", "sig")
    try:
        sig = _unb64(body["sig"])
        signed = _server_msg(state.body, state.fp, state.tls_fp, body["eph"], body["n"])
        peer = X25519PublicKey.from_public_bytes(_unb64(body["eph"]))
    except (KeyError, ValueError, TypeError) as exc:
        raise HandshakeError("malformed handshake") from exc
    if not verify_signature(pc_identity_pub_b64, sig, signed):
        raise HandshakeError("the PC's signature is invalid (wrong or impersonated PC)")
    shared = state.eph.exchange(peer)
    return Session("phone", _master(shared, state.psk, signed), session_id=hashlib.sha256(signed).hexdigest()[:16])
