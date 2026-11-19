#!/usr/bin/env python3
"""Static check: source code must not write key material, PINs or user content (message/clipboard/notification text) to logs,
and the Android app must not use debug/verbose logging. Run: `python3 tests/test_log_hygiene.py`"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PASS = FAIL = 0
# identifiers that must never appear inside a logging call
SECRETS = re.compile(r"(session_?key|sessionKey|private_?key|privateKey|priv\b|token|passphrase|password|\bpin\b|psk|exporter|udp_?key|"
                     r"master|secret|clipboard_?text|clipText|\.text\b|\bbody\b|\bnumber\b|sms_?text)", re.I)
PY_LOG = re.compile(r"\blog(?:ger)?\.(?:debug|info|warning|error|exception|critical)\(")
KT_LOG = re.compile(r"\bLog\.[dviwe]\(")


def check(label, cond):
    global PASS, FAIL
    PASS, FAIL = (PASS + 1, FAIL) if cond else (PASS, FAIL + 1)
    print(f"  {'ok ' if cond else 'FAIL'} {label}")


STRING = re.compile(r"(?<![\w])(f?)(\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*')")


def code_only(call: str) -> str:
    """The identifiers a log call actually evaluates: string literals are dropped, except the values interpolated into
    them (Kotlin "$name" / "${expr}", Python f"{expr}"), so prose like "Secret Service unavailable" is not flagged."""
    interpolated = []

    def drop(m):
        body = m.group(2)[1:-1]
        interpolated.extend(a or b for a, b in re.findall(r"\$\{([^}]*)\}|\$([A-Za-z_][\w.]*)", body))   # Kotlin
        if m.group(1) == "f":
            interpolated.extend(re.findall(r"\{([^{}]*)\}", body))                                       # Python f-string
        return " "

    return STRING.sub(drop, call) + " " + " ".join(interpolated)


def log_calls(path: Path, pattern):
    """Yields (line number, full call text) for each logging call, following simple multi-line calls."""
    lines = path.read_text(errors="ignore").splitlines()
    for i, line in enumerate(lines):
        if pattern.search(line):
            text, j = line, i
            while text.count("(") > text.count(")") and j + 1 < len(lines) and j - i < 6:
                j += 1
                text += " " + lines[j].strip()
            yield i + 1, text


def main():
    cases = {'log.info("pairing PIN is %s", pin)': True, 'log.warning("Secret Service unavailable (%s)", exc)': False,
             'Log.i(TAG, "got $sessionKey")': True, 'Log.i(TAG, "dropping: $why")': False, 'log.info(f"clip {clipboard_text}")': True,
             'log.info("device connected: %s", device.name)': False, 'Log.i(TAG, "x ${p.optString("body")}")': True}
    check("the detector itself flags secrets and ignores prose (self-check)",
          all(bool(SECRETS.search(code_only(c.split("(", 1)[1]))) == want for c, want in cases.items()))
    offenders = []
    py = [p for p in (ROOT / "linux/phonebridge").rglob("*.py") if "build" not in p.parts]
    kt = list((ROOT / "android/app/src/main/java").rglob("*.kt"))
    n = 0
    for p in py:
        for ln, call in log_calls(p, PY_LOG):
            n += 1
            if SECRETS.search(code_only(call.split("(", 1)[1])):
                offenders.append(f"{p.relative_to(ROOT)}:{ln}: {call.strip()[:110]}")
    for p in kt:
        for ln, call in log_calls(p, KT_LOG):
            n += 1
            if SECRETS.search(code_only(call.split("(", 1)[1])):
                offenders.append(f"{p.relative_to(ROOT)}:{ln}: {call.strip()[:110]}")
    print(f"scanned {n} logging calls in {len(py)} Python and {len(kt)} Kotlin files")
    for o in offenders:
        print("  suspicious:", o)
    check("no logging call mentions keys, PINs, tokens or user content", not offenders)
    verbose = [f"{p.relative_to(ROOT)}:{ln}" for p in kt for ln, _c in log_calls(p, re.compile(r"\bLog\.[dv]\("))]
    check("the Android app uses no debug/verbose logging", not verbose)
    for v in verbose:
        print("  verbose log:", v)
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
