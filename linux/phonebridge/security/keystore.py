"""Secret storage. Private keys and pairing session keys live here instead of in plain config files.

Backends:
  * SecretServiceStore — the desktop keyring (GNOME Keyring / KDE Wallet) through libsecret. Preferred.
  * FileStore — fallback when no Secret Service is reachable. If PHONEBRIDGE_PASSPHRASE is set the file is
    encrypted (scrypt -> AES-256-GCM); otherwise it is only a 0600 file and `protected` is False — the
    dashboard reports that honestly rather than pretending it is encrypted.
"""
from __future__ import annotations

import base64
import json
import logging
import os
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

log = logging.getLogger("phonebridge.keystore")
def _account_home() -> Path:
    """The account's real home from the password database, NOT $HOME: a test (or script) that sets HOME to a
    scratch directory must never be mistaken for the real config and end up reading/overwriting the user's
    keyring entries."""
    try:
        import pwd

        return Path(pwd.getpwuid(os.getuid()).pw_dir)
    except (ImportError, KeyError):
        return Path.home()


DEFAULT_CONFIG_DIR = _account_home() / ".config" / "phonebridge"


class KeyStore:
    backend = "none"
    protected = False

    def get(self, name: str) -> bytes | None:
        raise NotImplementedError

    def set(self, name: str, value: bytes) -> None:
        raise NotImplementedError

    def delete(self, name: str) -> None:
        raise NotImplementedError


class SecretServiceStore(KeyStore):
    backend = "secret-service"
    protected = True

    def __init__(self) -> None:
        import gi

        gi.require_version("Secret", "1")
        from gi.repository import Secret

        self._Secret = Secret
        self._schema = Secret.Schema.new("dev.phonebridge.Secret", Secret.SchemaFlags.NONE,
                                         {"name": Secret.SchemaAttributeType.STRING})
        # Fail now (not on first use) if the keyring is locked/unreachable so the caller can fall back.
        Secret.password_lookup_sync(self._schema, {"name": "__probe__"}, None)

    def get(self, name: str) -> bytes | None:
        value = self._Secret.password_lookup_sync(self._schema, {"name": name}, None)
        return base64.b64decode(value) if value is not None else None

    def set(self, name: str, value: bytes) -> None:
        ok = self._Secret.password_store_sync(self._schema, {"name": name}, self._Secret.COLLECTION_DEFAULT,
                                              f"PhoneBridge {name}", base64.b64encode(value).decode(), None)
        if not ok:
            raise OSError("the keyring refused to store the secret")

    def delete(self, name: str) -> None:
        self._Secret.password_clear_sync(self._schema, {"name": name}, None)


class FileStore(KeyStore):
    backend = "file"

    def __init__(self, directory: Path, passphrase: str | None = None) -> None:
        self._path = directory / "secrets.json"
        self._passphrase = passphrase if passphrase is not None else os.environ.get("PHONEBRIDGE_PASSPHRASE") or None
        self.protected = self._passphrase is not None
        directory.mkdir(parents=True, exist_ok=True)

    def _kdf(self, salt: bytes) -> bytes:
        return Scrypt(salt=salt, length=32, n=2 ** 15, r=8, p=1).derive(self._passphrase.encode())

    def _load(self) -> dict[str, str]:
        if not self._path.exists():
            return {}
        raw = json.loads(self._path.read_text())
        if "enc" in raw:
            if not self._passphrase:
                raise OSError("secrets file is encrypted: set PHONEBRIDGE_PASSPHRASE")
            salt, blob = base64.b64decode(raw["salt"]), base64.b64decode(raw["enc"])
            return json.loads(AESGCM(self._kdf(salt)).decrypt(blob[:12], blob[12:], b"phonebridge-secrets-v1"))
        return raw.get("plain", {})

    def _save(self, data: dict[str, str]) -> None:
        if self._passphrase:
            salt, nonce = os.urandom(16), os.urandom(12)
            ct = AESGCM(self._kdf(salt)).encrypt(nonce, json.dumps(data).encode(), b"phonebridge-secrets-v1")
            doc = {"salt": base64.b64encode(salt).decode(), "enc": base64.b64encode(nonce + ct).decode()}
        else:
            doc = {"plain": data}
        tmp = self._path.with_suffix(".tmp")
        # Created 0600 from the start (O_EXCL on a fresh name), so there is no moment when the secrets are group/world-readable.
        tmp.unlink(missing_ok=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps(doc))
        tmp.replace(self._path)

    def get(self, name: str) -> bytes | None:
        v = self._load().get(name)
        return base64.b64decode(v) if v is not None else None

    def set(self, name: str, value: bytes) -> None:
        data = self._load()
        data[name] = base64.b64encode(value).decode()
        self._save(data)

    def delete(self, name: str) -> None:
        data = self._load()
        if data.pop(name, None) is not None:
            self._save(data)


_cache: dict[str, KeyStore] = {}


def for_config_dir(config_dir: Path) -> KeyStore:
    """The real ~/.config/phonebridge uses the desktop keyring (falling back to FileStore); any other
    directory (tests, portable setups) gets its own FileStore so it never touches the user's keyring."""
    key = str(config_dir)
    if key not in _cache:
        store: KeyStore | None = None
        if config_dir == DEFAULT_CONFIG_DIR and Path.home() == _account_home() and os.environ.get("PHONEBRIDGE_KEYSTORE") != "file":
            try:
                store = SecretServiceStore()
            except Exception as exc:  # noqa: BLE001 - no daemon, locked keyring, missing gi bindings
                log.warning("Secret Service unavailable (%s); using the file fallback", exc)
        _cache[key] = store or FileStore(config_dir)
    return _cache[key]
