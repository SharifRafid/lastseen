"""Command line: `lastseen` runs the web app and collector; a few helper commands."""

from __future__ import annotations

import argparse
from datetime import datetime
import errno
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import signal
import socket
import sqlite3
import sys
import threading
import time
from typing import IO, Any
import urllib.request
import webbrowser

from lastseen import __version__, config, db, service

log = logging.getLogger("lastseen")
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_lock_file: IO[Any] | None = None
DEFAULT_PORT = 8765
STOP_FILE = "stop-request"
WAIT_SECONDS = 2


def _env_port() -> int | None:
    raw = os.environ.get("LASTSEEN_PORT", str(DEFAULT_PORT))
    try:
        port = int(raw)
    except ValueError:
        return None
    return port if 0 <= port <= 65535 else None


def build_parser() -> argparse.ArgumentParser:
    # Options shared by every command, accepted before or after the subcommand.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--data-dir", default=argparse.SUPPRESS, help="where history and settings live")
    common.add_argument("--host", default=argparse.SUPPRESS, help="address to listen on (default 127.0.0.1)")
    common.add_argument("--port", type=int, default=argparse.SUPPRESS, help="port to listen on (default 8765)")
    common.add_argument(
        "--allowed-hosts", default=argparse.SUPPRESS, metavar="NAMES",
        help="extra host names (comma-separated) accepted behind a reverse proxy",
    )
    common.add_argument("--no-browser", action="store_true", default=argparse.SUPPRESS, help="don't open a browser")
    common.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS, help="more detailed logs")
    common.add_argument("--service", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    parser = argparse.ArgumentParser(
        prog="lastseen", parents=[common],
        description="A private location history for your Apple devices. Run without a command to start it.",
    )
    parser.add_argument("--version", action="version", version=f"lastseen {__version__}")
    sub = parser.add_subparsers(dest="cmd", metavar="command")
    sub.add_parser("status", parents=[common], help="show the data folder, account, service and recent checks")
    svc = sub.add_parser("service", parents=[common], help="start lastseen automatically at login")
    svc.add_argument("action", choices=["install", "uninstall", "status"])
    sub.add_parser("reset-password", parents=[common], help="remove the lastseen access password")
    sub.add_parser("stop", parents=[common], help="stop a running lastseen (and its background service)")
    return parser


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    args = build_parser().parse_args(argv)
    defaults = {
        "data_dir": None,
        "host": os.environ.get("LASTSEEN_HOST") or "127.0.0.1",
        "port": None,
        "allowed_hosts": os.environ.get("LASTSEEN_ALLOWED_HOSTS", ""),
        "no_browser": os.environ.get("LASTSEEN_NO_BROWSER") == "1" or config.in_docker(),
        "verbose": False,
        "service": False,
        "cmd": None,
    }
    for key, value in defaults.items():
        if not hasattr(args, key):
            setattr(args, key, value)
    args.port_error = None
    if args.port is None:
        args.port = _env_port()
        if args.port is None:
            # Only fatal for commands that listen on the port (see _need_port).
            args.port_error = f"LASTSEEN_PORT must be a port number, not {os.environ.get('LASTSEEN_PORT')!r}."
            args.port = DEFAULT_PORT
    args.allowed_hosts = sorted({h.strip().lower() for h in args.allowed_hosts.split(",") if h.strip()})
    return args


def _need_port(args: argparse.Namespace) -> None:
    if args.port_error:
        sys.exit(args.port_error)


def _data_dir(args: argparse.Namespace) -> Path:
    try:
        return config.resolve_data_dir(args.data_dir)
    except OSError as exc:
        where = exc.filename or args.data_dir or "the data folder"
        hint = (
            " In Docker, make the mounted folder writable by user id 1000 (sudo chown 1000 <folder>)."
            if config.in_docker() else " Choose another folder with --data-dir."
        )
        sys.exit(f"Can't use data folder {where}: {exc.strerror or exc}.{hint}")


def setup_logging(verbose: bool) -> None:
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    if sys.stderr is not None:  # None under pythonw.exe
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        root.addHandler(handler)
    if not verbose:
        for noisy in ("pyicloud", "urllib3"):
            logging.getLogger(noisy).setLevel(logging.WARNING)


def add_file_logging(data_dir: Path) -> None:
    handler = RotatingFileHandler(data_dir / "lastseen.log", maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    logging.getLogger().addHandler(handler)


def running_instance(port: int, data_dir: Path) -> str | None:
    """"same" if this data folder's lastseen answers on `port`, "other" for another lastseen."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2) as r:
            health = json.loads(r.read())
    except Exception:
        return None
    if not isinstance(health, dict) or health.get("app") != "lastseen":
        return None
    return "same" if health.get("instance") == config.instance_id(data_dir) else "other"


def request_stop(data_dir: Path) -> None:
    config.write_private(data_dir / STOP_FILE, repr(time.time()))


def stop_requested(data_dir: Path, started: float) -> bool:
    """True when `lastseen stop` ran since this process started."""
    try:
        return float((data_dir / STOP_FILE).read_text(encoding="utf-8")) >= started
    except (OSError, ValueError):
        return False


def acquire_lock(path: Path) -> IO[Any] | None:
    f = open(path, "a+")  # noqa: SIM115 - held for the life of the process
    try:
        if os.name == "nt":
            import msvcrt

            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


def lan_ip() -> str | None:
    # connect() on a UDP socket picks the outgoing interface without sending anything.
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 80))
            return str(s.getsockname()[0])
    except OSError:
        return None


def urls(host: str, port: int) -> list[str]:
    if host in ("0.0.0.0", "::", ""):
        out = [f"http://localhost:{port}"]
        ip = None if config.in_docker() else lan_ip()
        if ip:
            out.append(f"http://{ip}:{port}")
        return out
    shown = f"[{host.strip('[]')}]" if ":" in host else host
    return [f"http://{shown}:{port}"]


def _can_open_browser(args: argparse.Namespace) -> bool:
    if args.no_browser or config.in_docker() or not config.is_loopback(args.host):
        return False
    if sys.platform.startswith("linux"):
        return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    return True


def cmd_run(args: argparse.Namespace) -> int:
    global _lock_file
    from lastseen import web
    from lastseen.collector import Collector
    from lastseen.icloud import LoginFlow

    _need_port(args)
    started = time.time()
    stop = threading.Event()
    for name in ("SIGTERM", "SIGINT", "SIGBREAK"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), lambda *_: stop.set())

    data_dir = _data_dir(args)
    browse_url = f"http://127.0.0.1:{args.port}/"
    if not args.service:
        running = running_instance(args.port, data_dir)
        if running == "same":
            print(f"lastseen is already running at {browse_url}")
            if _can_open_browser(args):
                webbrowser.open(browse_url)
            return 0
        if running == "other":
            print(f"Port {args.port} is used by another lastseen with a different data folder. Use --port {args.port + 1}.")
            return 1

    waiting = False
    while (_lock_file := acquire_lock(data_dir / "lastseen.lock")) is None:
        if not args.service:
            print(f"Another lastseen is using {data_dir}")
            return 1
        # A background instance waits here for one started by hand to exit.
        # It gives up when autostart was turned off or `lastseen stop` ran.
        if not service.installed() or stop_requested(data_dir, started):
            return 0
        if not waiting:
            log.info("Another lastseen is running; waiting to take over when it stops")
            waiting = True
        if stop.wait(WAIT_SECONDS):
            return 0

    add_file_logging(data_dir)
    db_path = data_dir / "history.db"
    cfg = config.ConfigStore(data_dir)
    docker = config.in_docker()
    ctx = web.Context(
        data_dir=data_dir, db_path=db_path, cfg=cfg,
        host=args.host, port=args.port, allowed_hosts=set(args.allowed_hosts), docker=docker, is_service=args.service,
    )
    try:
        server = web.make_server(ctx)
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE or getattr(exc, "winerror", None) in (10013, 10048):
            msg = f"Port {args.port} is used by another app. Start with --port {args.port + 1} (or set LASTSEEN_PORT)."
        else:
            msg = f"Couldn't listen on {args.host}:{args.port} ({exc.strerror or exc})."
        print(msg)
        log.error("%s", msg)
        return 2
    try:
        db.init(db_path)
        ctx.collector = Collector(data_dir, db_path, cfg, stop)
    except (OSError, sqlite3.Error) as exc:
        server.server_close()
        msg = f"Can't open the history database in {data_dir}: {exc}"
        print(msg)
        log.error("%s", msg)
        return 2
    ctx.login = LoginFlow(data_dir, cfg, ctx.collector, docker=docker)

    ctx.collector.start()
    threading.Thread(target=server.serve_forever, name="web", daemon=True).start()

    links = urls(args.host, ctx.port)
    ctx.public_url = links[-1]
    log.info("lastseen %s is running at %s (data: %s)", __version__, " and ".join(links), data_dir)
    if not ctx.is_local:
        if cfg.get("access_password_hash"):
            config.delete_setup_token(data_dir)
        else:
            web.ensure_setup_token(ctx, announce=True)
    if _can_open_browser(args):
        webbrowser.open(f"http://127.0.0.1:{ctx.port}/")

    while not stop.wait(1):
        if stop_requested(data_dir, started):
            log.info("Stop requested")
            stop.set()
    log.info("Stopping")
    server.shutdown()
    server.server_close()
    ctx.collector.join(15)
    return 0


def _fmt(ms: int | None) -> str:
    return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M") if ms else "-"


def cmd_status(args: argparse.Namespace) -> int:
    data_dir = _data_dir(args)
    cfg = config.ConfigStore(data_dir).data
    info = service.info(data_dir)
    print(f"Data folder:  {data_dir}")
    print(f"Apple ID:     {cfg.get('apple_id') or 'not signed in'}")
    if cfg.get("needs_login"):
        print(f"              {cfg['needs_login'].get('message') or 'Sign in again.'}")
    print(f"Checks every: {cfg.get('interval_minutes')} min")
    autostart = "on" if info["installed"] else ("off" if info["supported"] else "not available")
    print(f"Autostart:    {autostart}")
    running = running_instance(args.port, data_dir) == "same"
    print(f"Running:      {'yes, http://127.0.0.1:%d' % args.port if running else 'no'}")
    db_path = data_dir / "history.db"
    if not db_path.exists():
        print("\nNo history yet.")
        return 0
    from lastseen.icloud import MESSAGES

    conn = db.connect(db_path)
    try:
        counts = db.counts(conn)
        polls = db.recent_polls(conn, 10)
    except Exception:
        print("\nThe history database couldn't be read.")
        return 1
    finally:
        conn.close()
    print(f"History:      {counts['fixes']} locations from {counts['devices']} devices since {_fmt(counts['since'])}")
    print("\nRecent checks:")
    if not polls:
        print("  none yet")
    known = set(MESSAGES.values())
    for p in polls:
        if p["ok"]:
            result = f"{p['devices']} devices, {p['new_fixes']} new locations"
        else:
            result = "failed: " + (p["error"] if p["error"] in known else "see lastseen.log")
        print(f"  {_fmt(p['polled_at'])}  {result}")
    return 0


def cmd_service(args: argparse.Namespace) -> int:
    data_dir = _data_dir(args)
    try:
        if args.action == "install":
            _need_port(args)
            print(service.install(data_dir, args.host, args.port, args.allowed_hosts))
        elif args.action == "uninstall":
            print(service.uninstall())
    except service.ServiceError as exc:
        print(exc.message)
        return 1
    info = service.info(data_dir)
    state = "installed" if info["installed"] else "not installed"
    print(f"Autostart ({info['kind'] or 'unsupported'}): {state}. {info['detail']}")
    # Exit code 3 for "not installed" lets scripts (the installers) check it.
    return 0 if info["installed"] or args.action != "status" else 3


def cmd_reset_password(args: argparse.Namespace) -> int:
    data_dir = _data_dir(args)
    config.ConfigStore(data_dir).update(access_password_hash=None)
    # The running lastseen may be reachable from the network even when this
    # command's own --host is the default, so always leave a setup link.
    token = config.setup_token(data_dir, create=True)
    print("The lastseen access password was removed.")
    print("On this computer, open lastseen to choose a new one (or keep using it without a password).")
    if config.is_loopback(args.host):
        print(f"From another device, open: http://<this computer's address>:{args.port}/?setup={token}")
    else:
        print(f"Choose a new one at {urls(args.host, args.port)[-1]}/?setup={token}")
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    data_dir = _data_dir(args)
    request_stop(data_dir)
    service.stop()
    deadline = time.time() + 20
    while time.time() < deadline:
        lock = acquire_lock(data_dir / "lastseen.lock")
        if lock is not None:
            lock.close()
            if service.kind() == "windows-startup":
                # A waiting background instance holds no lock; give it time to notice.
                time.sleep(WAIT_SECONDS + 1)
            print("lastseen is stopped.")
            if service.installed():
                print("It starts again at your next login (or run: lastseen service install).")
            return 0
        time.sleep(0.5)
    print(f"lastseen didn't stop within 20 seconds (data folder {data_dir}).")
    return 1


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    if os.name != "nt":
        os.umask(0o077)
    setup_logging(args.verbose)
    commands = {
        None: cmd_run, "status": cmd_status, "service": cmd_service,
        "reset-password": cmd_reset_password, "stop": cmd_stop,
    }
    try:
        code = commands[args.cmd](args)
    except KeyboardInterrupt:
        code = 130
    sys.exit(code)
