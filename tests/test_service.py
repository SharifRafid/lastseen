from __future__ import annotations

import plistlib
import subprocess

import pytest

from lastseen import service


@pytest.fixture
def fake_env(tmp_path, monkeypatch):
    monkeypatch.setattr(service.Path, "home", lambda: tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    calls = []

    def run(args, **kw):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(service.subprocess, "run", run)
    monkeypatch.setattr(service.subprocess, "Popen", lambda args, **kw: calls.append(["popen", *args]))
    monkeypatch.setattr(service.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(service.os, "getuid", lambda: 501, raising=False)
    monkeypatch.setattr(service.sys, "executable", "/opt/lastseen/bin/python")
    monkeypatch.setattr(service.sys, "prefix", "/opt/lastseen")
    service.systemd_available.cache_clear()
    yield calls
    service.systemd_available.cache_clear()


def test_command():
    cmd = service.command(service.Path("/d"), "127.0.0.1", 8765)
    assert cmd[1:] == [
        "-m", "lastseen", "--service", "--no-browser",
        "--data-dir", str(service.Path("/d")), "--host", "127.0.0.1", "--port", "8765",
    ]


def test_command_keeps_allowed_hosts():
    cmd = service.command(service.Path("/d"), "127.0.0.1", 8765, {"b.example", "a.example"})
    assert cmd[-2:] == ["--allowed-hosts", "a.example,b.example"]


def test_service_install_keeps_allowed_hosts(fake_env, tmp_path, monkeypatch):
    monkeypatch.setattr(service.sys, "platform", "darwin")
    service.install(tmp_path / "data", "127.0.0.1", 8765, ["lastseen.example.com"])
    plist = plistlib.loads(service.plist_path().read_bytes())
    assert plist["ProgramArguments"][-2:] == ["--allowed-hosts", "lastseen.example.com"]


def test_refuses_uvx_cache(monkeypatch):
    monkeypatch.setattr(service.sys, "executable", "/home/u/.cache/uv/archive-v0/abc/bin/python")
    with pytest.raises(service.ServiceError, match="Install lastseen first"):
        service.command(service.Path("/d"), "127.0.0.1", 8765)


def test_launchd(fake_env, tmp_path, monkeypatch):
    monkeypatch.setattr(service.sys, "platform", "darwin")
    msg = service.install(tmp_path / "data", "127.0.0.1", 8765)
    assert "start automatically" in msg
    plist = plistlib.loads(service.plist_path().read_bytes())
    assert plist["Label"] == "io.github.sharifrafid.lastseen"
    assert plist["KeepAlive"] is True and plist["ThrottleInterval"] == 30 and plist["StandardOutPath"] == "/dev/null"
    assert plist["ProgramArguments"][0] == "/opt/lastseen/bin/python"
    assert fake_env[-1] == ["launchctl", "bootstrap", "gui/501", str(service.plist_path())]
    info = service.info()
    assert info["installed"] and info["kind"] == "launchd"
    service.stop()
    assert fake_env[-1] == ["launchctl", "bootout", "gui/501/io.github.sharifrafid.lastseen"]
    assert service.plist_path().exists()
    service.uninstall()
    assert not service.plist_path().exists()


def test_systemd(fake_env, tmp_path, monkeypatch):
    monkeypatch.setattr(service.sys, "platform", "linux")
    service.install(tmp_path / "my data", "127.0.0.1", 8765)
    unit = service.unit_path().read_text()
    assert "ExecStart=/opt/lastseen/bin/python -m lastseen --service" in unit
    assert "'" + str(tmp_path / "my data") + "'" in unit
    assert "Restart=on-failure" in unit and "WantedBy=default.target" in unit
    assert fake_env[-2:] == [["systemctl", "--user", "enable", "lastseen"], ["systemctl", "--user", "restart", "lastseen"]]
    assert service.installed()
    service.stop()
    assert fake_env[-1] == ["systemctl", "--user", "stop", "lastseen"]
    assert "enable-linger" in service.info()["detail"]
    service.uninstall(stop=False)
    assert ["systemctl", "--user", "disable", "lastseen"] in fake_env
    assert not service.unit_path().exists()


def test_systemd_missing(fake_env, monkeypatch):
    monkeypatch.setattr(service.sys, "platform", "linux")
    monkeypatch.setattr(service.shutil, "which", lambda name: None)
    assert service.info()["supported"] is False
    with pytest.raises(service.ServiceError):
        service.install(service.Path("/d"), "127.0.0.1", 8765)


def test_windows(fake_env, tmp_path, monkeypatch):
    monkeypatch.setattr(service.sys, "platform", "win32")
    service.install(tmp_path / "My Data", "127.0.0.1", 8765)
    vbs = service.startup_path().read_text()
    assert vbs.startswith('CreateObject("WScript.Shell").Run "')
    assert f'""{tmp_path / "My Data"}""' in vbs and vbs.rstrip().endswith(", 0, False")
    assert fake_env[-1][:4] == ["popen", "/opt/lastseen/bin/python", "-m", "lastseen"]
    assert service.info()["installed"]
    service.uninstall()
    assert not service.startup_path().exists()


def test_docker(fake_env, monkeypatch):
    monkeypatch.setenv("LASTSEEN_IN_DOCKER", "1")
    info = service.info()
    assert info["supported"] is False and "Docker" in info["detail"]
    with pytest.raises(service.ServiceError):
        service.install(service.Path("/d"), "0.0.0.0", 8765)


def test_failure_message_is_trimmed(fake_env, monkeypatch, tmp_path):
    monkeypatch.setattr(service.sys, "platform", "darwin")
    monkeypatch.setattr(service.subprocess, "run",
                        lambda args, **kw: subprocess.CompletedProcess(args, 5, "", "E" * 1000))
    with pytest.raises(service.ServiceError) as err:
        service.install(tmp_path, "127.0.0.1", 8765)
    assert err.value.code == "service_failed" and len(err.value.message) == 300
