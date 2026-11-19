#!/usr/bin/env python3
"""Checks PhoneBridge's dependencies against the OSV vulnerability database (https://osv.dev).

Inventory: the Python packages declared in linux/pyproject.toml (installed versions) and the Maven libraries declared in
android/app/build.gradle.kts. Only package names and versions are sent to api.osv.dev, and only when you run this tool;
nothing is sent by the apps themselves. `--offline` prints the inventory without any network access.

Exit status: 0 = no known vulnerabilities, 1 = vulnerabilities found, 2 = the check could not run.
Run: python3 tools/audit_dependencies.py [--offline] [--json]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OSV = "https://api.osv.dev/v1"


def app_python() -> str | None:
    """The interpreter the installed PC app runs on (its own venv), if installed; that is what really ships."""
    venv = Path.home() / ".local/share/phonebridge/venv/bin/python"
    return str(venv) if venv.exists() else None


def _versions(names: list[str], python: str | None) -> dict[str, str | None]:
    if python is None:
        out = {}
        for n in names:
            try:
                out[n] = metadata.version(n)
            except metadata.PackageNotFoundError:
                out[n] = None
        return out
    import subprocess

    code = ("import json,sys\nfrom importlib import metadata\nout={}\nfor n in sys.argv[1:]:\n try: out[n]=metadata.version(n)\n"
            " except metadata.PackageNotFoundError: out[n]=None\nprint(json.dumps(out))")
    return json.loads(subprocess.run([python, "-I", "-c", code, *names], capture_output=True, text=True, check=True).stdout)


def python_inventory(python: str | None = None) -> list[dict]:
    text = (ROOT / "linux/pyproject.toml").read_text()
    block = re.search(r"dependencies\s*=\s*\[(.*?)\]", text, re.S)
    names = re.findall(r'"([A-Za-z0-9_.\-]+)', block.group(1)) if block else []
    versions = _versions(names, python)
    return [{"ecosystem": "PyPI", "name": n, "version": versions[n]} for n in names]


def android_inventory() -> list[dict]:
    text = (ROOT / "android/app/build.gradle.kts").read_text()
    out = []
    for group, artifact, version in re.findall(r'implementation\("([^":]+):([^":]+):([^"]+)"\)', text):
        out.append({"ecosystem": "Maven", "name": f"{group}:{artifact}", "version": version})
    return out


def _post(url: str, body: dict) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as resp:  # nosec B310 - fixed https://api.osv.dev
        return json.load(resp)


def _get(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=20) as resp:  # nosec B310 - fixed https://api.osv.dev
        return json.load(resp)


def scan(inventory: list[dict], post=_post, get=_get) -> list[dict]:
    """Returns [{package, version, id, summary, aliases}] for every known vulnerability affecting the inventory."""
    known = [p for p in inventory if p["version"]]
    if not known:
        return []
    queries = [{"package": {"name": p["name"], "ecosystem": p["ecosystem"]}, "version": p["version"]} for p in known]
    results = post(f"{OSV}/querybatch", {"queries": queries}).get("results", [])
    findings = []
    for pkg, res in zip(known, results):
        for vuln in res.get("vulns", []):
            try:
                detail = get(f"{OSV}/vulns/{vuln['id']}")
            except OSError:
                detail = {}
            findings.append({"package": pkg["name"], "version": pkg["version"], "id": vuln["id"],
                             "summary": detail.get("summary", ""), "aliases": detail.get("aliases", [])})
    return findings


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true", help="only list what would be checked")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--python", help="interpreter whose installed packages to check (default: the installed app's venv, "
                                     "else this interpreter)")
    args = ap.parse_args(argv)
    python = args.python or app_python()
    inventory = python_inventory(python) + android_inventory()
    print(f"(Python packages from {python or sys.executable})", file=sys.stderr)
    if args.offline:
        for p in inventory:
            print(f"{p['ecosystem']:6} {p['name']:45} {p['version'] or '(not installed)'}")
        return 0
    try:
        findings = scan(inventory)
    except OSError as exc:
        print(f"could not reach the OSV database: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps({"checked": inventory, "vulnerabilities": findings}, indent=2))
    else:
        print(f"Checked {len([p for p in inventory if p['version']])} packages against OSV.")
        for f in findings:
            print(f"  VULNERABLE  {f['package']} {f['version']}  {f['id']}  {f['summary']}")
        if not findings:
            print("  no known vulnerabilities")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
