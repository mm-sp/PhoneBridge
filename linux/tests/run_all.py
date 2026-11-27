#!/usr/bin/env python3
"""Runs every test file (GUI ones under xvfb-run when no display is wanted) and prints one summary line each.
Usage: python3 tests/run_all.py [python-executable]   (default: the interpreter running this script)
Tests needing fusepy (test_phone_fs) are skipped by their own check if it isn't installed for that interpreter."""
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
FAIL_TAIL = 30  # lines of a failing file's output that are shown under its summary line
py = sys.argv[1] if len(sys.argv) > 1 else sys.executable
total_pass = total_fail = 0
bad = []
for t in sorted(HERE.glob("test_*.py")):
    cmd = [py, "-u", str(t)]
    if t.name.endswith("_ui.py") and shutil.which("xvfb-run"):
        cmd = ["xvfb-run", "-a"] + cmd
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        out = r.stdout + r.stderr
    except subprocess.TimeoutExpired:
        out, r = "TIMEOUT", None
    m = re.findall(r"(\d+) passed, (\d+) failed", out)
    if m:
        p, f = (int(x) for x in m[-1])
    else:
        p, f = 0, 0 if "SKIP" in out else 1
    status = "SKIP" if "SKIP" in out and not m else ("ok" if f == 0 and (r is not None and r.returncode == 0 or p) else "FAIL")
    if r is None or (r.returncode != 0 and not m):
        status = "FAIL"
    if f:
        status = "FAIL"
    if status == "SKIP" and os.environ.get("PHONEBRIDGE_STRICT") == "1":
        status = "FAIL"  # strict mode (CI): a skipped test is missing evidence, not a pass
        out_tail = out.strip().splitlines()[-1:] 
        print("     (strict) skipped:", *out_tail)
    total_pass += p; total_fail += f
    if status == "FAIL":
        bad.append(t.name)
    print(f"{status:4} {t.name:34} {p:4} passed {f} failed")
    if status == "FAIL":  # without this a crash in the full run says nothing about why
        for line in out.strip().splitlines()[-FAIL_TAIL:]:
            print(f"     | {line}")
print(f"\nTOTAL {total_pass} checks passed, {total_fail} failed" + (f"  FAILING FILES: {', '.join(bad)}" if bad else ""))
sys.exit(1 if bad else 0)
