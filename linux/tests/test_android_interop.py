#!/usr/bin/env python3
"""Cross-language check of protocol v4: the REAL Android SecureSession.kt/Crypto.kt (compiled on the JVM with Gradle's
bundled Kotlin compiler) play the phone against the real Python secure_session.py. Catches any byte-level mismatch
in signatures, key schedule, AAD, rekeying and data/UDP key derivation. Skips if java/kotlinc/bcprov aren't found.
Run: `python3 tests/test_android_interop.py`"""
import glob
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from phonebridge import crypto, secure_session as S  # noqa: E402
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat  # noqa: E402

ANDROID = HERE.parents[1] / "android/app/src/main/java/dev/phonebridge"
PASS = FAIL = 0


def check(label, cond):
    global PASS, FAIL
    PASS, FAIL = (PASS + 1, FAIL) if cond else (PASS, FAIL + 1)
    print(f"  {'ok ' if cond else 'FAIL'} {label}")


def read_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("closed")
        buf += chunk
    return buf


def recv_frame(sock):
    return read_exact(sock, crypto.unpack_frame_len(read_exact(sock, 4)))


def main():
    java = subprocess.run(["which", "java"], capture_output=True, text=True).stdout.strip()
    libs = sorted(l for l in glob.glob(os.path.expanduser("~/.gradle/wrapper/dists/gradle-*/*/gradle-*/lib"))
                  if glob.glob(f"{l}/kotlin-stdlib-2*.jar"))
    bc = sorted(glob.glob(os.path.expanduser("~/.gradle/caches/modules-2/files-2.1/org.bouncycastle/bcprov-jdk18on/*/*/bcprov-jdk18on-*.jar")))
    js = sorted(glob.glob(os.path.expanduser("~/.gradle/caches/modules-2/files-2.1/org.json/json/*/*/json-*.jar")))
    bc = [j for j in bc if "sources" not in j]
    if not (java and libs and bc and js):
        print("SKIP: need java, Gradle's Kotlin compiler, bcprov and org.json jars in ~/.gradle")
        return 0
    lib = libs[0]
    jars = lambda *pats: [j for p in pats for j in sorted(glob.glob(f"{lib}/{p}"))]
    kcp = ":".join(jars("kotlin-*.jar", "trove4j*.jar", "annotations*.jar", "kotlinx-coroutines-core-jvm*.jar"))
    stdlib = jars("kotlin-stdlib-2*.jar")[0]
    run_cp = f"{stdlib}:{bc[-1]}:{js[-1]}"

    with tempfile.TemporaryDirectory() as tmp:
        r = subprocess.run([java, "-cp", kcp, "org.jetbrains.kotlin.cli.jvm.K2JVMCompiler", "-no-stdlib", "-no-reflect",
                            "-cp", run_cp, "-d", f"{tmp}/out", str(ANDROID / "SecureSession.kt"), str(ANDROID / "Crypto.kt"),
                            str(HERE / "v4/Harness.kt"), str(HERE / "v4/android/util/Base64.kt")], capture_output=True, text=True)
        if r.returncode:
            print(r.stdout, r.stderr)
            check("Android sources compile on the JVM", False)
            return 1
        check("Android SecureSession.kt + Crypto.kt compile", True)

        psk = os.urandom(32)
        fp, tls = "abcd1234abcd1234", "AB" * 32
        phone_id, pc_id = S.Identity.generate(), S.Identity.generate()
        priv_raw = phone_id._private.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
        srv = socket.socket(); srv.bind(("127.0.0.1", 0)); srv.listen(1)
        port = srv.getsockname()[1]
        res = {}

        def pc_side():
            try:
                conn, _ = srv.accept(); conn.settimeout(20)
                reply, session = S.server_hello(psk, fp, tls, phone_id.public_b64, pc_id, recv_frame(conn))
                conn.sendall(crypto.pack_frame(reply))
                res["session"] = session
                res["ping"] = json.loads(session.cipher.decrypt(recv_frame(conn)))
                conn.sendall(crypto.pack_frame(session.cipher.encrypt(b'{"type":"pong"}')))
                res["file"] = session.data_cipher("file|abc").decrypt(recv_frame(conn))
                res["video"] = session.data_cipher("video").decrypt(recv_frame(conn))
                conn.sendall(crypto.pack_frame(session.data_cipher("file|abc").encrypt(b"file-data-from-pc")))
                eph = session.start_rekey()
                conn.sendall(crypto.pack_frame(session.cipher.encrypt(json.dumps(
                    {"ch": "control", "type": "rekey", "seq": 9, "payload": {"eph": eph}}).encode())))
                ack = json.loads(session.cipher.decrypt(recv_frame(conn)))
                res["ack"] = ack
                session.finish_rekey(ack["payload"]["eph"])
                conn.sendall(crypto.pack_frame(session.cipher.encrypt(b'{"ch":"control","type":"rekey.done","seq":10,"payload":{}}')))
                session.commit_send()
                res["ping2"] = json.loads(session.cipher.decrypt(recv_frame(conn)))
                conn.sendall(crypto.pack_frame(session.cipher.encrypt(b'{"type":"pong-after-rekey"}')))
            except Exception as exc:  # noqa: BLE001
                res["error"] = repr(exc)

        new_pc, new_phone = S.Identity.generate(), S.Identity.generate()
        pc_sig = S.sign_rotation(pc_id, fp, new_pc.public_b64)
        t = threading.Thread(target=pc_side); t.start()
        p = subprocess.run([java, "-cp", f"{tmp}/out:{run_cp}", "HarnessKt", str(port), psk.hex(), fp, tls, priv_raw.hex(),
                            pc_id.public_b64, new_pc.public_b64, pc_sig, new_phone.public_b64],
                           capture_output=True, text=True, timeout=60)
        t.join(30)
        print(p.stdout.strip() or "", p.stderr.strip()[-600:] if p.returncode else "")
        out = {ln.split(" ", 1)[0]: ln.split(" ", 1)[1] for ln in p.stdout.splitlines() if " " in ln}
        check("Kotlin phone completed the whole exchange", p.returncode == 0 and "error" not in res)
        if "error" in res:
            print("  python side:", res["error"])
        s = res.get("session")
        if s:
            print("1. handshake")
            check("Kotlin verified the PC's Ed25519 signature and Python verified the phone's (else the exchange aborts)", p.returncode == 0 and "error" not in res)
            check("both derive the same session id", out.get("SESSION") == s.session_id)
            check("both derive the same exporter secret", out.get("EXPORTER") == s.exporter.hex())
            check("both derive the same UDP video key", out.get("UDP") == s.udp_key.hex())
            print("2. frames")
            check("Python decrypted Kotlin's control frame", res.get("ping", {}).get("type") == "ping")
            check("Kotlin decrypted Python's control frame", "pong" in out.get("GOT", ""))
            check("file data key matches across languages", res.get("file") == b"file-data-from-android"
                  and out.get("DATA") == "file-data-from-pc")
            check("video data key matches across languages", res.get("video") == b"video-from-android")
            print("3. rotation")
            check("rekey acknowledged by Kotlin and finished by Python", res.get("ack", {}).get("type") == "rekey.ack")
            check("both are on generation 1", out.get("GEN") == "1" and s.generation == 1)
            check("traffic flows both ways on the rotated keys", res.get("ping2", {}).get("type") == "ping"
                  and "pong-after-rekey" in out.get("AFTER", ""))
            check("the exporter (data/UDP keys) is unchanged by rotation", out.get("EXPORTER2") == s.exporter.hex())
            print("4. identity rotation")
            check("Kotlin verifies a PC rotation notice signed by Python's old key", out.get("ROT_VERIFY") == "true")
            check("Kotlin rejects the same notice checked against the wrong key", out.get("ROT_FORGED") == "false")
            check("Python verifies a phone rotation signed by Kotlin's old key",
                  S.verify_rotation(phone_id.public_b64, fp, new_phone.public_b64, out.get("ROT_SIGN", "")))
            check("…and rejects it for a different new key",
                  not S.verify_rotation(phone_id.public_b64, fp, new_pc.public_b64, out.get("ROT_SIGN", "")))
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
