"""Tamper-evident security ledger.

SecurityLedger          — interface every ledger implements
LocalLedger             — append-only JSONL, each record hash-chained to the previous one and signed with an
                          Ed25519 key held in the keystore. Works fully offline; this is the one that is built.
LedgerVerifier          — re-checks hashes, chain links and signatures of a ledger file
PrivateBlockchain       — OPTIONAL (security/anchor.py): anchors this ledger's head hash on a private, permissioned chain
                          you host, so even a full replacement of the log is detected. Off by default; the app runs on
                          LocalLedger alone.

Records hold only security metadata (device fingerprints, event names, counts) — never messages, files, clipboard
text, passwords or keys.
"""
from __future__ import annotations

import base64
import hashlib
import json
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat

from .keystore import KeyStore

GENESIS = "0" * 64
SIGNING_KEY_NAME = "ledger-signing-key"

# Event types (kept as plain strings so ledgers stay readable without this module).
PAIRED, REVOKED, AUTH_OK, AUTH_FAIL, AUTH_THROTTLED = "device.paired", "device.revoked", "auth.ok", "auth.failed", "auth.throttled"
SESSION_START, SESSION_END, DISCONNECT_ALL = "session.start", "session.end", "session.disconnect_all"
REKEY = "session.rekey"
IDENTITY_ROTATED = "identity.rotated"
PERMISSION, FILE_TRANSFER, SECURITY_ERROR, DENIED = "permission.changed", "file.transfer", "security.error", "access.denied"


@dataclass
class VerifyResult:
    ok: bool
    records: int
    error: str | None = None
    bad_index: int | None = None


class SecurityLedger:
    def append(self, event_type: str, device: str | None = None, **metadata: Any) -> dict:
        raise NotImplementedError

    def records(self) -> list[dict]:
        raise NotImplementedError

    def verify(self) -> VerifyResult:
        raise NotImplementedError


def PrivateBlockchain(*args, **kwargs):  # noqa: N802
    """The optional anchored ledger lives in security/anchor.py; this name is kept for the original interface."""
    from .anchor import PrivateBlockchain as _impl

    return _impl(*args, **kwargs)


def _canonical(obj: dict) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()


def _record_hash(body: dict) -> str:
    return hashlib.sha256(_canonical(body)).hexdigest()


class LocalLedger(SecurityLedger):
    def __init__(self, path: Path, keystore: KeyStore) -> None:
        self._path = path
        self._lock = threading.Lock()
        raw = keystore.get(SIGNING_KEY_NAME)
        if raw is None:
            key = Ed25519PrivateKey.generate()
            keystore.set(SIGNING_KEY_NAME, key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption()))
        else:
            key = Ed25519PrivateKey.from_private_bytes(raw)
        self._key = key
        self.public_key_b64 = base64.b64encode(key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._prev = self._last_hash()

    def _last_hash(self) -> str:
        last = GENESIS
        if self._path.exists():
            for line in self._path.read_text().splitlines():
                if line.strip():
                    try:
                        last = json.loads(line)["hash"]
                    except (ValueError, KeyError):
                        break
        return last

    def append(self, event_type: str, device: str | None = None, **metadata: Any) -> dict:
        with self._lock:
            body = {"id": uuid.uuid4().hex, "ts": round(time.time(), 3), "device": device, "type": event_type,
                    "meta": metadata, "prev": self._prev, "pub": self.public_key_b64}
            digest = _record_hash(body)
            record = {**body, "hash": digest, "sig": base64.b64encode(self._key.sign(digest.encode())).decode()}
            with self._path.open("a") as fh:
                fh.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
            self._path.chmod(0o600)
            self._prev = digest
            return record

    def records(self) -> list[dict]:
        if not self._path.exists():
            return []
        out = []
        for line in self._path.read_text().splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                break
        return out

    def verify(self) -> VerifyResult:
        # One verifier for the life of the ledger: it remembers which (hash, signature) pairs already passed, so
        # re-verifying a long log only re-checks hashes and chain links (cheap) and verifies new signatures.
        if getattr(self, "_verifier", None) is None:
            self._verifier = LedgerVerifier(self.public_key_b64)
        return self._verifier.verify_file(self._path)


class LedgerVerifier:
    """`trusted_pub_b64` is the ledger signing key the verifier trusts (a record can't vouch for its own key)."""

    def __init__(self, trusted_pub_b64: str) -> None:
        self._pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(trusted_pub_b64))
        self._pub_b64 = trusted_pub_b64
        self._verified: set[tuple[str, str]] = set()   # (hash, signature) pairs whose Ed25519 signature already checked out

    def verify_file(self, path: Path) -> VerifyResult:
        if not path.exists():
            return VerifyResult(True, 0)
        prev, n = GENESIS, 0
        for i, line in enumerate(path.read_text().splitlines()):
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
                body = {k: rec[k] for k in ("id", "ts", "device", "type", "meta", "prev", "pub")}
                if rec["prev"] != prev:
                    return VerifyResult(False, n, "chain broken: record does not follow the previous one (deleted/reordered)", i)
                if _record_hash(body) != rec["hash"]:
                    return VerifyResult(False, n, "record contents were modified", i)
                if rec["pub"] != self._pub_b64:
                    return VerifyResult(False, n, "record signed by an untrusted key", i)
                if (rec["hash"], rec["sig"]) not in self._verified:   # a changed hash OR signature is a cache miss
                    self._pub.verify(base64.b64decode(rec["sig"]), rec["hash"].encode())
                    self._verified.add((rec["hash"], rec["sig"]))
            except InvalidSignature:
                return VerifyResult(False, n, "invalid signature", i)
            except (ValueError, KeyError, TypeError):
                return VerifyResult(False, n, "malformed record", i)
            prev, n = rec["hash"], n + 1
        return VerifyResult(True, n)
