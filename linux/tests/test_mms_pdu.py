#!/usr/bin/env python3
"""Compiles android/.../MmsPdu.kt with the Kotlin compiler bundled in the Gradle install, runs it
(tests/mms/Harness.kt) and decodes the resulting M-Send.req with an independent parser written
from OMA-TS-MMS-ENC-V1.2 / WAP-230. Skips if no Kotlin compiler or java is available.
Run: `python3 tests/test_mms_pdu.py`"""
import glob
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE.parents[1] / "android/app/src/main/java/dev/phonebridge/MmsPdu.kt"
PASS = FAIL = 0


def check(label, cond):
    global PASS, FAIL
    PASS, FAIL = (PASS + 1, FAIL) if cond else (PASS, FAIL + 1)
    print(f"  {'ok ' if cond else 'FAIL'} {label}")


def uintvar(b, i):
    v = 0
    while True:
        c = b[i]; i += 1
        v = (v << 7) | (c & 0x7F)
        if not c & 0x80:
            return v, i


def cstr(b, i):
    j = b.index(0, i)
    return b[i:j].decode(), j + 1


def decode(b):
    h, i = {}, 0
    h["to"] = []
    while True:
        k = b[i]; i += 1
        if k == 0x8C: h["type"] = b[i]; i += 1
        elif k == 0x98: h["tid"], i = cstr(b, i)
        elif k == 0x8D: h["version"] = b[i]; i += 1
        elif k == 0x89:
            n = b[i]; i += 1; h["from"] = b[i:i + n]; i += n
        elif k == 0x97:
            a, i = cstr(b, i); h["to"].append(a)
        elif k in (0x8A, 0x8F, 0x86, 0x90): h[k] = b[i]; i += 1
        elif k == 0x84:
            h["ctype"] = b[i]; i += 1
            break
        else:
            raise ValueError(f"unknown header {k:#x} at {i-1}")
    n, i = uintvar(b, i)
    parts = []
    for _ in range(n):
        hl, i = uintvar(b, i)
        dl, i = uintvar(b, i)
        hdr, data = b[i:i + hl], b[i + hl:i + hl + dl]
        i += hl + dl
        if hdr[0] == 3:                      # value-length 3: well-known type + charset param
            ct, extra, j = hdr[1], hdr[2:4], 4
        elif hdr[0] & 0x80:
            ct, extra, j = hdr[0], b"", 1
        else:
            ct, j = None, 0
            ct, j = cstr(hdr, 0); extra = b""
        loc = None
        if j < len(hdr) and hdr[j] == 0x8E:
            loc, _ = cstr(hdr, j + 1)
        parts.append((ct, extra, loc, data))
    return h, parts, i


def main():
    java = subprocess.run(["which", "java"], capture_output=True, text=True).stdout.strip()
    libs = sorted(l for l in glob.glob(os.path.expanduser("~/.gradle/wrapper/dists/gradle-*/*/gradle-*/lib"))
                  if glob.glob(f"{l}/kotlin-stdlib-2*.jar"))
    if not java or not libs:
        print("SKIP: java / Kotlin compiler (Gradle lib dir) not found")
        return 0
    lib = libs[0]
    jars = lambda *pats: [j for p in pats for j in sorted(glob.glob(f"{lib}/{p}"))]
    cp = ":".join(jars("kotlin-*.jar", "trove4j*.jar", "annotations*.jar", "kotlinx-coroutines-core-jvm*.jar"))
    stdlib = jars("kotlin-stdlib-2*.jar")[0]
    with tempfile.TemporaryDirectory() as tmp:
        r = subprocess.run([java, "-cp", cp, "org.jetbrains.kotlin.cli.jvm.K2JVMCompiler", "-no-stdlib", "-no-reflect",
                            "-cp", stdlib, "-d", f"{tmp}/out", str(SRC), str(HERE / "mms/Harness.kt")],
                           capture_output=True, text=True)
        if r.returncode:
            print(r.stdout, r.stderr)
            check("Kotlin compiles", False)
            return 1
        pdu = f"{tmp}/x.pdu"
        r = subprocess.run([java, "-cp", f"{tmp}/out:{stdlib}", "HarnessKt", pdu], capture_output=True, text=True)
        check("encoder runs", r.returncode == 0)
        b = Path(pdu).read_bytes()

    h, parts, end = decode(b)
    print("1. headers")
    check("message type m-send-req", h["type"] == 0x80)
    check("transaction id", h["tid"] == "T1234")
    check("MMS version 1.2", h["version"] == 0x92)
    check("From is insert-address-token", h["from"] == b"\x81")
    check("recipients normalised to PLMN addresses", h["to"] == ["+15550102030/TYPE=PLMN", "5550109999/TYPE=PLMN"])
    check("content type multipart/mixed is last header", h["ctype"] == 0xA3)
    check("no delivery/read reports requested", h[0x86] == 0x81 and h[0x90] == 0x81)
    print("2. body")
    check("three parts, nothing left over", len(parts) == 3 and end == len(b))
    ct, extra, loc, data = parts[0]
    check("text part: text/plain, utf-8 charset, name, bytes", (ct, extra, loc, data) == (0x83, b"\x81\xea", "text.txt", "héllo MMS".encode()))
    ct, extra, loc, data = parts[1]
    check("image part: image/jpeg, 300 bytes (2-byte uintvar), intact",
          ct == 0x9E and loc == "photo.jpg" and data == bytes(i % 256 for i in range(300)))
    ct, extra, loc, data = parts[2]
    check("unknown type is sent as a text string", ct == "application/x-thing" and data == b"\x01\x02\x03")
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
