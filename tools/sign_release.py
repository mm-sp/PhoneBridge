#!/usr/bin/env python3
"""Maintainer tool: sign a PhoneBridge release so users can verify it (phonebridge.updates).

  python3 tools/sign_release.py keygen release.key            # once; keep release.key offline and private
  python3 tools/sign_release.py sign release.key 1.4.0 dist/phonebridge-1.4.0-py3-none-any.whl app-release.apk
      -> writes manifest.json and manifest.sig next to the first artifact
Publish manifest.json + manifest.sig + the artifacts; users pin the public key printed by keygen.
"""
import base64
import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "linux"))
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat  # noqa: E402

from phonebridge.updates import canonical  # noqa: E402


def main(argv) -> int:
    if len(argv) == 2 and argv[0] == "keygen":
        key = Ed25519PrivateKey.generate()
        path = Path(argv[1])
        path.write_bytes(key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption()))
        path.chmod(0o600)
        print("public key (give this to users to pin):", base64.b64encode(key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode())
        return 0
    if len(argv) >= 4 and argv[0] == "sign":
        key = Ed25519PrivateKey.from_private_bytes(Path(argv[1]).read_bytes())
        artifacts = {}
        for a in argv[3:]:
            data = Path(a).read_bytes()
            artifacts[Path(a).name] = {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
        manifest = {"app": "phonebridge", "version": argv[2], "artifacts": artifacts}
        out = Path(argv[3]).parent
        (out / "manifest.json").write_bytes(canonical(manifest))
        (out / "manifest.sig").write_text(base64.b64encode(key.sign(canonical(manifest))).decode() + "\n")
        print(f"wrote {out / 'manifest.json'} and manifest.sig for version {argv[2]}")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
