"""Local web UI and JSON API."""

from __future__ import annotations

import base64
from collections import deque
import csv
from dataclasses import dataclass, field
from datetime import datetime, timezone
import gzip
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
import io
import json
import logging
import os
from pathlib import Path
import re
import socket
import socketserver
import sqlite3
import threading
import time
from typing import Any, Callable, Iterable
from urllib.parse import parse_qs, urlsplit
from xml.sax.saxutils import escape, quoteattr

from lastseen import __version__, config, db, service
from lastseen.collector import Collector
from lastseen.icloud import MESSAGES, LoginError, LoginFlow

log = logging.getLogger(__name__)

COOKIE = "lastseen_session"
COOKIE_MAX_AGE = 30 * 86400
MAX_BODY = 64 * 1024
LOOPBACK_NAMES = {"localhost", "127.0.0.1", "::1"}
INT_RE = re.compile(r"^\d{1,15}$")
FIXES_DEFAULT_LIMIT, FIXES_MAX_LIMIT = 20_000, 50_000
GENERIC_POLL_ERROR = "This check failed."
XML_INVALID = re.compile("[^\u0009\u000a\u000d -퟿-�\U00010000-\U0010ffff]")
CSV_FORMULA = ("=", "+", "-", "@", "\t", "\r")


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def csp_for(index: bytes) -> str:
    scripts = re.findall(rb"<script>(.*?)</script>", index, re.S)
    if not scripts:
        raise RuntimeError("index.html has no inline <script>")
    hashes = " ".join(
        f"'sha256-{base64.b64encode(hashlib.sha256(s).digest()).decode()}'" for s in scripts
    )
    return (
        "default-src 'self'; "
        f"script-src 'self' https://cdnjs.cloudflare.com {hashes}; "
        "style-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com; "
        "img-src 'self' data: https://tile.openstreetmap.org; "
        "connect-src 'self'; base-uri 'none'; form-action 'self'; "
        "frame-ancestors 'none'; object-src 'none'"
    )


