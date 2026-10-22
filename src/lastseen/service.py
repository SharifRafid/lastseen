"""Start lastseen automatically: launchd (macOS), systemd --user (Linux), Startup folder (Windows)."""

from __future__ import annotations

import functools
import os
from pathlib import Path
import plistlib
import shlex
import shutil
import subprocess
import sys
from typing import Any, Iterable

from lastseen import config

LABEL = "io.github.sharifrafid.lastseen"
DOCKER_DETAIL = (
    "Docker keeps lastseen running when it has a restart policy: docker compose sets "
    "'restart: unless-stopped'; with docker run, add --restart unless-stopped."
)


class ServiceError(Exception):
    def __init__(self, message: str, code: str = "unsupported"):
        super().__init__(message)
        self.message, self.code = message, code


def kind() -> str | None:
    if config.in_docker():
        return None
    if sys.platform == "darwin":
        return "launchd"
    if sys.platform.startswith("linux"):
        return "systemd"
    if sys.platform == "win32":
        return "windows-startup"
    return None


def _launch_agents() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def plist_path() -> Path:
    return _launch_agents() / f"{LABEL}.plist"


def unit_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    return (Path(base) if base else Path.home() / ".config") / "systemd" / "user" / "lastseen.service"


def startup_path() -> Path:
    appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "lastseen.vbs"


def _path_for(k: str | None) -> Path | None:
    return {"launchd": plist_path, "systemd": unit_path, "windows-startup": startup_path}.get(k or "", lambda: None)()


def _python(windowed: bool = False) -> str:
    exe = Path(sys.executable)
    for path in (exe, Path(sys.prefix)):
        if {"archive-v0", "builds-v0"} & set(path.parts):
            raise ServiceError("Install lastseen first (see README) to enable autostart.")
    if windowed:
        pythonw = exe.with_name("pythonw.exe")
        if pythonw.exists():
            return str(pythonw)
    return sys.executable


def command(data_dir: Path, host: str, port: int, allowed_hosts: Iterable[str] = (), windowed: bool = False) -> list[str]:
    # --service: wait for a running instance to exit instead of quitting, so
    # the service takes over cleanly from a lastseen started by hand.
    cmd = [
        _python(windowed), "-m", "lastseen", "--service", "--no-browser",
        "--data-dir", str(data_dir), "--host", host, "--port", str(port),
    ]
    hosts = ",".join(sorted(allowed_hosts))
    if hosts:
        cmd += ["--allowed-hosts", hosts]
    return cmd


def _run(args: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    if not shutil.which(args[0]):
        raise ServiceError(f"{args[0]} isn't available on this system.")
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ServiceError(f"{args[0]} failed ({type(exc).__name__}).", "service_failed") from None
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()[:300]
        raise ServiceError(detail or f"{args[0]} failed.", "service_failed")
    return result


@functools.lru_cache(maxsize=1)
def systemd_available() -> bool:
    if not shutil.which("systemctl"):
        return False
    try:
        return subprocess.run(
            ["systemctl", "--user", "show-environment"], capture_output=True, timeout=30,
        ).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def info(data_dir: Path | None = None) -> dict[str, Any]:
    k = kind()
    out: dict[str, Any] = {"supported": False, "installed": False, "kind": k, "detail": ""}
    if config.in_docker():
        out["detail"] = DOCKER_DETAIL
        return out
    if k is None:
        out["detail"] = "Automatic start isn't supported on this system; run lastseen under your own supervisor."
        return out
    if k == "systemd" and not systemd_available():
        out["detail"] = "No systemd user session; run lastseen under your own supervisor or use Docker."
        return out
    path = _path_for(k)
    out["supported"] = True
    out["installed"] = bool(path and path.exists())
    out["detail"] = {
        "launchd": "Starts lastseen in the background when you log in to this Mac.",
        "systemd": (
            "Runs lastseen as a systemd user service. To keep it running while you're logged out, "
            "run: loginctl enable-linger $USER"
        ),
        "windows-startup": "Starts lastseen in the background when you sign in to Windows.",
    }[k]
    return out


def install(data_dir: Path, host: str, port: int, allowed_hosts: Iterable[str] = ()) -> str:
    """Turn autostart on and (re)start the background instance."""
    k = kind()
    if config.in_docker():
        raise ServiceError(DOCKER_DETAIL)
    installers = {"launchd": _install_launchd, "systemd": _install_systemd, "windows-startup": _install_windows}
    if k not in installers:
        raise ServiceError("Automatic start isn't supported on this system.")
    return installers[k](command(data_dir, host, port, allowed_hosts, windowed=k == "windows-startup"))


def installed() -> bool:
    path = _path_for(kind())
    return bool(path and path.exists())


def stop() -> None:
    """Stop the background service now; it starts again at the next login."""
    k = kind()
    if not installed():
        return
    if k == "launchd":
        _run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], check=False)
    elif k == "systemd":
        _run(["systemctl", "--user", "stop", "lastseen"], check=False)
    # Windows: the running instance notices `lastseen stop` itself.


