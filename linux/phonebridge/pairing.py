"""Pairing state machine — transport-agnostic (phase 2 wires this to real BT RFCOMM).

PC side: PendingPairing. Phone side equivalent lives in the Android app
(crypto/Pairing.kt) and must produce byte-identical JSON for the same inputs.
"""
from __future__ import annotations

import base64
import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import crypto, spake2

PAIRING_TTL_SECONDS = 60   # a QR code / PIN is valid for one minute; the dialog shows a fresh one automatically
PROTOCOL_VERSION = 4   # v4: the PIN is verified with SPAKE2 (a PAKE) instead of an offline-testable HMAC proof
# Wrong PINs allowed per pairing dialog; after that the PIN is burned and a new dialog is
# needed, so a 6-digit PIN can't be guessed online while the dialog sits open.
MAX_FAILED_ATTEMPTS = 3
DEVICES_DIR = Path.home() / ".config" / "phonebridge" / "devices"


def pc_control_ttl_seconds() -> int:
    """How long "Allow PC control" lasts once switched on: PHONEBRIDGE_PC_CONTROL_MINUTES (default 60, 1..1440)."""
    try:
        minutes = int(os.environ.get("PHONEBRIDGE_PC_CONTROL_MINUTES", "60"))
    except ValueError:
        minutes = 60
    return max(1, min(1440, minutes)) * 60

PLATFORMS = ("android", "ios")
IDENTITY_GRACE_SECONDS = 7 * 24 * 3600   # how long a retired phone identity key keeps working after a rotation
# Sidebar sections an iPhone can't back (iOS gives apps no access to SMS, calls, other apps'
# notifications, the photo library over the bridge yet, screen capture, or camera streaming).
IOS_HIDDEN_SECTIONS = frozenset({"notifications", "messages", "calls", "photos", "mirror", "sync", "camera", "apps"})

SERVICE_UUID = "b43c1e00-35ee-4b8a-9e3f-6c3f2a1b9d10"


class PairingError(Exception):
    pass


_identities: dict = {}
_written_keys: dict = {}   # (config dir, fingerprint) -> session key last written to the keystore by this process


def pc_identity():
    """This PC's long-term Ed25519 identity (private key in the keystore, created on first use)."""
    from .secure_session import Identity

    store = _keystore()
    key = str(DEVICES_DIR.parent)
    if key not in _identities:
        _identities[key] = Identity.load_or_create(store)
    return _identities[key]


PREV_IDENTITY_KEY = "identity-ed25519-prev"