class RateLimiter:
    """At most 5 failures per IP in 15 minutes, then 60 s lockouts."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self.clock = clock
        self._fails: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def blocked(self, ip: str) -> bool:
        now = self.clock()
        with self._lock:
            q = self._fails.get(ip)
            if not q:
                return False
            while q and now - q[0] > 900:
                q.popleft()
            return len(q) >= 5 and now - q[-1] < 60

    def fail(self, ip: str) -> None:
        with self._lock:
            self._fails.setdefault(ip, deque(maxlen=20)).append(self.clock())

    def reset(self, ip: str) -> None:
        with self._lock:
            self._fails.pop(ip, None)


@dataclass
class Context:
    data_dir: Path
    db_path: Path
    cfg: config.ConfigStore
    # Set once the database is ready (cli binds the port first).
    collector: Collector = None  # type: ignore[assignment]
    login: LoginFlow = None  # type: ignore[assignment]
    host: str = "127.0.0.1"
    port: int = 8765
    allowed_hosts: set[str] = field(default_factory=set)
    docker: bool = False
    is_service: bool = False
    index: bytes = b""
    csp: str = ""
    limiter: RateLimiter = field(default_factory=RateLimiter)
    public_url: str = ""  # shown in the log next to a new setup link

    @property
    def is_local(self) -> bool:
        return config.is_loopback(self.host)


def ensure_setup_token(ctx: Context, announce: bool = False) -> str:
    """The setup token that proves the first password is set by the owner.

    Created on demand, so `lastseen reset-password` or a proxy request after
    startup never leaves a remote instance without a way to set a password.
    """
    token = config.setup_token(ctx.data_dir)
    if token is None:
        token = config.setup_token(ctx.data_dir, create=True)
        announce = True
    if announce:
        base = ctx.public_url or f"http://<lastseen's address>:{ctx.port}"
        log.warning("Open %s/?setup=%s to finish setup", base, token)
    return token or ""


class Server(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows SO_REUSEADDR lets a second socket bind a port that is in use;
    # there SO_EXCLUSIVEADDRUSE is set instead (see server_bind).
    allow_reuse_address = os.name != "nt"

    def __init__(self, ctx: Context):
        self.ctx = ctx
        if ":" in ctx.host:
            self.address_family = socket.AF_INET6
        super().__init__((ctx.host.strip("[]"), ctx.port), Handler)
        ctx.port = self.server_address[1]

    def server_bind(self) -> None:
        if self.address_family == socket.AF_INET6 and self.server_address[0] == "::":
            try:
                self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
            except (OSError, AttributeError):
                pass
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        # Skip HTTPServer.server_bind: its getfqdn() lookup can stall for seconds.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = str(self.server_address[0]), self.server_address[1]


def make_server(ctx: Context) -> Server:
    if not ctx.index:
        ctx.index = files("lastseen").joinpath("static/index.html").read_bytes()
    ctx.csp = csp_for(ctx.index)
    return Server(ctx)


def _ms_to_iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _csv_cell(value: Any) -> Any:
    if isinstance(value, str) and value.lstrip(" ").startswith(CSV_FORMULA):
        return "'" + value
    return value


def _xml(value: Any) -> str:
    return escape(XML_INVALID.sub("", str(value)))


class Handler(BaseHTTPRequestHandler):
    server: Server
    timeout = 30
    server_version = "lastseen"
    sys_version = ""
    protocol_version = "HTTP/1.0"

    # --- plumbing ---

    @property
    def ctx(self) -> Context:
        return self.server.ctx

    def log_message(self, fmt: str, *args: Any) -> None:
        path = urlsplit(getattr(self, "path", "") or "").path
        log.debug("%s %s %s", getattr(self, "command", "-"), path, args[1] if len(args) > 1 else "")

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_HEAD(self) -> None:
        self._dispatch("HEAD")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def _not_allowed(self) -> None:
        self._dispatch(self.command)

    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _not_allowed

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        self.close_connection = True
        try:
            self._error(code, "bad_request", "The request couldn't be understood.")
        except Exception:
            pass

    def _security_headers(self, path: str) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cache-Control", "no-cache" if path == "/" else "no-store")

    def _send(self, status: int, body: bytes, ctype: str, headers: dict[str, str] | None = None) -> None:
        self._started = True
        path = urlsplit(getattr(self, "path", "") or "").path
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self._security_headers(path)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        for c in getattr(self, "_cookies", []):
            self.send_header("Set-Cookie", c)
        self.end_headers()
        if getattr(self, "command", "") != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, data: Any) -> None:
        body = json.dumps(data, separators=(",", ":")).encode()
        headers = {}
        request_headers = getattr(self, "headers", None)
        accept = (request_headers.get("Accept-Encoding") or "") if request_headers else ""
        if len(body) > 1024 and "gzip" in accept:
            body = gzip.compress(body, 6)
            headers = {"Content-Encoding": "gzip", "Vary": "Accept-Encoding"}
        self._send(status, body, "application/json; charset=utf-8", headers)

    def _error(self, status: int, code: str, message: str) -> None:
        self._json(status, {"error": code, "message": message})

    # --- request pipeline ---

    def _dispatch(self, method: str) -> None:
        self._started = False
        self._cookies: list[str] = []
        try:
            url = urlsplit(self.path)
            self._path, self._query = url.path, parse_qs(url.query, keep_blank_values=True)
            self._route(method)
        except ApiError as exc:
            self._error(exc.status, exc.code, exc.message)
        except LoginError as exc:
            self._error(exc.status, exc.code, exc.message)
        except (ConnectionError, socket.timeout):
            self.close_connection = True
        except Exception:
            log.exception("Request failed: %s %s", method, urlsplit(self.path).path)
            if not self._started:
                self._error(500, "internal", "Something went wrong. See the log.")
            self.close_connection = True

    def _host_name(self) -> str:
        host = (self.headers.get("Host") or "").strip().lower()
        if host.startswith("["):
            return host[1:].split("]", 1)[0]
        return host.rsplit(":", 1)[0] if host.count(":") == 1 else host

    def _host_ok(self) -> bool:
        if not self.ctx.is_local:
            return True
        name = self._host_name()
        return bool(name) and (name in LOOPBACK_NAMES or name in self.ctx.allowed_hosts)

    def _via_proxy(self) -> bool:
        """A loopback-bound lastseen reached through a reverse proxy (LASTSEEN_ALLOWED_HOSTS)."""
        return self.ctx.is_local and self._host_name() in self.ctx.allowed_hosts - LOOPBACK_NAMES

    def _local(self) -> bool:
        """Only this computer can make the request; anything else needs a password."""
        return self.ctx.is_local and not self._via_proxy()

    def _route(self, method: str) -> None:
        path = self._path
        if not self._host_ok():
            raise ApiError(403, "bad_host", "This address isn't allowed. Set LASTSEEN_ALLOWED_HOSTS to use a custom hostname.")
        api = path.startswith("/api/")
        if method == "POST":
            self._body = self._read_json()
        if method in ("GET", "HEAD") and path == "/":
            self._send(200, self.ctx.index, "text/html; charset=utf-8", {"Content-Security-Policy": self.ctx.csp})
            return
        if method in ("GET", "HEAD") and path == "/api/health":
            self._json(200, {
                "ok": True, "app": "lastseen", "version": __version__,
                "instance": config.instance_id(self.ctx.data_dir),
            })
            return
        routes = GET_ROUTES if method == "GET" else POST_ROUTES if method == "POST" else {}
        handler = routes.get(path)
        if handler is None:
            if path in GET_ROUTES or path in POST_ROUTES:
                raise ApiError(405, "method_not_allowed", "That method isn't supported here.")
            if api:
                raise ApiError(404, "not_found", "No such API endpoint.")
            self._send(404, b"Not found\n", "text/plain; charset=utf-8")
            return
        if path not in PUBLIC and not self._auth()["logged_in"]:
            raise ApiError(401, "auth_required", "Enter the lastseen password to continue.")
        handler(self)

    def _read_json(self) -> dict[str, Any]:
        origin = self.headers.get("Origin")
        if origin is not None:
            parts = urlsplit(origin)
            if not parts.netloc or parts.netloc.lower() != (self.headers.get("Host") or "").lower():
                raise ApiError(403, "bad_origin", "This request came from another website and was blocked.")
        ctype = (self.headers.get("Content-Type") or "").lower()
        if not ctype.startswith("application/json"):
            raise ApiError(415, "json_required", "Requests must be sent as JSON.")
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ApiError(400, "bad_request", "Invalid Content-Length.") from None
        if length > MAX_BODY or length < 0:
            raise ApiError(413, "payload_too_large", "The request is too large.")
        raw = self.rfile.read(length) if length else b""
        if not raw.strip():
            return {}
        try:
            data = json.loads(raw)
        except ValueError:
            raise ApiError(400, "bad_json", "The request body isn't valid JSON.") from None
        if not isinstance(data, dict):
            raise ApiError(400, "bad_json", "The request body must be a JSON object.")
        return data

    # --- auth ---

    def _cookie_value(self) -> str | None:
        for part in (self.headers.get("Cookie") or "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == COOKIE:
                return value
        return None

    def _sign(self, expiry: int, pw_hash: str) -> str:
        bound = hashlib.sha256(pw_hash.encode()).hexdigest()[:16]
        mac = hmac.new(config.secret_key(self.ctx.data_dir), f"{expiry}.{bound}".encode(), hashlib.sha256)
        return base64.urlsafe_b64encode(mac.digest()).decode().rstrip("=")

    def _cookie_ok(self, pw_hash: str) -> bool:
        value = self._cookie_value()
        if not value or "." not in value:
            return False
        expiry, _, sig = value.partition(".")
        if not expiry.isdigit() or int(expiry) < db.now_ms():
            return False
        return hmac.compare_digest(sig, self._sign(int(expiry), pw_hash))

    def _set_session_cookie(self, pw_hash: str) -> None:
        expiry = db.now_ms() + COOKIE_MAX_AGE * 1000
        secure = "; Secure" if self.headers.get("X-Forwarded-Proto", "").lower() == "https" else ""
        self._cookies.append(
            f"{COOKIE}={expiry}.{self._sign(expiry, pw_hash)}; HttpOnly; SameSite=Strict; Path=/; Max-Age={COOKIE_MAX_AGE}{secure}"
        )

    def _clear_cookie(self) -> None:
        self._cookies.append(f"{COOKIE}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0")

    def _auth(self) -> dict[str, Any]:
        pw_hash = self.ctx.cfg.get("access_password_hash")
        local = self._local()
        required = bool(pw_hash) or not local
        logged_in = (not required) or bool(pw_hash and self._cookie_ok(pw_hash))
        if not local and not pw_hash:
            ensure_setup_token(self.ctx)
        return {
            "password_set": bool(pw_hash),
            "required": required,
            "logged_in": logged_in,
            "setup_token_required": not local and not pw_hash,
        }

    def _ip(self) -> str:
        """The client address used for rate limiting.

        Behind a reverse proxy on this computer every request comes from
        127.0.0.1, so the proxy's X-Forwarded-For (last hop) is used instead.
        """
        if self._via_proxy() and config.is_loopback(str(self.client_address[0])):
            forwarded = (self.headers.get("X-Forwarded-For") or "").split(",")[-1].strip()
            if forwarded:
                return forwarded[:64]
        return str(self.client_address[0])

    def _check_rate(self) -> None:
        if self.ctx.limiter.blocked(self._ip()):
            raise ApiError(429, "too_many_attempts", "Too many attempts. Wait a minute and try again.")

    # --- params ---

    def _int(self, name: str, default: int | None = None) -> int | None:
        values = self._query.get(name)
        if not values:
            return default
        if not INT_RE.match(values[0]):
            raise ApiError(400, "bad_param", f"'{name}' must be a whole number.")
        return int(values[0])

    def _device_ids(self) -> list[str] | None:
        ids = self._query.get("device")
        if not ids:
            return None
        if len(ids) > 200 or any(not 1 <= len(i) <= 200 for i in ids):
            raise ApiError(400, "bad_param", "Invalid device list.")
        return ids

    def _range(self) -> tuple[list[str] | None, int | None, int | None]:
        return self._device_ids(), self._int("start"), self._int("end")

    # --- GET ---

    def get_status(self) -> None:
        auth = self._auth()
        if not auth["logged_in"]:
            self._json(200, {"version": __version__, "auth": auth, "docker": self.ctx.docker})
            return
        ctx, cfg = self.ctx, self.ctx.cfg.data
        auth["can_remove_password"] = self._local()
        conn = db.connect(ctx.db_path)
        try:
            data = db.counts(conn)
        finally:
            conn.close()
        self._json(200, {
            "version": __version__,
            "auth": auth,
            "bind": {"host": ctx.host, "port": ctx.port, "local": self._local()},
            "docker": ctx.docker,
            "account": {
                "configured": bool(cfg.get("apple_id")),
                "apple_id": cfg.get("apple_id"),
                "credential_store": cfg.get("credential_store"),
            },
            "collector": ctx.collector.status(),
            "settings": {"interval_minutes": cfg["interval_minutes"], "retention_days": cfg["retention_days"]},
            "service": service.info(ctx.data_dir),
            "data": {"dir": str(ctx.data_dir), **data},
        })

    def get_devices(self) -> None:
        with self._db() as conn:
            self._json(200, db.list_devices(conn))

    def get_fixes(self) -> None:
        ids, start, end = self._range()
        limit = min(max(self._int("limit", FIXES_DEFAULT_LIMIT) or 1, 1), FIXES_MAX_LIMIT)
        with self._db() as conn:
            fixes, truncated = db.query_fixes(conn, ids, start, end, limit)
        self._json(200, {"fixes": fixes, "truncated": truncated, "limit": limit})

    def get_polls(self) -> None:
        limit = min(max(self._int("limit", 20) or 1, 1), 100)
        known = set(MESSAGES.values())
        with self._db() as conn:
            polls = db.recent_polls(conn, limit)
        self._json(200, [
            {
                "polled_at": p["polled_at"], "ok": bool(p["ok"]), "devices": p["devices"],
                "new_fixes": p["new_fixes"],
                "error": None if p["ok"] else (p["error"] if p["error"] in known else GENERIC_POLL_ERROR),
            }
            for p in polls
        ])

    def get_export(self) -> None:
        fmt = (self._query.get("format") or ["csv"])[0]
        if fmt not in ("csv", "gpx"):
            raise ApiError(400, "bad_param", "'format' must be gpx or csv.")
        ids, start, end = self._range()
        conn = db.connect(self.ctx.db_path)
        try:
            first, last = db.fix_span(conn, ids, start, end)
            if first is None:
                name = f"lastseen-empty.{fmt}"
            else:
                day = lambda ms: datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d")  # noqa: E731
                name = f"lastseen-{day(first)}_to_{day(last)}.{fmt}"
            rows = db.iter_fixes_full(conn, ids, start, end, by_device=fmt == "gpx")
            chunks = self._gpx(rows) if fmt == "gpx" else self._csv(rows)
            self._stream(chunks, "application/gpx+xml" if fmt == "gpx" else "text/csv; charset=utf-8", name)
        finally:
            conn.close()

    def _stream(self, chunks: Iterable[str], ctype: str, filename: str) -> None:
        self._started = True
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self._security_headers(self._path)
        self.end_headers()
        buf: list[str] = []
        size = 0
        for chunk in chunks:
            buf.append(chunk)
            size += len(chunk)
            if size >= 65536:
                self.wfile.write("".join(buf).encode())
                buf, size = [], 0
        self.wfile.write("".join(buf).encode())

    @staticmethod
    def _csv(rows: Iterable[dict[str, Any]]) -> Iterable[str]:
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(["device_name", "owner", "time_utc", *db.FIX_COLUMNS])
        yield "﻿" + out.getvalue()
        for r in rows:
            out.seek(0)
            out.truncate()
            writer.writerow([
                _csv_cell(v) for v in
                [r.get("device_name"), r.get("owner"), _ms_to_iso(r["ts"]), *(r.get(c) for c in db.FIX_COLUMNS)]
            ])
            yield out.getvalue()

    @staticmethod
    def _gpx(rows: Iterable[dict[str, Any]]) -> Iterable[str]:
        yield (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<gpx version="1.1" creator="lastseen" xmlns="http://www.topografix.com/GPX/1/1">\n'
        )
        current = None
        for p in rows:
            if p["device_id"] != current:
                if current is not None:
                    yield "</trkseg></trk>\n"
                current = p["device_id"]
                desc = f"<desc>{_xml(p['owner'])}</desc>" if p.get("owner") else ""
                yield f"<trk><name>{_xml(p.get('device_name') or current)}</name>{desc}<trkseg>\n"
            ele = f"<ele>{float(p['altitude'])}</ele>" if p.get("altitude") is not None else ""
            yield (
                f"<trkpt lat={quoteattr(str(float(p['lat'])))} lon={quoteattr(str(float(p['lon'])))}>"
                f"{ele}<time>{_ms_to_iso(p['ts'])}</time></trkpt>\n"
            )
        if current is not None:
            yield "</trkseg></trk>\n"
        yield "</gpx>\n"

    # --- POST: UI password ---

    def post_session_login(self) -> None:
        self._check_rate()
        pw_hash = self.ctx.cfg.get("access_password_hash")
        if not pw_hash:
            raise ApiError(409, "no_password", "No lastseen password is set.")
        if not config.verify_password(self._body.get("password"), pw_hash):
            self.ctx.limiter.fail(self._ip())
            raise ApiError(401, "wrong_password", "That password isn't right.")
        self.ctx.limiter.reset(self._ip())
        self._set_session_cookie(pw_hash)
        self._json(200, {"ok": True})

    def post_session_logout(self) -> None:
        self._clear_cookie()
        self._json(200, {"ok": True})

    def post_access_password(self) -> None:
        ctx, body = self.ctx, self._body
        if "password" not in body:
            raise ApiError(400, "missing_fields", "Enter a password.")
        new = body.get("password")
        pw_hash = ctx.cfg.get("access_password_hash")
        auth = self._auth()
        self._check_rate()

        def check_current() -> None:
            if not auth["logged_in"]:
                raise ApiError(401, "auth_required", "Enter the lastseen password to continue.")
            if not config.verify_password(body.get("current_password"), pw_hash):
                ctx.limiter.fail(self._ip())
                raise ApiError(401, "wrong_password", "The current password isn't right.")

        if new is None:
            if not pw_hash:
                self._json(200, {"ok": True})
                return
            if not self._local():
                raise ApiError(403, "cannot_remove_remote", "A password is required when lastseen is reachable from other computers.")
            check_current()
            ctx.cfg.update(access_password_hash=None)
            self._clear_cookie()
            self._json(200, {"ok": True})
            return
        if not isinstance(new, str) or not 8 <= len(new) <= 256:
            raise ApiError(400, "weak_password", "Use at least 8 characters (and at most 256).")
        if pw_hash:
            check_current()
        elif auth["setup_token_required"]:
            token = ensure_setup_token(ctx)
            given = body.get("setup_token")
            if not token or not isinstance(given, str) or not hmac.compare_digest(given.encode(), token.encode()):
                ctx.limiter.fail(self._ip())
                raise ApiError(403, "bad_setup_token", "The setup link isn't valid. Copy the latest one from lastseen's log, or run lastseen reset-password to get a new one.")
        new_hash = config.hash_password(new)
        ctx.cfg.update(access_password_hash=new_hash)
        config.delete_setup_token(ctx.data_dir)
        ctx.limiter.reset(self._ip())
        self._set_session_cookie(new_hash)
        self._json(200, {"ok": True})

    # --- POST: Apple Account ---

    def post_icloud_login(self) -> None:
        self._json(200, self.ctx.login.start(self._body.get("apple_id"), self._body.get("password")))

    def post_icloud_code(self) -> None:
        self._json(200, self.ctx.login.submit_code(self._body.get("code")))

    def post_icloud_resend(self) -> None:
        self._json(200, self.ctx.login.resend())

    def post_icloud_security_key(self) -> None:
        self._json(200, self.ctx.login.security_key())

    def post_icloud_logout(self) -> None:
        if self.ctx.collector.status()["state"] == "polling":
            raise ApiError(409, "busy", "A check is running. Try again in a minute.")
        self.ctx.login.logout()
        self._json(200, {"ok": True})

    # --- POST: collection ---

    def post_poll(self) -> None:
        cfg = self.ctx.cfg.data
        if not cfg.get("apple_id"):
            raise ApiError(409, "not_configured", "Sign in to your Apple Account first.")
        if cfg.get("needs_login"):
            raise ApiError(409, "needs_login", "Sign in to your Apple Account again first.")
        if not self.ctx.collector.poll_now():
            raise ApiError(409, "busy", "A check is already running.")
        self._json(202, {"ok": True})

    def post_settings(self) -> None:
        changes: dict[str, int] = {}
        for key, ok in (
            ("interval_minutes", lambda v: 5 <= v <= 1440),
            ("retention_days", lambda v: 0 <= v <= 3650),
        ):
            if key in self._body:
                value = self._body[key]
                if isinstance(value, bool) or not isinstance(value, int) or not ok(value):
                    raise ApiError(400, "bad_setting", f"'{key}' is out of range.")
                changes[key] = value
        cfg = self.ctx.cfg.update(**changes) if changes else self.ctx.cfg.data
        if "interval_minutes" in changes:
            self.ctx.collector.reschedule()
        self._json(200, {"settings": {"interval_minutes": cfg["interval_minutes"], "retention_days": cfg["retention_days"]}})

    def post_history_delete(self) -> None:
        if self._body.get("confirm") != "DELETE":
            raise ApiError(400, "confirm_required", "Type DELETE to confirm.")
        with self._db() as conn:
            db.delete_history(conn)
        log.info("All history deleted")
        self._json(200, {"ok": True})

    def post_service_install(self) -> None:
        ctx = self.ctx
        try:
            if not (ctx.is_service and service.info(ctx.data_dir)["installed"]):
                service.install(ctx.data_dir, ctx.host, ctx.port, ctx.allowed_hosts)
        except service.ServiceError as exc:
            raise ApiError(500 if exc.code == "service_failed" else 400, exc.code, exc.message) from None
        self._json(200, {"service": service.info(ctx.data_dir)})

    def post_service_uninstall(self) -> None:
        ctx = self.ctx
        try:
            # Don't stop ourselves mid-response when we are the service.
            service.uninstall(stop=not ctx.is_service)
        except service.ServiceError as exc:
            raise ApiError(500 if exc.code == "service_failed" else 400, exc.code, exc.message) from None
        self._json(200, {"service": service.info(ctx.data_dir)})

    def _db(self) -> _Conn:
        return _Conn(self.ctx.db_path)


class _Conn:
    def __init__(self, path: Path):
        self.path = path

    def __enter__(self) -> sqlite3.Connection:
        self.conn = db.connect(self.path)
        return self.conn

    def __exit__(self, *exc: Any) -> None:
        self.conn.close()


GET_ROUTES: dict[str, Callable[[Handler], None]] = {
    "/api/status": Handler.get_status,
    "/api/devices": Handler.get_devices,
    "/api/fixes": Handler.get_fixes,
    "/api/polls": Handler.get_polls,
    "/api/export": Handler.get_export,
}
POST_ROUTES: dict[str, Callable[[Handler], None]] = {
    "/api/session/login": Handler.post_session_login,
    "/api/session/logout": Handler.post_session_logout,
    "/api/access-password": Handler.post_access_password,
    "/api/icloud/login": Handler.post_icloud_login,
    "/api/icloud/code": Handler.post_icloud_code,
    "/api/icloud/resend": Handler.post_icloud_resend,
    "/api/icloud/security-key": Handler.post_icloud_security_key,
    "/api/icloud/logout": Handler.post_icloud_logout,
    "/api/poll": Handler.post_poll,
    "/api/settings": Handler.post_settings,
    "/api/history/delete": Handler.post_history_delete,
    "/api/service/install": Handler.post_service_install,
    "/api/service/uninstall": Handler.post_service_uninstall,
}
# Reachable without a session cookie (each checks what it needs itself).
PUBLIC = {"/api/status", "/api/session/login", "/api/session/logout", "/api/access-password"}