def uninstall(stop: bool = True) -> str:
    """Turn autostart off. With stop=False a running service instance keeps running."""
    k = kind()
    if k == "launchd":
        path = plist_path()
        if stop:
            _run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], check=False)
        path.unlink(missing_ok=True)
        return "lastseen won't start automatically anymore."
    if k == "systemd":
        path = unit_path()
        if path.exists():
            _run(["systemctl", "--user", "disable", *(["--now"] if stop else []), "lastseen"], check=False)
            path.unlink(missing_ok=True)
            _run(["systemctl", "--user", "daemon-reload"], check=False)
        return "lastseen won't start automatically anymore."
    if k == "windows-startup":
        startup_path().unlink(missing_ok=True)
        return "lastseen won't start automatically anymore."
    raise ServiceError(DOCKER_DETAIL if config.in_docker() else "Automatic start isn't supported on this system.")


def render_plist(cmd: list[str]) -> bytes:
    return plistlib.dumps({
        "Label": LABEL,
        "ProgramArguments": cmd,
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 30,
        "ProcessType": "Background",
        # lastseen writes its own rotating log in the data folder.
        "StandardOutPath": "/dev/null",
        "StandardErrorPath": "/dev/null",
    })


def _install_launchd(cmd: list[str]) -> str:
    uid = os.getuid()
    _run(["launchctl", "bootout", f"gui/{uid}/{LABEL}"], check=False)
    path = plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(render_plist(cmd))
    _run(["launchctl", "bootstrap", f"gui/{uid}", str(path)])
    return "lastseen will start automatically when you log in."


def render_unit(cmd: list[str]) -> str:
    return (
        "[Unit]\n"
        "Description=lastseen location history\n\n"
        "[Service]\n"
        f"ExecStart={shlex.join(cmd)}\n"
        "Restart=on-failure\n"
        "RestartSec=30\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def _install_systemd(cmd: list[str]) -> str:
    if not shutil.which("systemctl"):
        raise ServiceError("systemctl isn't available; run lastseen under your own supervisor or use Docker.")
    if _run(["systemctl", "--user", "show-environment"], check=False).returncode != 0:
        raise ServiceError("No systemd user session; run lastseen under your own supervisor or use Docker.")
    path = unit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_unit(cmd), encoding="utf-8")
    _run(["systemctl", "--user", "daemon-reload"])
    _run(["systemctl", "--user", "enable", "lastseen"])
    # restart, not start: picks up a new command line or an upgraded lastseen.
    _run(["systemctl", "--user", "restart", "lastseen"])
    return "lastseen will start automatically. To keep it running while you're logged out, run: loginctl enable-linger $USER"


def _vbs_quote(arg: str) -> str:
    return f'""{arg}""' if (" " in arg or not arg) else arg


def render_vbs(cmd: list[str]) -> str:
    line = " ".join(_vbs_quote(a) for a in cmd)
    return f'CreateObject("WScript.Shell").Run "{line}", 0, False\r\n'


def _install_windows(cmd: list[str]) -> str:
    path = startup_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_vbs(cmd), encoding="utf-8")
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        # Start now too; it waits quietly while lastseen is already running, and
        # exits if autostart is turned off again (see cli.cmd_run).
        subprocess.Popen(cmd, creationflags=flags, close_fds=True)
    except OSError as exc:
        raise ServiceError(f"Couldn't start lastseen in the background ({type(exc).__name__}).", "service_failed") from None
    return "lastseen will start automatically when you sign in to Windows."
