"""Verifying an application update before installing it (no auto-update, no update server, nothing fetched here).

A release is a manifest listing each artifact's SHA-256, signed with the maintainer's Ed25519 release key. You pin that
public key once (`python -m phonebridge.updates pin <key>`); after that an update is accepted only if
  * the manifest signature verifies against the pinned key,
  * its version is strictly newer than the installed one (so an old, validly signed release can't be replayed to
    downgrade you), and
  * the artifact file you downloaded matches the signed SHA-256 and size.
Without a pinned key everything is refused: there is no built-in key that someone else holds.
Android: updates are sideloaded APKs, which Android only installs over the existing app if they are signed with the same
key — the platform enforces that; this module covers the PC side.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


class UpdateError(Exception):
    pass


def canonical(manifest: dict) -> bytes:
    return json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()


def parse_version(v: str) -> tuple[int, ...]:
    if not re.fullmatch(r"\d+(\.\d+){0,3}", v):
        raise UpdateError(f"bad version {v!r}")
    return tuple(int(x) for x in v.split("."))


@dataclass
class Manifest:
    app: str
    version: str
    artifacts: dict[str, dict]   # name -> {"sha256": ..., "size": ...}


def pinned_key_path() -> Path:
    from . import pairing

    return pairing.DEVICES_DIR.parent / "release_key.pub"


def pinned_key() -> str | None:
    try:
        return pinned_key_path().read_text().strip() or None
    except OSError:
        return None


def pin_key(public_b64: str) -> None:
    Ed25519PublicKey.from_public_bytes(base64.b64decode(public_b64))   # must be a valid key
    path = pinned_key_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(public_b64 + "\n")
    path.chmod(0o600)


def verify_manifest(manifest_bytes: bytes, signature_b64: str, current_version: str, public_b64: str | None = None) -> Manifest:
    key = public_b64 or pinned_key()
    if not key:
        raise UpdateError("no release key is pinned: refusing every update (pin the maintainer's public key first)")
    try:
        manifest = json.loads(manifest_bytes)
        Ed25519PublicKey.from_public_bytes(base64.b64decode(key)).verify(base64.b64decode(signature_b64), canonical(manifest))
    except InvalidSignature as exc:
        raise UpdateError("the manifest signature is not valid for the pinned release key") from exc
    except (ValueError, TypeError) as exc:
        raise UpdateError(f"malformed manifest or signature: {exc}") from exc
    if manifest.get("app") != "phonebridge":
        raise UpdateError("this manifest is not for PhoneBridge")
    if parse_version(str(manifest.get("version", ""))) <= parse_version(current_version):
        raise UpdateError(f"version {manifest.get('version')} is not newer than the installed {current_version} "
                          "(refusing a downgrade or replay)")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict) or not artifacts:
        raise UpdateError("the manifest lists no artifacts")
    return Manifest("phonebridge", str(manifest["version"]), artifacts)


def verify_artifact(path: Path, manifest: Manifest, name: str | None = None) -> None:
    entry = manifest.artifacts.get(name or path.name)
    if entry is None:
        raise UpdateError(f"{name or path.name} is not part of the signed release")
    digest, size = hashlib.sha256(), 0
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            digest.update(chunk)
            size += len(chunk)
    if size != entry.get("size") or digest.hexdigest() != entry.get("sha256"):
        raise UpdateError(f"{path.name} does not match the signed release (damaged or tampered with)")


def _fetch(base_url: str, name: str, limit: int) -> bytes:
    """Reads `<base_url>/<name>` over https or file:// (plain http is refused: the signature protects integrity, but there is
    no reason to leak what you are fetching). At most `limit` bytes."""
    import urllib.parse
    import urllib.request

    scheme = urllib.parse.urlparse(base_url).scheme
    if scheme not in ("https", "file"):
        raise UpdateError(f"update URL must be https:// or file://, not {scheme or 'a bare path'}://")
    if "/" in name or name.startswith("."):
        raise UpdateError(f"unsafe artifact name {name!r}")
    with urllib.request.urlopen(base_url.rstrip("/") + "/" + name, timeout=30) as resp:  # nosec B310 - scheme checked above
        data = resp.read(limit + 1)
    if len(data) > limit:
        raise UpdateError(f"{name} is larger than {limit} bytes")
    return data


def check_for_update(base_url: str, current_version: str, download_dir: Path | None = None) -> tuple[Manifest, list[Path]]:
    """Fetches manifest.json + manifest.sig, verifies them against the pinned key (and that the version is newer), and — if
    `download_dir` is given — downloads every listed artifact there and checks each against the signed SHA-256. Installs
    nothing. Raises UpdateError on any problem."""
    manifest_bytes = _fetch(base_url, "manifest.json", 1 << 20)
    signature = _fetch(base_url, "manifest.sig", 4096).decode().strip()
    manifest = verify_manifest(manifest_bytes, signature, current_version)
    saved: list[Path] = []
    if download_dir is not None:
        target = Path(download_dir) / manifest.version
        target.mkdir(parents=True, exist_ok=True)
        for name, entry in manifest.artifacts.items():
            path = target / name
            path.write_bytes(_fetch(base_url, name, int(entry.get("size", 0)) + 1))
            verify_artifact(path, manifest, name)   # a download that doesn't match is deleted, not kept
            saved.append(path)
    return manifest, saved


def updates_errors():
    import urllib.error

    return (UpdateError, urllib.error.URLError, OSError)


def main(argv: list[str]) -> int:
    from importlib import metadata

    if len(argv) >= 2 and argv[0] == "pin":
        pin_key(argv[1])
        print(f"pinned release key {argv[1][:12]}…")
        return 0
    if len(argv) >= 3 and argv[0] == "verify":
        manifest_path, sig_path = Path(argv[1]), Path(argv[2])
        try:
            current = metadata.version("phonebridge")
        except metadata.PackageNotFoundError:
            current = "0"
        try:
            manifest = verify_manifest(manifest_path.read_bytes(), sig_path.read_text().strip(), current)
            for artifact in argv[3:]:
                verify_artifact(Path(artifact), manifest)
        except UpdateError as exc:
            print(f"REFUSED: {exc}", file=sys.stderr)
            return 1
        print(f"OK: release {manifest.version} is signed by the pinned key; " + (f"{len(argv) - 3} artifact(s) verified" if argv[3:] else "manifest verified"))
        return 0
    if len(argv) >= 2 and argv[0] == "check":
        try:
            current = metadata.version("phonebridge")
        except metadata.PackageNotFoundError:
            current = "0"
        dest = Path(argv[2]) if len(argv) > 2 else None
        try:
            manifest, saved = check_for_update(argv[1], current, dest)
        except updates_errors() as exc:
            print(f"NO UPDATE: {exc}", file=sys.stderr)
            return 1
        print(f"Update available and verified: {current} -> {manifest.version}")
        for p in saved:
            print(f"  downloaded and verified: {p}")
        if saved:
            print("Install it yourself, e.g.:  ~/.local/share/phonebridge/venv/bin/pip install " + " ".join(str(p) for p in saved if p.suffix == ".whl"))
        return 0
    print("usage: python -m phonebridge.updates pin <public-key-b64> | check <base-url> [download-dir] | verify <manifest.json> <manifest.sig> [artifact…]")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
