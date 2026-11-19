#!/usr/bin/env python3
"""PrivateBlockchain: anchoring the ledger head on a private permissioned chain (HTTP service and file backends).
Run: `python3 tests/test_anchor.py`"""
import base64
import json
import shutil
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from phonebridge.security import anchor as A, anchor_server as AS, keystore, ledger as L  # noqa: E402

PASS = FAIL = 0


def check(label, cond):
    global PASS, FAIL
    PASS, FAIL = (PASS + 1, FAIL) if cond else (PASS, FAIL + 1)
    print(f"  {'ok ' if cond else 'FAIL'} {label}")


def new_ledger(tmp, name):
    return L.LocalLedger(tmp / f"{name}.jsonl", keystore.FileStore(tmp / f"ks-{name}"))


def main():
    tmp = Path(tempfile.mkdtemp())
    try:
        led = new_ledger(tmp, "a")
        service = AS.AnchorService(tmp / "anchors.jsonl", {led.public_key_b64})
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), AS.make_handler(service))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{httpd.server_address[1]}"

        print("1. anchoring through the HTTP service")
        chain = A.PrivateBlockchain(led, A.HttpAnchor(url, service_pub=service.public_key_b64), every_records=1000)
        for i in range(6):
            chain.append(L.AUTH_OK, "devA", n=i)
        check("before any anchor, verify says so but passes", chain.verify().ok and chain.verify().error == "no anchors yet")
        block = chain.anchor_now()
        check("the service returns a block chained from genesis and signed by the service",
              block["height"] == 0 and block["prev"] == A.GENESIS and A._verify(service.public_key_b64, block["service_sig"], block["hash"].encode()))
        for i in range(4):
            chain.append(L.FILE_TRANSFER, "devA", n=i)
        chain.anchor_now()
        check("a second anchor extends the chain", len(service.blocks()) == 2 and service.blocks()[1]["prev"] == service.blocks()[0]["hash"])
        check("the ledger verifies against its anchors", chain.verify().ok and chain.verify().error is None)
        stored = (tmp / "anchors.jsonl").read_text()
        check("the service stores only hashes, counts and public keys (no event names, devices or metadata)",
              "devA" not in stored and "auth.ok" not in stored and "file.transfer" not in stored)

        print("2. attacks the local ledger alone cannot detect")
        original = (tmp / "a.jsonl").read_text()
        replacement = new_ledger(tmp, "b")                    # a different, perfectly valid chain...
        for i in range(10):
            replacement.append(L.AUTH_OK, "attacker", n=i)
        (tmp / "a.jsonl").write_text((tmp / "b.jsonl").read_text())
        # ...signed by a key the verifier doesn't trust, so first show that even with the SAME key a rewrite is caught:
        (tmp / "a.jsonl").write_text(original)
        led_rewrite = L.LocalLedger(tmp / "rewrite.jsonl", keystore.FileStore(tmp / "ks-a"))   # attacker holds the ledger key
        for i in range(10):
            led_rewrite.append(L.AUTH_OK, "innocent-looking", n=i)
        (tmp / "a.jsonl").write_text((tmp / "rewrite.jsonl").read_text())
        fresh = A.PrivateBlockchain(L.LocalLedger(tmp / "a.jsonl", keystore.FileStore(tmp / "ks-a")), A.HttpAnchor(url, service.public_key_b64))
        check("the rewritten log is a valid chain on its own", fresh.local.verify().ok)
        r = fresh.verify()
        check("…but the anchors prove history was replaced", not r.ok and "no longer contains anchored record" in r.error)
        (tmp / "a.jsonl").write_text("\n".join(original.splitlines()[:5]) + "\n")
        r = A.PrivateBlockchain(L.LocalLedger(tmp / "a.jsonl", keystore.FileStore(tmp / "ks-a")), A.HttpAnchor(url, service.public_key_b64)).verify()
        check("truncating the log below an anchored record is caught", not r.ok)
        (tmp / "a.jsonl").write_text(original)

        print("3. the anchor service")
        check("an unregistered ledger key is refused (permissioned)", _status(url, {"pub": new_ledger(tmp, "evil").public_key_b64, "head": "0" * 64, "count": 1, "sig": "AAAA"}) == 403)
        good = led.records()[-1]["hash"]
        check("a registered key with a bad signature is refused", _status(url, {"pub": led.public_key_b64, "head": good, "count": 1, "sig": base64.b64encode(b"x" * 64).decode()}) == 400)
        check("malformed requests are refused, not crashed on", _status(url, {"pub": 5}) == 400)
        blocks = service.blocks()
        tampered = [dict(b) for b in blocks]; tampered[0]["entry"] = {**tampered[0]["entry"], "count": 3}
        class Fake(A.AnchorBackend):
            service_pub = service.public_key_b64
            def blocks(self, pub): return tampered
        ok = A.PrivateBlockchain(L.LocalLedger(tmp / "a.jsonl", keystore.FileStore(tmp / "ks-a")), Fake())
        check("an edited anchor block is detected (hash chain)", not ok.verify().ok)
        class Wrong(A.AnchorBackend):
            service_pub = A.base64.b64encode(b"\x01" * 32).decode()
            def blocks(self, pub): return blocks
        check("blocks not signed by the pinned service key are rejected", not A.PrivateBlockchain(L.LocalLedger(tmp / "a.jsonl", keystore.FileStore(tmp / "ks-a")), Wrong()).verify().ok)

        print("4. the ledger keeps working when the service is down")
        httpd.shutdown(); httpd.server_close()
        offline = A.PrivateBlockchain(L.LocalLedger(tmp / "a.jsonl", keystore.FileStore(tmp / "ks-a")), A.HttpAnchor(url, service.public_key_b64, timeout=1), every_records=1000)
        rec = offline.append(L.AUTH_OK, "devA", n=99)
        check("appending still works", rec["type"] == L.AUTH_OK)
        check("a failed anchor is remembered and never raises", offline.anchor_now() is None and offline.pending == 1 and offline.last_error)
        v = offline.verify()
        check("verify reports the service is unreachable but the local ledger verified", v.ok and "unreachable" in (v.error or ""))

        print("5. the file backend (second medium)")
        fa = A.FileAnchor(tmp / "usb" / "anchors.jsonl")
        l3 = new_ledger(tmp, "c")
        pb = A.PrivateBlockchain(l3, fa, every_records=1000)
        for i in range(3): pb.append(L.AUTH_OK, "d", n=i)
        pb.anchor_now(); l3.append(L.AUTH_OK, "d", n=9); pb.anchor_now()
        check("a file anchor chains blocks and verifies", len(fa.blocks(l3.public_key_b64)) == 2 and pb.verify().ok and pb.verify().error is None)
        pb2 = A.PrivateBlockchain(L.LocalLedger(tmp / "c.jsonl", keystore.FileStore(tmp / "ks-c")), fa)
        lines = (tmp / "c.jsonl").read_text().splitlines()
        (tmp / "c.jsonl").write_text("\n".join(lines[:2] + lines[3:]) + "\n")
        check("a deleted record is caught by the local chain", not pb2.verify().ok)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


def _status(url, body):
    import urllib.error
    import urllib.request
    req = urllib.request.Request(url + "/anchor", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=5)  # nosec B310
        return 200
    except urllib.error.HTTPError as e:
        return e.code


if __name__ == "__main__":
    sys.exit(main())
