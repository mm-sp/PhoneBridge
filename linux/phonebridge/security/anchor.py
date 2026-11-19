"""Optional private, permissioned chain that ANCHORS the local ledger (what `PrivateBlockchain` is).

The local ledger already detects edits, deletions and reordering. What it cannot detect alone is someone who controls the
device replacing the whole file with a new, internally valid chain. Anchoring fixes that: from time to time the ledger's
head hash (+ record count), signed by the ledger key, is sent to an *anchor service you host* (a second PC, NAS, Pi). The
service keeps its own hash-chained blocks and returns a receipt signed by the service. Later, `verify()` checks that every
anchored head still appears at the same position in the local ledger.

Only hashes, counts and public keys travel: never messages, files, names, keys or anything else. No tokens, no mining, no
public blockchain, and everything keeps working if the service is unreachable (anchors queue and retry).

  LocalLedger  ->  PrivateBlockchain(local, backend)  ->  backend = HttpAnchor("http://host:8480") | FileAnchor(path)
  Reference service: `python -m phonebridge.security.anchor_server --store anchors.jsonl --allow <ledger public key>`
"""
from __future__ import annotations

import base64
import hashlib
import json
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .ledger import LocalLedger, SecurityLedger, VerifyResult

GENESIS = "0" * 64


def entry_message(ledger_pub: str, head: str, count: int) -> bytes:
    return f"phonebridge-anchor-v1|{ledger_pub}|{head}|{count}".encode()


def block_hash(block: dict) -> str:
    body = {k: block[k] for k in ("height", "ts", "prev", "entry")}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _verify(pub_b64: str, sig_b64: str, message: bytes) -> bool:
    try:
        Ed25519PublicKey.from_public_bytes(base64.b64decode(pub_b64)).verify(base64.b64decode(sig_b64), message)
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


class AnchorBackend:
    def submit(self, ledger_pub: str, head: str, count: int, signature: str) -> dict:
        raise NotImplementedError

    def blocks(self, ledger_pub: str) -> list[dict]:
        raise NotImplementedError

    service_pub: str | None = None


class HttpAnchor(AnchorBackend):
    """Talks to anchor_server.py. `service_pub` pins the service's key (receipts from any other key are rejected)."""

    def __init__(self, url: str, service_pub: str | None = None, timeout: float = 5.0) -> None:
        self.url, self.service_pub, self._timeout = url.rstrip("/"), service_pub, timeout

    def _call(self, path: str, body: dict | None = None) -> Any:
        req = urllib.request.Request(self.url + path, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self._timeout) as resp:  # nosec B310 - operator-configured http(s) URL
            return json.load(resp)

    def submit(self, ledger_pub: str, head: str, count: int, signature: str) -> dict:
        return self._call("/anchor", {"pub": ledger_pub, "head": head, "count": count, "sig": signature})

    def blocks(self, ledger_pub: str) -> list[dict]:
        return self._call("/blocks?pub=" + ledger_pub.replace("+", "%2B").replace("/", "%2F").replace("=", "%3D"))["blocks"]


class FileAnchor(AnchorBackend):
    """Anchors to a file on another medium (USB stick, NAS mount): the same chained blocks, no service key."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def _read(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text().splitlines() if line.strip()]

    def submit(self, ledger_pub: str, head: str, count: int, signature: str) -> dict:
        with self._lock:
            blocks = self._read()
            prev = blocks[-1]["hash"] if blocks else GENESIS
            block = {"height": len(blocks), "ts": round(time.time(), 3), "prev": prev,
                     "entry": {"pub": ledger_pub, "head": head, "count": count, "sig": signature}}
            block["hash"] = block_hash(block)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a") as fh:
                fh.write(json.dumps(block, sort_keys=True) + "\n")
            return block

    def blocks(self, ledger_pub: str) -> list[dict]:
        return self._read()


class PrivateBlockchain(SecurityLedger):
    """A LocalLedger plus anchoring. Behaves exactly like the local ledger when no backend answers."""

    def __init__(self, local: LocalLedger, backend: AnchorBackend, every_records: int = 10, every_seconds: float = 300.0) -> None:
        self.local, self.backend = local, backend
        self._every_records, self._every_seconds = every_records, every_seconds
        self._since = 0
        self._last = time.monotonic()
        self._lock = threading.Lock()
        self.pending = 0       # anchors that failed and will be retried with the next one
        self.last_error: str | None = None

    # -- SecurityLedger
    def append(self, event_type: str, device: str | None = None, **metadata: Any) -> dict:
        record = self.local.append(event_type, device, **metadata)
        with self._lock:
            self._since += 1
            due = self._since >= self._every_records or time.monotonic() - self._last >= self._every_seconds
        if due:
            threading.Thread(target=self.anchor_now, daemon=True).start()
        return record

    def records(self) -> list[dict]:
        return self.local.records()

    @property
    def public_key_b64(self) -> str:
        return self.local.public_key_b64

    def anchor_now(self) -> dict | None:
        """Sends the current head to the backend; a failure is remembered, never raised."""
        recs = self.local.records()
        if not recs:
            return None
        head, count = recs[-1]["hash"], len(recs)
        try:
            sig = base64.b64encode(self.local._key.sign(entry_message(self.local.public_key_b64, head, count))).decode()  # noqa: SLF001
            block = self.backend.submit(self.local.public_key_b64, head, count, sig)
        except Exception as exc:  # noqa: BLE001 - offline, refused, anything: the ledger must keep working
            with self._lock:
                self.pending += 1
                self.last_error = str(exc)[:120]
            return None
        with self._lock:
            self._since, self._last, self.pending, self.last_error = 0, time.monotonic(), 0, None
        return block

    def verify(self) -> VerifyResult:
        """Local chain check, then: the anchor chain is intact and every anchored head is still in the ledger at its count."""
        local = self.local.verify()
        if not local.ok:
            return local
        try:
            blocks = self.backend.blocks(self.local.public_key_b64)
        except Exception as exc:  # noqa: BLE001
            return VerifyResult(True, local.records, f"anchor service unreachable ({str(exc)[:60]}); local ledger verified only")
        prev = GENESIS
        recs = self.local.records()
        mine = 0
        for i, b in enumerate(blocks):
            if b.get("height") != i or b.get("prev") != prev or block_hash(b) != b.get("hash"):
                return VerifyResult(False, local.records, f"anchor chain broken at block {i}", i)
            prev = b["hash"]
            e = b["entry"]
            if e["pub"] != self.local.public_key_b64:
                continue
            if not _verify(e["pub"], e["sig"], entry_message(e["pub"], e["head"], e["count"])):
                return VerifyResult(False, local.records, f"anchor {i}: bad ledger signature", i)
            if self.backend.service_pub:
                sig = b.get("service_sig")
                if not isinstance(sig, str) or not _verify(self.backend.service_pub, sig, b["hash"].encode()):
                    return VerifyResult(False, local.records, f"anchor {i}: not signed by the pinned anchor service", i)
            if e["count"] > len(recs) or recs[e["count"] - 1]["hash"] != e["head"]:
                return VerifyResult(False, local.records, f"the ledger no longer contains anchored record #{e['count']} (replaced or truncated)", i)
            mine += 1
        return VerifyResult(True, local.records, None if mine else "no anchors yet")