def pc_identity_prev():
    """The PC's previous identity key (kept only until every paired phone has acknowledged the new one), or None."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from .secure_session import Identity

    raw = _keystore().get(PREV_IDENTITY_KEY)
    return Identity(Ed25519PrivateKey.from_private_bytes(raw)) if raw else None


def pc_identity_for(device):
    """The PC identity this phone currently trusts: the current key, or the previous one while the phone hasn't yet
    acknowledged a rotation."""
    cur = pc_identity()
    if device.pc_pub_acked in (None, cur.public_b64):
        return cur
    prev = pc_identity_prev()
    return prev if prev is not None and prev.public_b64 == device.pc_pub_acked else cur


def rotate_pc_identity():
    """Replaces this PC's identity key. The old one is kept (in the keystore) to authenticate phones that haven't heard
    about the change yet; it is deleted once all of them acknowledged. Returns (old_identity, new_identity)."""
    from .secure_session import Identity

    old = pc_identity()
    store = _keystore()
    old.save(store, PREV_IDENTITY_KEY)
    new = Identity.generate()
    new.save(store)
    _identities[str(DEVICES_DIR.parent)] = new
    return old, new


def finish_rotation_if_done() -> bool:
    """Deletes the previous PC identity key once every paired phone acknowledged the current one."""
    cur = pc_identity().public_b64
    if all(d.pc_pub_acked == cur for d in PairedDevice.list_all()):
        _keystore().delete(PREV_IDENTITY_KEY)
        return True
    return False


def _keystore():
    from .security import keystore

    return keystore.for_config_dir(DEVICES_DIR.parent)


@dataclass
class PairedDevice:
    fingerprint: str
    name: str
    session_key: bytes
    bt_addr: str | None = None
    last_lan_ip: str | None = None
    last_lan_port: int | None = None
    cert_fingerprint: str | None = None
    # Shell / PC files / remote input / screen view from the phone. Off until the user
    # turns it on for this phone on the PC (device card toggle).
    allow_pc_control: bool = False
    # Remote control is time-limited: it counts as on only until this Unix time (set when the user turns it on).
    # A stored "on" with no deadline (older versions) is treated as expired - fail closed.
    pc_control_until: float | None = None
    # "android" or "ios" (sent in the phone's pairing hello). iPhones lack SMS, calls,
    # notification access, screen capture and file browsing, so the UI hides those tabs.
    platform: str = "android"
    # The phone's Ed25519 identity public key (base64), learned during pairing. Public data, so it lives in the JSON.
    identity_pub: str | None = None
    # During a phone identity rotation the old key stays acceptable until the phone has signed in with the new one.
    identity_pub_prev: str | None = None
    identity_prev_at: float | None = None   # when the previous identity key was retired; it is only honoured for 7 days
    # The PC identity public key this phone last acknowledged; if it differs from the PC's current one the PC still
    # signs handshakes with the previous key and tells the phone about the new one in-session (see transport).
    pc_pub_acked: str | None = None
    # feature -> "allow" | "deny" | "ask" (security/permissions.py); missing features use the defaults.
    permissions: dict = field(default_factory=dict)

    def pc_control_active(self, now: float | None = None) -> bool:
        """True only while remote control is switched on AND its time limit has not run out."""
        import time

        return bool(self.allow_pc_control and self.pc_control_until is not None
                    and (time.time() if now is None else now) < self.pc_control_until)

    def save(self) -> Path:
        DEVICES_DIR.mkdir(parents=True, exist_ok=True)
        DEVICES_DIR.chmod(0o700)
        DEVICES_DIR.parent.chmod(0o700)
        path = DEVICES_DIR / f"{self.fingerprint}.json"
        tmp = path.with_name(path.name + f".{os.getpid()}.{threading.get_ident()}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)   # 0600 from creation, not chmod afterwards
        with os.fdopen(fd, "w") as fh:
            fh.write(
            json.dumps(
                {
                    "fingerprint": self.fingerprint,
                    "name": self.name,
                    "bt_addr": self.bt_addr,
                    "last_lan_ip": self.last_lan_ip,
                    "last_lan_port": self.last_lan_port,
                    "cert_fingerprint": self.cert_fingerprint,
                    "allow_pc_control": self.allow_pc_control,
                    "pc_control_until": self.pc_control_until,
                    "platform": self.platform,
                    "identity_pub": self.identity_pub,
                    "identity_pub_prev": self.identity_pub_prev,
                    "identity_prev_at": self.identity_prev_at,
                    "pc_pub_acked": self.pc_pub_acked,
                    "permissions": self.permissions,
                },
                indent=2,
            ))
        tmp.chmod(0o600)
        os.replace(tmp, path)   # atomic: a reader never sees a half-written file
        # The key goes to the keystore, never into the JSON file. Skip the (slow, D-Bus) write when it is unchanged.
        cache_key = (str(DEVICES_DIR.parent), self.fingerprint)
        if _written_keys.get(cache_key) != self.session_key:
            _keystore().set(f"session-key:{self.fingerprint}", self.session_key)
            _written_keys[cache_key] = self.session_key
        return path

    _update_lock = threading.RLock()

    @staticmethod
    def mutate(fingerprint: str, fn) -> "PairedDevice | None":
        """Runs fn(device) on the CURRENT stored record under the update lock, then saves it (see update())."""
        with PairedDevice._update_lock:
            device = PairedDevice.load(fingerprint)
            if device is None:
                return None
            fn(device)
            device.save()
            return device

    @staticmethod
    def update(fingerprint: str, **changes) -> "PairedDevice | None":
        """Applies `changes` to the CURRENT stored record and saves it, under a lock. Use this instead of
        load()-modify-save() from background code: that pattern silently undoes a change (a permission the user just
        toggled) made between the load and the save. Returns the updated device, or None if it no longer exists."""
        with PairedDevice._update_lock:
            device = PairedDevice.load(fingerprint)
            if device is None:
                return None
            for key, value in changes.items():
                if not hasattr(device, key):
                    raise AttributeError(key)
                setattr(device, key, value)
            device.save()
            return device

    @staticmethod
    def _from_obj(obj: dict) -> "PairedDevice":
        key = _keystore().get(f"session-key:{obj['fingerprint']}")
        if key is None and "session_key" in obj:   # pairing made before keystore support: migrate it
            key = bytes.fromhex(obj["session_key"])
            _keystore().set(f"session-key:{obj['fingerprint']}", key)
            migrated = {k: v for k, v in obj.items() if k != "session_key"}
            path = DEVICES_DIR / f"{obj['fingerprint']}.json"
            path.write_text(json.dumps(migrated, indent=2))
            path.chmod(0o600)
        if key is None:
            raise PairingError(f"no session key stored for {obj['fingerprint']}")
        return PairedDevice(
            fingerprint=obj["fingerprint"],
            name=obj["name"],
            session_key=key,
            identity_pub=obj.get("identity_pub"),
            identity_pub_prev=obj.get("identity_pub_prev"),
            identity_prev_at=obj.get("identity_prev_at"),
            pc_pub_acked=obj.get("pc_pub_acked"),
            permissions={k: v for k, v in (obj.get("permissions") or {}).items() if v in ("allow", "deny", "ask")},
            bt_addr=obj.get("bt_addr"),
            last_lan_ip=obj.get("last_lan_ip"),
            last_lan_port=obj.get("last_lan_port"),
            cert_fingerprint=obj.get("cert_fingerprint"),
            allow_pc_control=bool(obj.get("allow_pc_control", False)),
            pc_control_until=obj["pc_control_until"] if isinstance(obj.get("pc_control_until"), (int, float)) else None,
            platform=obj.get("platform") if obj.get("platform") in PLATFORMS else "android",
        )

    @staticmethod
    def load(fingerprint: str) -> "PairedDevice | None":
        if not valid_fingerprint(fingerprint):   # never build a file path from unvalidated (network-supplied) text
            return None
        path = DEVICES_DIR / f"{fingerprint}.json"
        if not path.exists():
            return None
        try:
            return PairedDevice._from_obj(json.loads(path.read_text()))
        except (KeyError, ValueError, TypeError, PairingError):   # a corrupt or foreign file is "no such device", not a crash
            return None

    @staticmethod
    def list_all() -> list["PairedDevice"]:
        if not DEVICES_DIR.exists():
            return []
        out = []
        for path in DEVICES_DIR.glob("*.json"):
            try:
                out.append(PairedDevice._from_obj(json.loads(path.read_text())))
            except (KeyError, ValueError, TypeError, PairingError, OSError):
                continue   # a stray or corrupt file must not break discovery, the UI or pairing
        return out


_FP_RE = re.compile(r"[0-9a-f]{1,32}")


def valid_fingerprint(fp: object) -> bool:
    """A device fingerprint is lowercase hex only. Anything else (dots, slashes, spaces) is attacker text, not a device."""
    return isinstance(fp, str) and _FP_RE.fullmatch(fp) is not None


def acceptable_phone_keys(device: "PairedDevice") -> list[str]:
    """The phone identity keys that may sign a handshake: the current one, plus the previous one for a limited grace period
    after a rotation (long enough for an offline phone to catch up; an attacker holding a key that was rotated away because
    it leaked can't use it forever)."""
    import time

    keys = [device.identity_pub]
    if device.identity_pub_prev and device.identity_prev_at is not None \
            and time.time() - device.identity_prev_at < IDENTITY_GRACE_SECONDS:
        keys.append(device.identity_pub_prev)
    return [k for k in keys if k]


def forget_device(fingerprint: str) -> bool:
    """Deletes a pairing and its session key. Returns False if there was no such pairing."""
    if not valid_fingerprint(fingerprint):
        return False
    path = DEVICES_DIR / f"{fingerprint}.json"
    existed = path.exists()
    path.unlink(missing_ok=True)
    _keystore().delete(f"session-key:{fingerprint}")
    _written_keys.pop((str(DEVICES_DIR.parent), fingerprint), None)
    return existed


def remove_stale_pairings(keep: PairedDevice) -> list[str]:
    """Re-pairing the same phone creates a new key/fingerprint; the old record can never
    connect again, so drop records with the same phone name. Returns removed fingerprints."""
    removed = []
    for other in PairedDevice.list_all():
        if other.fingerprint != keep.fingerprint and other.name == keep.name:
            forget_device(other.fingerprint)
            removed.append(other.fingerprint)
    return removed


@dataclass
class PakeSession:
    """State of one phone's SPAKE2 exchange on the PC side."""
    phone_pub_b64: str
    share: bytes          # the PC's SPAKE2 share (Y)
    keys: "spake2.Keys"


class PendingPairing:
    """PC side of a single in-flight pairing attempt (one QR code's worth)."""

    def __init__(self, bt_addr: str | None, pc_name: str, lan_ips: list[str] | None = None,
                 pair_port: int | None = None) -> None:
        self.keypair = crypto.Keypair.generate()
        self.token = crypto.gen_token()
        self.bt_addr = bt_addr
        self.pc_name = pc_name
        self.lan_ips = lan_ips or []
        self.pair_port = pair_port
        self.created_at = time.monotonic()
        self.failed_attempts = 0
        self.used = False   # single-use: set by the first correct PIN proof (see verify_phone_confirmation)
        self._lock = threading.Lock()  # Wi-Fi and Bluetooth listeners share one pending pairing

    def is_expired(self) -> bool:
        return (time.monotonic() - self.created_at) > PAIRING_TTL_SECONDS

    def is_locked_out(self) -> bool:
        return self.failed_attempts >= MAX_FAILED_ATTEMPTS

    def is_spent(self) -> bool:
        """True once this code can never pair anyone again (expired, used, or guessed at too often)."""
        return self.used or self.is_expired() or self.is_locked_out()

    def seconds_left(self) -> int:
        return max(0, int(PAIRING_TTL_SECONDS - (time.monotonic() - self.created_at)))

    def qr_payload(self) -> dict:
        payload = {
            "v": 1,
            "bt_addr": self.bt_addr,
            "token": self.token,
            "pub": self.keypair.public_b64(),
        }
        if self.lan_ips and self.pair_port:
            # Wi-Fi pairing (PROTOCOL.md section 1, "Wi-Fi first"): the phone tries these
            # addresses before falling back to Bluetooth.
            payload["lan"] = self.lan_ips
            payload["pport"] = self.pair_port
        return payload

    def offer_json(self, nonce: str) -> dict:
        """Sent first on the pairing link, right after accept() — see PROTOCOL.md section 1.
        Never contains the PIN: the phone proves it knows the PIN through SPAKE2 (begin / verify_phone_confirmation)."""
        return {"v": PROTOCOL_VERSION, "pub": self.keypair.public_b64(), "name": self.pc_name, "nonce": nonce,
                "pake": "spake2-p256"}

    def _context(self, phone_pub_b64: str, nonce: str) -> bytes:
        # Binds both X25519 keys and the PC's nonce into the PAKE transcript, so the keys that were exchanged (and the
        # QR-pinned PC key) are authenticated by the PIN too.
        return "|".join(["phonebridge-pair-v4", self.keypair.public_b64(), phone_pub_b64, nonce]).encode("utf-8")

    def begin(self, phone_hello: dict, nonce: str) -> "PakeSession":
        """Step 1: validates the phone's hello and answers its SPAKE2 share. Nothing here reveals whether the PIN is right;
        the phone's confirmation (verify_phone_confirmation) does, and counts a failed attempt."""
        with self._lock:
            self._check_usable()
            if phone_hello.get("v") != PROTOCOL_VERSION:
                raise PairingError("the phone app is too old for this PC — update PhoneBridge on the phone")
            phone_pub_b64 = phone_hello.get("pub")
            try:
                crypto.public_from_b64(str(phone_pub_b64))
                phone_share = base64.b64decode(str(phone_hello.get("spake")), validate=True)
                spake2.decode(phone_share)
            except Exception as exc:  # noqa: BLE001 - malformed key or point from the network
                raise PairingError("invalid phone key") from exc
            engine = spake2.Spake2("B", self.token, self._context(str(phone_pub_b64), nonce))
            keys = engine.finish(phone_share)
            return PakeSession(str(phone_pub_b64), engine.share, keys)

    def _check_usable(self) -> None:
        if self.is_locked_out():
            raise PairingError("too many wrong PINs — close the Pair dialog and open it again")
        if self.is_expired():
            raise PairingError("pairing PIN expired, generate a new QR code")
        if self.used:
            raise PairingError("this pairing code was already used — scan the new one")

    def verify_phone_confirmation(self, session: "PakeSession", confirm_hex: object) -> str:
        """Step 2: the phone's SPAKE2 confirmation MAC. A wrong one is a wrong PIN: it is counted toward
        MAX_FAILED_ATTEMPTS. The first correct one consumes the code (single use), whether or not the user then allows the
        phone. Returns the phone's X25519 public key (base64)."""
        with self._lock:
            self._check_usable()
            try:
                given = bytes.fromhex(confirm_hex) if isinstance(confirm_hex, str) else None
            except ValueError:
                given = None
            if not spake2.macs_equal(session.keys.mac_phone, given):
                self.failed_attempts += 1
                raise PairingError("wrong PIN")
            self.used = True
            return session.phone_pub_b64

    def pc_confirmation(self, session: "PakeSession") -> str:
        """The PC's SPAKE2 confirmation MAC (hex): the phone checks it before trusting anything this PC says."""
        return session.keys.mac_pc.hex()

    def confirmation_code(self, phone_pub_b64: str) -> str:
        return crypto.confirmation_code(self.keypair.public_b64(), phone_pub_b64)

    def complete(self, session: "PakeSession", phone_name: str, platform: str = "android") -> PairedDevice:
        """Derives and saves the pairing for a phone that passed verify_phone_confirmation() and that the user allowed
        on the PC. The pairing key mixes the X25519 secret with the SPAKE2 key, so it depends on the PIN exchange."""
        phone_pub_b64 = session.phone_pub_b64
        phone_pub = crypto.public_from_b64(phone_pub_b64)
        session_key = crypto.derive_session_key_v4(self.keypair.private, phone_pub, session.keys.ke)
        fp = crypto.fingerprint(self.keypair.public_b64(), phone_pub_b64)

        device = PairedDevice(fingerprint=fp, name=phone_name, session_key=session_key,
                              platform=platform if platform in PLATFORMS else "android",
                              pc_pub_acked=pc_identity().public_b64)   # lan_info hands the phone this key right away
        device.save()
        remove_stale_pairings(device)
        return device

    def lan_info_message(self, lan_ip: str, lan_port: int, cert_fingerprint: str) -> dict:
        # id_pub: this PC's Ed25519 identity. It travels inside the PIN-authenticated, encrypted frame, so a
        # man in the middle who doesn't know the PIN cannot substitute his own.
        return {"lan_ip": lan_ip, "lan_port": lan_port, "cert_fingerprint": cert_fingerprint,
                "id_pub": pc_identity().public_b64}
