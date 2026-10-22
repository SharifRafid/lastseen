from __future__ import annotations

import json
import os

import keyring
from keyring.backends import chainer, fail
import pytest

from lastseen import config


def test_config_ignores_unknown_keys(tmp_path):
    (tmp_path / "config.json").write_text(json.dumps({"apple_id": "a@icloud.com", "junk": 1}))
    cfg = config.ConfigStore(tmp_path).data
    assert cfg["apple_id"] == "a@icloud.com" and cfg["interval_minutes"] == 15 and "junk" not in cfg


def test_corrupt_config_recovers(tmp_path):
    (tmp_path / "config.json").write_text("{nope")
    store = config.ConfigStore(tmp_path)
    assert store.data["apple_id"] is None
    assert (tmp_path / "config.json.bad").exists()
    store.update(interval_minutes=30)
    assert config.ConfigStore(tmp_path).get("interval_minutes") == 30


def test_external_edits_are_seen(tmp_path):
    store = config.ConfigStore(tmp_path)
    store.update(access_password_hash="x")
    other = config.ConfigStore(tmp_path)
    other.update(access_password_hash=None)
    os.utime(tmp_path / "config.json", ns=(1, 1))  # make sure mtime differs
    assert store.get("access_password_hash") is None


def test_update_does_not_undo_other_writers(tmp_path):
    server = config.ConfigStore(tmp_path)
    server.update(access_password_hash="HASH")
    config.ConfigStore(tmp_path).update(access_password_hash=None)  # lastseen reset-password
    os.utime(tmp_path / "config.json", ns=(1, 1))  # coarse-mtime file systems
    server.update(needs_login={"reason": "x"})
    on_disk = json.loads((tmp_path / "config.json").read_text())
    assert on_disk["access_password_hash"] is None and on_disk["needs_login"] == {"reason": "x"}


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_chmod_failure_is_not_fatal(tmp_path, monkeypatch, caplog):
    def deny(*a, **k):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(config.os, "chmod", deny)
    d = tmp_path / "share"
    config.ensure_private_dir(d)
    config.write_private(d / "x", "y")
    assert (d / "x").read_text() == "y"
    assert "Couldn't restrict permissions" in caplog.text


def test_instance_id(tmp_path):
    assert config.instance_id(tmp_path) == config.instance_id(tmp_path / "." )
    assert config.instance_id(tmp_path) != config.instance_id(tmp_path / "other")
    assert str(tmp_path) not in config.instance_id(tmp_path)


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_private_permissions(tmp_path):
    d = tmp_path / "data"
    config.ensure_private_dir(d)
    config.ConfigStore(d).update(apple_id="a")
    config.secret_key(d)
    config.setup_token(d, create=True)
    config.store_password(d, "a", "pw")
    assert d.stat().st_mode & 0o777 == 0o700
    for name in ("config.json", "secret.key", "setup-token", "credentials.json"):
        assert (d / name).stat().st_mode & 0o777 == 0o600, name


def test_password_hashing():
    h = config.hash_password("correct horse")
    assert h.startswith("scrypt$16384$8$1$")
    assert config.verify_password("correct horse", h)
    assert not config.verify_password("wrong", h)
    assert not config.verify_password("x", "garbage")
    assert not config.verify_password(None, h)


def test_data_dir_resolution(tmp_path, monkeypatch):
    monkeypatch.setenv("LASTSEEN_DATA_DIR", str(tmp_path / "env"))
    assert config.resolve_data_dir() == (tmp_path / "env").resolve()
    assert config.resolve_data_dir(tmp_path / "flag") == (tmp_path / "flag").resolve()


def test_default_dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(config.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(config.sys, "platform", "linux")
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    assert config.default_data_dir() == tmp_path / ".local" / "share" / "lastseen"
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert config.default_data_dir() == tmp_path / "xdg" / "lastseen"
    monkeypatch.setattr(config.sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    assert config.default_data_dir() == tmp_path / "local" / "lastseen"
    monkeypatch.setattr(config.sys, "platform", "darwin")
    assert config.default_data_dir() == tmp_path / "Library" / "Application Support" / "lastseen"


class AltBackend:
    pass


AltBackend.__module__ = "keyrings.alt.file"


def test_keyring_usable(monkeypatch):
    monkeypatch.setenv("LASTSEEN_CREDENTIAL_STORE", "auto")
    monkeypatch.setattr(keyring, "get_keyring", lambda: fail.Keyring())
    assert not config.keyring_usable()
    monkeypatch.setattr(keyring, "get_keyring", lambda: AltBackend())
    assert not config.keyring_usable()
    monkeypatch.setattr(chainer.ChainerBackend, "backends", [AltBackend()])
    monkeypatch.setattr(keyring, "get_keyring", lambda: chainer.ChainerBackend())
    assert not config.keyring_usable()
    monkeypatch.setattr(chainer.ChainerBackend, "backends", [])
    assert not config.keyring_usable()
    monkeypatch.setenv("LASTSEEN_CREDENTIAL_STORE", "file")
    monkeypatch.setattr(keyring, "get_keyring", lambda: object())
    assert not config.keyring_usable()


def test_keyring_error_falls_back_to_file(tmp_path, monkeypatch):
    monkeypatch.setenv("LASTSEEN_CREDENTIAL_STORE", "auto")
    monkeypatch.setattr(config, "keyring_usable", lambda: True)

    def broken(*a):
        raise keyring.errors.KeyringError("locked")

    monkeypatch.setattr(keyring, "set_password", broken)
    monkeypatch.setattr(keyring, "get_password", broken)
    monkeypatch.setattr(keyring, "delete_password", broken)
    assert config.store_password(tmp_path, "a@icloud.com", "pw") == "file"
    assert config.load_password(tmp_path, "A@icloud.com") == "pw"
    assert config.load_password(tmp_path, "other@icloud.com") is None
    config.forget_password(tmp_path, "a@icloud.com")
    assert config.load_password(tmp_path, "a@icloud.com") is None


def test_keyring_used_when_available(tmp_path, monkeypatch):
    store = {}
    monkeypatch.setattr(config, "keyring_usable", lambda: True)
    monkeypatch.setattr(keyring, "set_password", lambda s, u, p: store.__setitem__((s, u), p))
    monkeypatch.setattr(keyring, "get_password", lambda s, u: store.get((s, u)))
    monkeypatch.setattr(keyring, "delete_password", lambda s, u: store.pop((s, u)))
    (tmp_path / "credentials.json").write_text("{}")
    assert config.store_password(tmp_path, "a", "pw") == "keyring"
    assert store == {("pyicloud://icloud-password", "a"): "pw"}
    assert not (tmp_path / "credentials.json").exists()
    assert config.load_password(tmp_path, "a") == "pw"
