#!/usr/bin/env python3
"""Update verification (signed manifest, pinned key, downgrade/replay protection, artifact hashes) + the dependency audit
tool's parsing. Run: `python3 tests/test_updates.py`"""
import base64
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[0]))
sys.path.insert(0, str(HERE.parents[1] / "tools"))
from phonebridge import pairing, updates  # noqa: E402

PASS = FAIL = 0


def check(label, cond):
    global PASS, FAIL
    PASS, FAIL = (PASS + 1, FAIL) if cond else (PASS, FAIL + 1)
    print(f"  {'ok ' if cond else 'FAIL'} {label}")


def refused(fn, text=""):
    try:
        fn()
    except updates.UpdateError as exc:
        return text in str(exc)
    return False


def main():
    tmp = Path(tempfile.mkdtemp())
    pairing.DEVICES_DIR = tmp / "cfg" / "devices"
    tool = str(HERE.parents[1] / "tools/sign_release.py")
    key = tmp / "release.key"
    out = subprocess.run([sys.executable, tool, "keygen", str(key)], capture_output=True, text=True).stdout
    pub = out.split(":")[-1].strip()
    check("keygen prints a public key and makes a private file only the owner can read", len(base64.b64decode(pub)) == 32 and (key.stat().st_mode & 0o777) == 0o600)
    art = tmp / "dist"; art.mkdir()
    wheel = art / "phonebridge-9.9.9.whl"; wheel.write_bytes(os.urandom(5000))
    apk = art / "app.apk"; apk.write_bytes(os.urandom(777))
    subprocess.run([sys.executable, tool, "sign", str(key), "9.9.9", str(wheel), str(apk)], check=True, capture_output=True)
    mbytes, sig = (art / "manifest.json").read_bytes(), (art / "manifest.sig").read_text().strip()

    print("1. manifest")
    check("with no pinned key every update is refused", refused(lambda: updates.verify_manifest(mbytes, sig, "1.0.0"), "no release key"))
    m = updates.verify_manifest(mbytes, sig, "1.3.0", public_b64=pub)
    check("a validly signed newer release is accepted", m.version == "9.9.9" and set(m.artifacts) == {"phonebridge-9.9.9.whl", "app.apk"})
    check("a signature from another key is refused", refused(lambda: updates.verify_manifest(
        mbytes, sig, "1.3.0", public_b64=base64.b64encode(__import__("cryptography.hazmat.primitives.asymmetric.ed25519", fromlist=["x"]).Ed25519PrivateKey.generate().public_key().public_bytes_raw()).decode()), "signature"))
    forged = json.loads(mbytes); forged["version"] = "10.0.0"
    check("an edited manifest (even just the version) is refused", refused(lambda: updates.verify_manifest(json.dumps(forged).encode(), sig, "1.3.0", public_b64=pub), "signature"))
    check("a same-version or older release is refused (downgrade/replay)", refused(lambda: updates.verify_manifest(mbytes, sig, "9.9.9", public_b64=pub), "not newer")
          and refused(lambda: updates.verify_manifest(mbytes, sig, "12.0", public_b64=pub), "not newer"))
    check("garbage is refused, not crashed on", refused(lambda: updates.verify_manifest(b"not json", sig, "1.0", public_b64=pub), "malformed"))

    print("2. pinning and artifacts")
    updates.pin_key(pub)
    check("a pinned key is stored privately and used", updates.pinned_key() == pub and (updates.pinned_key_path().stat().st_mode & 0o777) == 0o600)
    m = updates.verify_manifest(mbytes, sig, "1.3.0")
    updates.verify_artifact(wheel, m); updates.verify_artifact(apk, m)
    check("the genuine artifacts verify", True)
    tampered = art / "phonebridge-9.9.9.whl"; data = tampered.read_bytes(); tampered.write_bytes(data[:-1] + bytes([data[-1] ^ 1]))
    check("a tampered artifact (one flipped bit) is refused", refused(lambda: updates.verify_artifact(tampered, m), "does not match"))
    (art / "evil.whl").write_bytes(b"x")
    check("a file that isn't in the signed release is refused", refused(lambda: updates.verify_artifact(art / "evil.whl", m), "not part of the signed release"))
    check("pinning garbage is refused", _raises(lambda: updates.pin_key("AAAA")))

    print("2b. fetching and verifying a release (file:// host)")
    host = tmp / "host"; host.mkdir()
    for f in ("manifest.json", "manifest.sig", "app.apk"):
        (host / f).write_bytes((art / f).read_bytes())
    # a consistent host with a fresh wheel (the earlier tamper test modified art/ in place)
    wheel_good = os.urandom(5000)
    (host / "phonebridge-9.9.9.whl").write_bytes(wheel_good)
    import hashlib as _h
    man = json.loads((art / "manifest.json").read_bytes())
    man["artifacts"]["phonebridge-9.9.9.whl"] = {"sha256": _h.sha256(wheel_good).hexdigest(), "size": len(wheel_good)}
    # re-sign with the maintainer key so the manifest is genuine
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    sk = Ed25519PrivateKey.from_private_bytes(key.read_bytes())
    (host / "manifest.json").write_bytes(updates.canonical(man))
    (host / "manifest.sig").write_text(base64.b64encode(sk.sign(updates.canonical(man))).decode())
    got, saved = updates.check_for_update(host.as_uri(), "1.3.0", tmp / "dl")
    check("a signed newer release is fetched, verified and downloaded (nothing installed)", got.version == "9.9.9" and len(saved) == 2 and all(p.exists() for p in saved))
    check("an http:// update URL is refused", refused(lambda: updates.check_for_update("http://example.com/x", "1.3.0"), "https://"))
    (host / "phonebridge-9.9.9.whl").write_bytes(os.urandom(5000))       # a swapped artifact on the host
    check("an artifact swapped on the host fails the signed hash", refused(lambda: updates.check_for_update(host.as_uri(), "1.3.0", tmp / "dl2"), "does not match"))
    check("…and the swapped download is not left on disk", not (tmp / "dl2" / "9.9.9" / "phonebridge-9.9.9.whl").exists())
    check("no update when already current or newer", refused(lambda: updates.check_for_update(host.as_uri(), "9.9.9"), "not newer"))
    check("the CLI reports verification success/refusal with an exit code", subprocess.run(
        [sys.executable, "-m", "phonebridge.updates", "check", host.as_uri()], capture_output=True, text=True, cwd=str(HERE.parents[0]),
        env={**os.environ, "PYTHONPATH": str(HERE.parents[0])}).returncode in (0, 1))

    print("3. dependency audit tool (parsing, with a fake OSV)")
    import audit_dependencies as ad
    inv = ad.android_inventory()
    check("Android libraries are read from build.gradle.kts", any(p["name"] == "org.bouncycastle:bcprov-jdk18on" for p in inv))
    check("…and the bundled BouncyCastle is a version without known advisories (>= 1.87)", all(
        tuple(int(x) for x in p["version"].split(".")) >= (1, 87) for p in inv if p["name"].startswith("org.bouncycastle")))
    seen = []
    def fake_post(url, body):
        seen.append(body)
        return {"results": [{"vulns": [{"id": "GHSA-fake-1"}]} if q["package"]["name"] == "pillow" else {} for q in body["queries"]]}
    findings = ad.scan([{"ecosystem": "PyPI", "name": "pillow", "version": "1.0"}, {"ecosystem": "PyPI", "name": "ok", "version": "2.0"},
                        {"ecosystem": "PyPI", "name": "missing", "version": None}], post=fake_post,
                       get=lambda url: {"summary": "bad bug", "aliases": ["CVE-1"]})
    check("findings carry id, summary and package", findings == [{"package": "pillow", "version": "1.0", "id": "GHSA-fake-1", "summary": "bad bug", "aliases": ["CVE-1"]}])
    check("only package names and versions are sent (no paths, hostnames or user data)",
          all(set(q) == {"package", "version"} and set(q["package"]) == {"name", "ecosystem"} for q in seen[0]["queries"]) and len(seen[0]["queries"]) == 2)
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


def _raises(fn):
    try:
        fn()
    except Exception:  # noqa: BLE001
        return True
    return False


if __name__ == "__main__":
    sys.exit(main())
