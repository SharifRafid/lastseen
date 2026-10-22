"""Fakes for pyicloud and a running test server. Nothing here talks to Apple."""

from __future__ import annotations

import http.client
import json
from pathlib import Path
import threading
from typing import Any

import pytest

from lastseen import collector as collector_mod, config, db, web
from lastseen.collector import Collector
from lastseen.icloud import LoginFlow


class FakeDevice:
    def __init__(self, content: dict[str, Any]):
        self.data = content


def device(dev_id: str, name: str, ts: int | None, prs: str | None = None, *, finished: bool = True,
           status: str = "200", share: bool = True, lat: float | None = 1.0, lon: float | None = 2.0) -> dict[str, Any]:
    loc = None if ts is None else {
        "latitude": lat, "longitude": lon, "timeStamp": ts, "horizontalAccuracy": 10,
        "locationFinished": finished, "positionType": "GPS", "altitude": 0.0,
    }
    return {
        "id": dev_id, "name": name, "prsId": prs, "deviceStatus": status, "fmlyShare": share,
        "deviceDisplayName": "iPhone 17", "deviceClass": "iPhone", "features": {"LOC": True},
        "batteryLevel": 0.5, "location": loc,
    }


class FakeManager:
    """Mimics FMIP: a locate request only resolves on the following refresh."""

    def __init__(self, ts: int = 1_700_000_000_000):
        self.calls: list[bool] = []
        self.ts = ts
        self.user_info = {"membersInfo": {"p2": {"firstName": "Sam", "lastName": "Lee"}}}
        self.devices = {
            "a": FakeDevice(device("a", "iPhone", ts)),
            "b": FakeDevice(device("b", "Sam's iPad", ts, "p2")),
        }
        self.fail_on: int | None = None  # index into calls that raises

    def refresh(self, locate: bool = True) -> None:
        self.calls.append(locate)
        if self.fail_on is not None and len(self.calls) - 1 == self.fail_on:
            raise ConnectionError("boom")
        if locate:
            for d in self.devices.values():
                if d.data.get("location"):
                    d.data = {**d.data, "location": {**d.data["location"], "locationFinished": False}}
        else:
            self.ts += 60_000
            for d in self.devices.values():
                c = d.data
                if c.get("location") and c["deviceStatus"] == "200" and (not c.get("prsId") or c.get("fmlyShare")):
                    d.data = {**c, "location": {**c["location"], "timeStamp": self.ts, "locationFinished": True}}


class FakeApi:
    requires_2fa = False
    requires_2sa = False
    is_trusted_session = True

    def __init__(self, manager: FakeManager | None = None):
        self.devices = manager or FakeManager()


class FakeLoginService(FakeApi):
    """A pyicloud service in the middle of a 2FA sign-in."""

    def __init__(self, method: str = "trusted_device", good_code: str = "123456", needs_2fa: bool = True):
        super().__init__()
        self.requires_2fa = needs_2fa
        self.is_trusted_session = not needs_2fa
        self.two_factor_delivery_method = method
        self.two_factor_delivery_notice = None
        self.security_key_names = ["YubiKey"] if method == "security_key" else None
        self.good_code = good_code
        self.codes: list[str] = []
        self.fido2_devices: list[Any] = []
        self.session = object()

    def validate_2fa_code(self, code: str) -> bool:
        self.codes.append(code)
        if code == self.good_code:
            self.requires_2fa = False
            self.is_trusted_session = True
            return True
        return False


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(collector_mod, "SETTLE_POLL_SECONDS", 0.01)
    monkeypatch.setenv("LASTSEEN_CREDENTIAL_STORE", "file")
    monkeypatch.delenv("LASTSEEN_IN_DOCKER", raising=False)
    monkeypatch.setenv("LASTSEEN_DATA_DIR", str(tmp_path / "default-data"))


class Client:
    def __init__(self, port: int):
        self.port = port
        self.cookie: str | None = None

    def request(self, method: str, path: str, body: Any = None, headers: dict[str, str] | None = None,
                raw: bytes | None = None) -> tuple[int, dict[str, str], bytes]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"Host": f"127.0.0.1:{self.port}"}
        if body is not None:
            raw = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        if self.cookie:
            h["Cookie"] = self.cookie
        h.update(headers or {})
        conn.request(method, path, body=raw, headers=h)
        r = conn.getresponse()
        data = r.read()
        hdrs = {k.lower(): v for k, v in r.getheaders()}
        if "set-cookie" in hdrs:
            self.cookie = hdrs["set-cookie"].split(";")[0]
        conn.close()
        return r.status, hdrs, data

    def json(self, method: str, path: str, body: Any = None, **kw: Any) -> tuple[int, Any]:
        status, _, data = self.request(method, path, body, **kw)
        return status, json.loads(data)

    def get(self, path: str) -> tuple[int, Any]:
        return self.json("GET", path)

    def post(self, path: str, body: Any = None) -> tuple[int, Any]:
        return self.json("POST", path, {} if body is None else body)


class App:
    def __init__(self, tmp_path: Path, host: str = "127.0.0.1", factory: Any = None):
        self.data_dir = tmp_path / "data"
        config.ensure_private_dir(self.data_dir)
        self.db_path = self.data_dir / "history.db"
        db.init(self.db_path)
        self.cfg = config.ConfigStore(self.data_dir)
        self.stop = threading.Event()
        self.service = FakeLoginService()
        self.factory_calls: list[tuple[str, str | None]] = []

        def default_factory(apple_id: str, password: str | None, session_dir: Path) -> Any:
            self.factory_calls.append((apple_id, password))
            return self.service

        factory = factory or default_factory
        self.collector = Collector(self.data_dir, self.db_path, self.cfg, self.stop, service_factory=factory, settle=0.5)
        self.login = LoginFlow(self.data_dir, self.cfg, self.collector, service_factory=factory)
        self.ctx = web.Context(
            data_dir=self.data_dir, db_path=self.db_path, cfg=self.cfg, collector=self.collector,
            login=self.login, host="127.0.0.1", port=0,
        )
        self.server = web.make_server(self.ctx)
        # Pretend to be bound to `host` (e.g. 0.0.0.0) without exposing a test server.
        self.ctx.host = host
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.client = Client(self.server.server_address[1])

    def close(self) -> None:
        self.stop.set()
        self.server.shutdown()
        self.server.server_close()
        if self.collector.is_alive():
            self.collector.join(5)


@pytest.fixture
def app(tmp_path):
    a = App(tmp_path)
    yield a
    a.close()


@pytest.fixture
def remote_app(tmp_path):
    a = App(tmp_path, host="0.0.0.0")
    yield a
    a.close()
