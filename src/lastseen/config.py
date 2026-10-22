"""Data folder, settings, stored credentials and small secrets."""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import os
from pathlib import Path
import secrets
import sys
import threading
from typing import Any

log = logging.getLogger(__name__)

APP = "lastseen"
# pyicloud's keyring service name, kept so an existing Keychain entry still works.
KEYRING_SYSTEM = "pyicloud://icloud-password"

DEFAULTS: dict[str, Any] = {
    "apple_id": None,
    "interval_minutes": 15,
    "retention_days": 0,
    "access_password_hash": None,
    "needs_login": None,
    "credential_store": None,
}


def is_loopback(host: str) -> bool:
    if host.lower() in ("localhost", "127.0.0.1", "::1"):
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def instance_id(data_dir: Path) -> str:
    """Identifies a data folder in /api/health without revealing its path."""
    return hashlib.sha256(str(Path(data_dir).resolve()).encode()).hexdigest()[:16]


def in_docker() -> bool:
    return os.environ.get("LASTSEEN_IN_DOCKER") == "1"


def default_data_dir() -> Path:
    home = Path.home()
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / APP
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        return (Path(base) if base else home / "AppData" / "Local") / APP
    base = os.environ.get("XDG_DATA_HOME")
    return (Path(base) if base else home / ".local" / "share") / APP


def resolve_data_dir(override: str | Path | None = None) -> Path:
    """--data-dir, then LASTSEEN_DATA_DIR, then the platform default; created 0700."""
    raw = override or os.environ.get("LASTSEEN_DATA_DIR")
    path = Path(raw).expanduser() if raw else default_data_dir()
    ensure_private_dir(path)
    return path.resolve()


def ensure_private_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    chmod_private(path, 0o700)


def chmod_private(path: Path, mode: int) -> None:
    """Restrict permissions where the file system allows it (bind mounts, NAS shares may not)."""
    if os.name == "nt":
        return
    try:
        os.chmod(path, mode)
    except OSError as exc:
        log.warning("Couldn't restrict permissions on %s (%s)", path, exc.strerror or type(exc).__name__)


def write_private(path: Path, data: str | bytes) -> None:
    """Atomically write a file readable only by the current user."""
    raw = data.encode() if isinstance(data, str) else data
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    chmod_private(path, 0o600)


class ConfigStore:
    """config.json, reloaded when another process (e.g. `reset-password`) edits it."""

    def __init__(self, data_dir: Path):
        self.path = data_dir / "config.json"
        self._lock = threading.Lock()
        self._mtime: int | None = None
        self._data = self._load()

    def _load(self) -> dict[str, Any]:
        data = dict(DEFAULTS)
        try:
            self._mtime = self.path.stat().st_mtime_ns
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                raise ValueError("config is not an object")
        except FileNotFoundError:
            self._mtime = None
            return data
        except (OSError, ValueError) as exc:
            log.error("config.json is unreadable (%s); starting with defaults", type(exc).__name__)
            try:
                os.replace(self.path, self.path.with_name("config.json.bad"))
            except OSError:
                pass
            self._mtime = None
            return data
        data.update({k: v for k, v in loaded.items() if k in DEFAULTS})
        return data

    @property
    def data(self) -> dict[str, Any]:
        with self._lock:
            try:
                mtime: int | None = self.path.stat().st_mtime_ns
            except OSError:
                mtime = None
            if mtime != self._mtime:
                self._data = self._load()
            return dict(self._data)

    def get(self, key: str) -> Any:
        return self.data.get(key)

    def update(self, **changes: Any) -> dict[str, Any]:
        with self._lock:
            # Merge into what is on disk now, so a change made by another
            # process (e.g. `lastseen reset-password`) isn't written back over.
            try:
                mtime: int | None = self.path.stat().st_mtime_ns
            except OSError:
                mtime = None
            if mtime != self._mtime:
                self._data = self._load()
            self._data.update(changes)
            write_private(self.path, json.dumps(self._data, indent=2))
            self._mtime = self.path.stat().st_mtime_ns
            return dict(self._data)


