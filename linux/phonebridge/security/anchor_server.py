"""Reference anchor service for PrivateBlockchain: a tiny permissioned chain you run on ANOTHER machine.

  python -m phonebridge.security.anchor_server --store anchors.jsonl --allow <ledger-public-key-b64> [--port 8480]
  (the service's own signing key is created next to the store; its public key is printed — pin it in the client)

Only registered ledger keys may submit (permissioned). A submission is verified (Ed25519 signature by the ledger key over
head+count), appended as a block hash-chained to the previous one, and answered with the block plus the service's signature
of the block hash. Stores hashes and public keys only. Bind to a private interface; there is no authentication beyond the
signature check, so do not expose it to the internet.
"""
from __future__ import annotations

import argparse
import base64
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat

from .anchor import GENESIS, _verify, block_hash, entry_message


class AnchorService:
    def __init__(self, store: Path, allowed: set[str]) -> None:
        self.store, self.allowed = Path(store), set(allowed)
        self._lock = threading.Lock()
        keyfile = self.store.with_suffix(".key")
        if keyfile.exists():
            self._key = Ed25519PrivateKey.from_private_bytes(keyfile.read_bytes())
        else:
            self._key = Ed25519PrivateKey.generate()
            keyfile.write_bytes(self._key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption()))
            keyfile.chmod(0o600)
        self.public_key_b64 = base64.b64encode(self._key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()

    def blocks(self) -> list[dict]:
        return [json.loads(l) for l in self.store.read_text().splitlines() if l.strip()] if self.store.exists() else []

    def submit(self, pub: str, head: str, count: int, sig: str) -> dict:
        if pub not in self.allowed:
            raise PermissionError("this ledger key is not registered with the anchor service")
        if not (isinstance(head, str) and len(head) == 64 and isinstance(count, int) and count > 0 and _verify(pub, sig, entry_message(pub, head, count))):
            raise ValueError("bad anchor submission")
        with self._lock:
            blocks = self.blocks()
            block = {"height": len(blocks), "ts": round(time.time(), 3), "prev": blocks[-1]["hash"] if blocks else GENESIS,
                     "entry": {"pub": pub, "head": head, "count": count, "sig": sig}}
            block["hash"] = block_hash(block)
            block["service_sig"] = base64.b64encode(self._key.sign(block["hash"].encode())).decode()
            with self.store.open("a") as fh:
                fh.write(json.dumps(block, sort_keys=True) + "\n")
            return block


def make_handler(service: AnchorService):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, obj: dict) -> None:
            data = json.dumps(obj).encode()
            self.send_response(code); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(data)))
            self.end_headers(); self.wfile.write(data)

        def do_GET(self):  # noqa: N802
            url = urlparse(self.path)
            if url.path == "/blocks":
                self._send(200, {"blocks": service.blocks(), "service_pub": service.public_key_b64})
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self):  # noqa: N802
            if urlparse(self.path).path != "/anchor":
                return self._send(404, {"error": "not found"})
            try:
                n = int(self.headers.get("Content-Length", "0"))
                if not 0 < n < 4096:
                    raise ValueError("bad length")
                body = json.loads(self.rfile.read(n))
                self._send(200, service.submit(body["pub"], body["head"], body["count"], body["sig"]))
            except PermissionError as exc:
                self._send(403, {"error": str(exc)})
            except (ValueError, KeyError, TypeError) as exc:
                self._send(400, {"error": str(exc)[:80]})

        def log_message(self, *a):  # quiet
            pass

    return Handler


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", required=True)
    ap.add_argument("--allow", action="append", required=True, help="a ledger public key (base64) allowed to anchor; repeatable")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8480)
    a = ap.parse_args()
    service = AnchorService(Path(a.store), set(a.allow))
    print("anchor service public key (pin this):", service.public_key_b64)
    ThreadingHTTPServer((a.host, a.port), make_handler(service)).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
