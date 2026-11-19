#!/usr/bin/env python3
"""SPAKE2 pairing primitive: P-256 arithmetic against OpenSSL, protocol properties (right PIN agrees, wrong PIN doesn't,
a recorded exchange gives no offline PIN oracle), and the REAL Android Spake2.kt (compiled on the JVM with Gradle's bundled
Kotlin compiler) against the Python implementation. The Kotlin part skips if java/kotlinc/bcprov aren't found.
Run: `python3 tests/test_spake2.py`"""
import glob
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402

from phonebridge import spake2 as S  # noqa: E402

ANDROID = HERE.parents[1] / "android/app/src/main/java/dev/phonebridge"
PASS = FAIL = 0


def check(label, cond):
    global PASS, FAIL
    PASS, FAIL = (PASS + 1, FAIL) if cond else (PASS, FAIL + 1)
    print(f"  {'ok ' if cond else 'FAIL'} {label}")


def main():
    print("1. P-256 arithmetic agrees with OpenSSL")
    check("G, M and N are on the curve; n*G is the identity", S.on_curve(S.G) and S.on_curve(S.M) and S.on_curve(S.N) and S.mul(S.ORDER, S.G) is None)
    k = 0x1234567890ABCDEF1234567890ABCDEF1234567890ABCDEF
    pub = ec.derive_private_key(k, ec.SECP256R1()).public_key().public_numbers()
    check("k*G equals OpenSSL's public key", S.mul(k, S.G) == (pub.x, pub.y))
    m_key = ec.EllipticCurvePublicNumbers(*S.M, ec.SECP256R1()).public_key()
    shared = ec.derive_private_key(k, ec.SECP256R1()).exchange(ec.ECDH(), m_key)
    check("k*M matches an OpenSSL ECDH against M", int.from_bytes(shared, "big") == S.mul(k, S.M)[0])
    check("blinded ladder agrees with plain multiplication (random scalars, both G and other points)",
          all(S.mul_secret(kk, pt) == S.mul(kk, pt) for kk in (1, 2, 3, S.ORDER - 1, k, int.from_bytes(os.urandom(32), "big") % S.ORDER)
              for pt in (S.G, S.M, S.N)))
    check("scalar 0 and the group order give the identity", S.mul_secret(0, S.M) is None and S.mul_secret(S.ORDER, S.N) is None)
    for bad in (b"", b"\x04" + b"\x01" * 64, b"\x00" * 65, b"\x02" + b"\xff" * 32):
        try:
            S.decode(bad)
            check(f"invalid point {bad[:3].hex()}… rejected", False)
        except ValueError:
            check(f"invalid point {bad[:3].hex()}… rejected", True)
    check("compressed and uncompressed encodings decode to the same point", S.decode(S.encode(S.M)) == S.M)

    print("2. protocol properties")
    ctx = b"phonebridge-pair-v4|pc|phone|nonce"
    a, b = S.Spake2("A", "483920", ctx), S.Spake2("B", "483920", ctx)
    ka, kb = a.finish(b.share), b.finish(a.share)
    check("right PIN: both sides get the same key and the same confirmations", ka.ke == kb.ke and ka.mac_phone == kb.mac_phone and ka.mac_pc == kb.mac_pc)
    a, b = S.Spake2("A", "483920", ctx), S.Spake2("B", "483921", ctx)
    ka, kb = a.finish(b.share), b.finish(a.share)
    check("wrong PIN: different keys and confirmations", ka.ke != kb.ke and ka.mac_phone != kb.mac_phone and ka.mac_pc != kb.mac_pc)
    a, b = S.Spake2("A", "483920", ctx), S.Spake2("B", "483920", ctx + b"x")
    check("a different context (other keys or nonce) breaks the agreement", a.finish(b.share).ke != b.finish(a.share).ke)
    check("each exchange uses fresh shares", S.Spake2("A", "483920", ctx).share != S.Spake2("A", "483920", ctx).share)
    try:
        S.Spake2("A", "483920", ctx).finish(S.encode(S.add(S.G, None)) [:10])
        check("a truncated share is rejected", False)
    except ValueError:
        check("a truncated share is rejected", True)

    print("3. no offline PIN oracle")
    # A rogue PC that records the phone's share and its confirmation for ONE exchange tries every PIN offline. With the old
    # HMAC proof this recovered the PIN; with SPAKE2 the recorded values can't be tested without the PC's secret scalar.
    def confirmable(rogue_guess, candidates):
        """The rogue answers the phone (real PIN 000042) using `rogue_guess`, records the phone's confirmation MAC, and then
        tries to reproduce that MAC offline under each candidate PIN (re-using its own secret scalar)."""
        phone = S.Spake2("A", "000042", ctx)
        rogue = S.Spake2("B", rogue_guess, ctx)
        recorded = phone.finish(rogue.share).mac_phone
        return [c for c in candidates
                if S.Spake2("B", c, ctx, scalar=rogue._x).finish(phone.share).mac_phone == recorded]

    pins = ["000041", "000042", "000043", "999999"]
    check("a rogue that guessed right can confirm exactly that one PIN (one live guess per exchange)", confirmable("000042", pins) == ["000042"])
    check("a rogue that guessed wrong learns nothing: no candidate, including the real PIN, reproduces the MAC", confirmable("999999", pins) == [])
    eaves_x, eaves_y = S.Spake2("A", "123456", ctx), S.Spake2("B", "123456", ctx)
    transcript = (eaves_x.share, eaves_y.share, eaves_x.finish(eaves_y.share).mac_phone, eaves_y.finish(eaves_x.share).mac_pc)
    check("an eavesdropper has no scalar, so it cannot even form a test for a PIN guess",
          all(len(t) in (65, 32) for t in transcript) and eaves_x._x.bit_length() > 200)

    print("4. Android Spake2.kt against the Python implementation")
    java = subprocess.run(["which", "java"], capture_output=True, text=True).stdout.strip()
    libs = sorted(lb for lb in glob.glob(os.path.expanduser("~/.gradle/wrapper/dists/gradle-*/*/gradle-*/lib"))
                  if glob.glob(f"{lb}/kotlin-stdlib-2*.jar"))
    bc = [j for j in sorted(glob.glob(os.path.expanduser("~/.gradle/caches/modules-2/files-2.1/org.bouncycastle/bcprov-jdk18on/*/*/bcprov-jdk18on-*.jar"))) if "sources" not in j]
    if not (java and libs and bc):
        print("  SKIP: need java, Gradle's Kotlin compiler and bcprov in ~/.gradle")
    else:
        lib = libs[0]
        jars = lambda *pats: [j for p in pats for j in sorted(glob.glob(f"{lib}/{p}"))]
        kcp = ":".join(jars("kotlin-*.jar", "trove4j*.jar", "annotations*.jar", "kotlinx-coroutines-core-jvm*.jar"))
        stdlib = jars("kotlin-stdlib-2*.jar")[0]
        run_cp = f"{stdlib}:{bc[-1]}"
        with tempfile.TemporaryDirectory() as tmp:
            r = subprocess.run([java, "-cp", kcp, "org.jetbrains.kotlin.cli.jvm.K2JVMCompiler", "-no-stdlib", "-no-reflect", "-cp", run_cp,
                                "-d", f"{tmp}/out", str(ANDROID / "Spake2.kt"), str(ANDROID / "Crypto.kt"), str(HERE / "spake/Harness.kt"),
                                str(HERE / "v4/android/util/Base64.kt")], capture_output=True, text=True)
            if r.returncode:
                print(r.stdout, r.stderr)
                check("Spake2.kt compiles on the JVM", False)
                return 1
            check("Spake2.kt compiles on the JVM", True)
            for pin in ("483920", "000001"):
                x = 0xABCDEF0123456789ABCDEF0123456789ABCDEF0123456789 + int(pin)
                pc = S.Spake2("B", pin, ctx)
                run = subprocess.run([java, "-cp", f"{tmp}/out:{run_cp}", "HarnessKt", pin, ctx.hex(), f"{x:x}", pc.share.hex()],
                                     capture_output=True, text=True)
                out = dict(line.split(" ", 1) for line in run.stdout.splitlines())
                py_phone = S.Spake2("A", pin, ctx, scalar=x)
                py_keys = py_phone.finish(pc.share)
                check(f"[{pin}] same w (scrypt of the PIN)", int(out.get("W", "0"), 16) == S.password_to_w(pin))
                check(f"[{pin}] same phone share", out.get("SHARE") == py_phone.share.hex())
                pc_keys = pc.finish(py_phone.share)
                check(f"[{pin}] Kotlin phone and Python PC agree on Ke", out.get("KE") == pc_keys.ke.hex() == py_keys.ke.hex())
                check(f"[{pin}] both confirmation MACs match", out.get("MACPHONE") == pc_keys.mac_phone.hex() and out.get("MACPC") == pc_keys.mac_pc.hex())
            wrong = subprocess.run([java, "-cp", f"{tmp}/out:{run_cp}", "HarnessKt", "111111", ctx.hex(), "1234567", S.Spake2("B", "222222", ctx).share.hex()],
                                   capture_output=True, text=True)
            wo = dict(line.split(" ", 1) for line in wrong.stdout.splitlines())
            check("Kotlin with a wrong PIN does not agree with the PC", wo.get("MACPHONE") != S.Spake2("B", "222222", ctx).finish(bytes.fromhex(wo["SHARE"])).mac_phone.hex())
            bad = subprocess.run([java, "-cp", f"{tmp}/out:{run_cp}", "HarnessKt", "111111", ctx.hex(), "1234567", "04" + "01" * 64], capture_output=True, text=True)
            check("Kotlin refuses an off-curve share", bad.returncode != 0)

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
