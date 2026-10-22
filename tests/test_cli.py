from __future__ import annotations

import os
import subprocess
import sys

import pytest

from lastseen import cli


def test_version(capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["--version"])
    assert e.value.code == 0
    assert capsys.readouterr().out.startswith("lastseen ")


def test_global_options_after_subcommand(tmp_path):
    args = cli.parse_args(["status", "--data-dir", str(tmp_path), "--port", "9999"])
    assert args.cmd == "status" and args.data_dir == str(tmp_path) and args.port == 9999
    args = cli.parse_args(["--port", "9998", "status"])
    assert args.port == 9998 and args.host == "127.0.0.1"


def test_status_on_empty_dir_without_pyicloud(tmp_path):
    code = (
        "import sys; from lastseen import cli\n"
        "try:\n    cli.main(['status', '--port', '1'])\n"
        "except SystemExit as e:\n    assert e.code == 0, e.code\n"
        "assert 'pyicloud' not in sys.modules\n"
    )
    env = {**os.environ, "LASTSEEN_DATA_DIR": str(tmp_path / "d")}
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=60)
    assert out.returncode == 0, out.stderr
    assert "not signed in" in out.stdout and "No history yet" in out.stdout


def test_reset_password_writes_setup_token(tmp_path, capsys):
    from lastseen import config

    d = tmp_path / "d"
    config.ensure_private_dir(d)
    config.ConfigStore(d).update(access_password_hash="x")
    with pytest.raises(SystemExit):
        cli.main(["reset-password", "--data-dir", str(d), "--host", "0.0.0.0"])
    assert config.ConfigStore(d).get("access_password_hash") is None
    token = config.setup_token(d)
    assert token and f"?setup={token}" in capsys.readouterr().out


def test_urls():
    assert cli.urls("::1", 8765) == ["http://[::1]:8765"]
    assert cli.urls("0.0.0.0", 80)[0] == "http://localhost:80"


def test_lock_is_exclusive(tmp_path):
    first = cli.acquire_lock(tmp_path / "lastseen.lock")
    assert first is not None
    code = (
        "import sys; from pathlib import Path; from lastseen import cli\n"
        f"sys.exit(0 if cli.acquire_lock(Path({str(tmp_path / 'lastseen.lock')!r})) is None else 1)\n"
    )
    assert subprocess.run([sys.executable, "-c", code], timeout=60).returncode == 0
    first.close()


def test_reset_password_always_prints_setup_link(tmp_path, capsys):
    from lastseen import config

    d = tmp_path / "d"
    config.ensure_private_dir(d)
    config.ConfigStore(d).update(access_password_hash="x")
    with pytest.raises(SystemExit):
        cli.main(["reset-password", "--data-dir", str(d)])
    token = config.setup_token(d)
    assert token and f"?setup={token}" in capsys.readouterr().out


def test_bad_port_env_only_matters_when_listening(tmp_path, monkeypatch):
    monkeypatch.setenv("LASTSEEN_PORT", "abc")
    args = cli.parse_args(["status", "--data-dir", str(tmp_path)])
    assert args.port == 8765 and args.port_error
    with pytest.raises(SystemExit, match="LASTSEEN_PORT"):
        cli.cmd_run(cli.parse_args(["--data-dir", str(tmp_path)]))


def test_allowed_hosts_flag_and_env(monkeypatch):
    monkeypatch.setenv("LASTSEEN_ALLOWED_HOSTS", "A.example, b.example")
    assert cli.parse_args([]).allowed_hosts == ["a.example", "b.example"]
    assert cli.parse_args(["--allowed-hosts", "c.example"]).allowed_hosts == ["c.example"]


def test_unusable_data_dir_is_a_friendly_error(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    with pytest.raises(SystemExit) as e:
        cli.main(["status", "--data-dir", str(blocker / "sub")])
    assert "Can't use data folder" in str(e.value.code)


def test_running_instance_checks_data_folder(app, tmp_path):
    port = app.client.port
    assert cli.running_instance(port, app.data_dir) == "same"
    assert cli.running_instance(port, tmp_path / "elsewhere") == "other"
    assert cli.running_instance(1, app.data_dir) is None


def test_stop_request(tmp_path):
    import time

    started = time.time()
    assert not cli.stop_requested(tmp_path, started)
    cli.request_stop(tmp_path)
    assert cli.stop_requested(tmp_path, started)
    assert not cli.stop_requested(tmp_path, time.time() + 1)


def test_stop_command_stops_running_instance(tmp_path):
    import time

    from lastseen import config

    d = tmp_path / "d"
    config.ensure_private_dir(d)
    # A fake home keeps `stop` away from any real autostart service.
    home = tmp_path / "home"
    home.mkdir()
    env = {**os.environ, "LASTSEEN_DATA_DIR": str(d), "LASTSEEN_NO_BROWSER": "1", "HOME": str(home),
           "USERPROFILE": str(home), "XDG_CONFIG_HOME": str(home / ".config"), "APPDATA": str(home / "AppData")}
    proc = subprocess.Popen([sys.executable, "-m", "lastseen", "--port", "0"], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            if (d / "lastseen.log").exists() and "is running" in (d / "lastseen.log").read_text():
                break
            assert proc.poll() is None
            time.sleep(0.1)
        out = subprocess.run([sys.executable, "-m", "lastseen", "stop"], env=env, capture_output=True, text=True, timeout=60)
        assert out.returncode == 0, out.stdout + out.stderr
        assert proc.wait(timeout=10) == 0
    finally:
        proc.kill()


def test_service_waiter_exits_when_autostart_is_off(tmp_path, monkeypatch):
    d = tmp_path / "d"
    lock = cli.acquire_lock((cli.config.resolve_data_dir(d)) / "lastseen.lock")
    try:
        monkeypatch.setattr(cli.service, "installed", lambda: False)
        monkeypatch.setattr(cli.signal, "signal", lambda *a: None)
        assert cli.cmd_run(cli.parse_args(["--service", "--data-dir", str(d), "--port", "0"])) == 0
    finally:
        lock.close()


def test_port_in_use_leaves_database_alone(tmp_path, monkeypatch, capsys):
    import socket

    monkeypatch.setattr(cli.signal, "signal", lambda *a: None)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        port = s.getsockname()[1]
        d = tmp_path / "d"
        code = cli.cmd_run(cli.parse_args(["--data-dir", str(d), "--port", str(port), "--no-browser"]))
    assert code == 2 and "is used by" in capsys.readouterr().out
    assert not (d / "history.db").exists()
    cli._lock_file.close()
