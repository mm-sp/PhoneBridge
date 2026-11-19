"""The PC's long-lived TLS identity: one self-signed EC cert, reused across all pairings.

Its SHA-256 fingerprint is what gets handed to a phone during pairing (lan_info) and
pinned there forever after — this is the trust-on-first-use anchor for the Wi-Fi
transport, standing in for a CA since there is no CA, no account, no cloud.
"""
from __future__ import annotations

import datetime
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

CONFIG_DIR = Path.home() / ".config" / "phonebridge"
CERT_PATH = CONFIG_DIR / "server_cert.pem"
KEY_PATH = CONFIG_DIR / "server_key.pem"


KEY_NAME = "tls-server-key"


def _store():
    from .security import keystore

    return keystore.for_config_dir(CONFIG_DIR)


def _key_matches(pem: bytes, cert) -> bool:
    try:
        key = serialization.load_pem_private_key(pem, None)
    except ValueError:
        return False
    fmt = (serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return key.public_key().public_bytes(*fmt) == cert.public_key().public_bytes(*fmt)


def ensure_server_identity() -> tuple[Path, str]:
    """Returns (cert_path, sha256_fingerprint_hex). The private key lives in the keystore (desktop keyring), never
    in a file; an old server_key.pem from earlier versions is moved there and deleted."""
    if CONFIG_DIR.exists():   # tighten installs created before the dir was private (keys, pairings live here)
        CONFIG_DIR.chmod(0o700)
        if (CONFIG_DIR / "devices").is_dir():
            (CONFIG_DIR / "devices").chmod(0o700)
    store = _store()
    if KEY_PATH.exists():   # migrate
        if store.get(KEY_NAME) is None:
            store.set(KEY_NAME, KEY_PATH.read_bytes())
        KEY_PATH.unlink()
    if CERT_PATH.exists() and store.get(KEY_NAME) is not None:
        cert = x509.load_pem_x509_certificate(CERT_PATH.read_bytes())
        if _key_matches(store.get(KEY_NAME), cert):
            return CERT_PATH, cert.fingerprint(hashes.SHA256()).hex()
        # Cert and stored key disagree (key lost/replaced): the cert can never be served again, so make a new
        # identity. Phones pin the old fingerprint and will have to be paired again.
        import logging

        logging.getLogger("phonebridge.tls").error("stored TLS key does not match the certificate; generating a new identity "
                                                   "(paired phones must be re-paired)")

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_DIR.chmod(0o700)
    key = ec.generate_private_key(ec.SECP256R1())
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "PhoneBridge PC")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=3650))
        .sign(key, hashes.SHA256())
    )
    store.set(KEY_NAME, key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ))
    CERT_PATH.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return CERT_PATH, cert.fingerprint(hashes.SHA256()).hex()


def load_cert_chain(ctx) -> None:
    """Loads the cert + keystore key into an ssl.SSLContext. OpenSSL wants a file, so the key goes to a 0600 file in
    a private temp dir (XDG_RUNTIME_DIR = tmpfs when available) for the instant of loading, then is deleted."""
    import os
    import shutil
    import tempfile

    pem = _store().get(KEY_NAME)
    if pem is None:
        raise RuntimeError("server TLS key missing; call ensure_server_identity() first")
    tmp = tempfile.mkdtemp(prefix="pb-tls-", dir=os.environ.get("XDG_RUNTIME_DIR"))
    try:
        path = Path(tmp) / "k.pem"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(pem)
        ctx.load_cert_chain(str(CERT_PATH), str(path))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