# --- Access password, cookie secret, setup token -----------------------------

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    b64 = base64.b64encode
    return f"scrypt$16384$8$1${b64(salt).decode()}${b64(digest).decode()}"


def verify_password(password: str, stored: str | None) -> bool:
    if not stored or not isinstance(password, str):
        return False
    try:
        algo, n, r, p, salt, digest = stored.split("$")
        if algo != "scrypt":
            return False
        expected = base64.b64decode(digest)
        actual = hashlib.scrypt(
            password.encode(), salt=base64.b64decode(salt),
            n=int(n), r=int(r), p=int(p), dklen=len(expected),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def secret_key(data_dir: Path) -> bytes:
    path = data_dir / "secret.key"
    try:
        key = path.read_bytes()
        if len(key) >= 32:
            return key
    except FileNotFoundError:
        pass
    key = secrets.token_bytes(32)
    write_private(path, key)
    return key


def setup_token(data_dir: Path, create: bool = False) -> str | None:
    path = data_dir / "setup-token"
    try:
        token = path.read_text(encoding="utf-8").strip()
        if token:
            return token
    except FileNotFoundError:
        pass
    if not create:
        return None
    token = secrets.token_urlsafe(24)
    write_private(path, token)
    return token


def delete_setup_token(data_dir: Path) -> None:
    (data_dir / "setup-token").unlink(missing_ok=True)


# --- Apple Account password ---------------------------------------------------

def keyring_usable() -> bool:
    """True when the OS keychain can hold the Apple password.

    Plaintext/encrypted-file backends from keyrings.alt store the password
    outside the data folder, so the data folder's own 0600 file is preferred.
    """
    if os.environ.get("LASTSEEN_CREDENTIAL_STORE", "").lower() == "file" or in_docker():
        return False
    try:
        import keyring
        from keyring.backends import chainer, fail

        backend = keyring.get_keyring()
    except Exception:
        return False

    def unusable(b: object) -> bool:
        module = type(b).__module__
        return isinstance(b, fail.Keyring) or module.startswith(("keyrings.alt", "keyring.backends.null"))

    if isinstance(backend, chainer.ChainerBackend):
        try:
            backends = list(backend.backends)
        except Exception:
            return False
        return bool(backends) and not unusable(backends[0])
    return not unusable(backend)


def _cred_file(data_dir: Path) -> Path:
    return data_dir / "credentials.json"


def store_password(data_dir: Path, apple_id: str, password: str) -> str:
    """Save the Apple password; returns the store used ("keyring" or "file")."""
    if keyring_usable():
        try:
            import keyring

            keyring.set_password(KEYRING_SYSTEM, apple_id, password)
            _cred_file(data_dir).unlink(missing_ok=True)
            return "keyring"
        except Exception as exc:
            log.warning("Couldn't save to the system keychain (%s); using the data folder", type(exc).__name__)
    write_private(_cred_file(data_dir), json.dumps({"apple_id": apple_id, "password": password}))
    _keyring_delete(apple_id)
    return "file"


def load_password(data_dir: Path, apple_id: str) -> str | None:
    if keyring_usable():
        try:
            import keyring

            password = keyring.get_password(KEYRING_SYSTEM, apple_id)
            if password:
                return password
        except Exception as exc:
            log.warning("Couldn't read the system keychain (%s)", type(exc).__name__)
    try:
        data = json.loads(_cred_file(data_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if isinstance(data, dict) and str(data.get("apple_id", "")).lower() == apple_id.lower():
        return data.get("password") or None
    return None


def forget_password(data_dir: Path, apple_id: str | None) -> None:
    _cred_file(data_dir).unlink(missing_ok=True)
    if apple_id:
        _keyring_delete(apple_id)


def _keyring_delete(apple_id: str) -> None:
    if not keyring_usable():
        return
    try:
        import keyring

        keyring.delete_password(KEYRING_SYSTEM, apple_id)
    except Exception:
        pass
