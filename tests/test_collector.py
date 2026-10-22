from __future__ import annotations

import threading
import time

from conftest import FakeApi, FakeDevice, FakeManager, device
from lastseen import collector as collector_mod, config, db
from lastseen.collector import Collector, collect_once


def _conn(tmp_path):
    path = tmp_path / "h.db"
    db.init(path)
    return db.connect(path)


def test_locate_then_settle_and_dedupe(tmp_path):
    conn = _conn(tmp_path)
    api = FakeApi()
    assert collect_once(api, conn, settle=5) == (2, 2)
    assert api.devices.calls == [True, False]  # locate, then wait for the fix
    api.devices.refresh = lambda locate=True: None
    assert collect_once(api, conn, settle=0.05) == (2, 0)  # same fix again is not duplicated

    devices = {d["id"]: d for d in db.list_devices(conn)}
    assert devices["b"]["owner"] == "Sam Lee" and devices["b"]["is_family"] is True
    assert devices["a"]["owner"] is None and devices["a"]["is_family"] is False
    assert devices["a"]["state"] == "located" and devices["a"]["battery_level"] == 0.5
    assert [d["id"] for d in db.list_devices(conn)] == ["a", "b"]  # own devices first
    assert len(db.recent_polls(conn)) == 2


def test_refresh_failure_mid_settle_keeps_fixes(tmp_path):
    conn = _conn(tmp_path)
    manager = FakeManager()
    manager.fail_on = 1  # the first follow-up refresh fails
    assert collect_once(FakeApi(manager), conn, settle=5) == (2, 2)
    assert db.recent_polls(conn)[0]["ok"] == 1


def test_not_sharing_family_device_not_waited_on(tmp_path):
    conn = _conn(tmp_path)
    manager = FakeManager()
    manager.devices = {
        "a": FakeDevice(device("a", "iPhone", manager.ts, finished=True)),
        "c": FakeDevice(device("c", "Mum's iPhone", None, "p2", share=False)),
    }
    manager.refresh = lambda locate=True: manager.calls.append(locate)
    start = time.monotonic()
    collect_once(FakeApi(manager), conn, settle=5)
    assert time.monotonic() - start < 1
    assert manager.calls == [True]
    states = {d["id"]: d["state"] for d in db.list_devices(conn)}
    assert states == {"a": "located", "c": "not_sharing"}


def test_device_without_location_dropped_after_first_refresh(tmp_path):
    conn = _conn(tmp_path)
    manager = FakeManager()
    manager.devices["x"] = FakeDevice({**device("x", "Mac", None), "location": None})
    start = time.monotonic()
    collect_once(FakeApi(manager), conn, settle=5)
    assert time.monotonic() - start < 1
    assert manager.calls == [True, False]
    states = {d["id"]: d["state"] for d in db.list_devices(conn)}
    assert states["x"] == "no_location"


def test_device_that_gives_up_locating_is_not_waited_on(tmp_path):
    conn = _conn(tmp_path)
    manager = FakeManager()
    mac = device("m", "MacBook", manager.ts, finished=False)
    manager.devices = {"m": FakeDevice(mac)}

    def refresh(locate=True):
        # Starts locating, then stops without a new fix (seen on Macs).
        manager.calls.append(locate)
        manager.devices["m"].data = {**mac, "isLocating": locate}

    manager.refresh = refresh
    start = time.monotonic()
    assert collect_once(FakeApi(manager), conn, settle=5) == (1, 1)
    assert time.monotonic() - start < 1
    assert manager.calls == [True, False]


def test_offline_and_null_coordinates(tmp_path):
    conn = _conn(tmp_path)
    manager = FakeManager()
    manager.devices = {
        "o": FakeDevice(device("o", "Old iPad", None, status="201")),
        "n": FakeDevice(device("n", "Watch", manager.ts, lon=None)),
    }
    assert collect_once(FakeApi(manager), conn, settle=0.05) == (2, 0)
    states = {d["id"]: d["state"] for d in db.list_devices(conn)}
    assert states["o"] == "offline"


def test_awaiting_fix_without_features():
    assert not collector_mod._awaiting_fix({"id": "z", "features": None, "deviceStatus": "200"}, None, set())


def _collector(tmp_path, factory, **cfg):
    data = tmp_path / "data"
    config.ensure_private_dir(data)
    db.init(data / "history.db")
    store = config.ConfigStore(data)
    store.update(apple_id="me@example.com", **cfg)
    stop = threading.Event()
    return Collector(data, data / "history.db", store, stop, service_factory=factory, settle=0.05), store, stop


def _wait(pred, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return False


def test_needs_login_is_persisted_and_never_retried(tmp_path):
    from pyicloud.exceptions import PyiCloudFailedLoginException

    calls = []

    def factory(apple_id, password, session_dir):
        calls.append(password)
        raise PyiCloudFailedLoginException("No password set")

    c, store, stop = _collector(tmp_path, factory)
    c.start()
    try:
        assert _wait(lambda: c.status()["state"] == "idle")
        assert c.poll_now()
        assert _wait(lambda: c.status()["state"] == "needs_login")
        nl = store.get("needs_login")
        assert nl["reason"] == "session_expired" and "Sign in again" in nl["message"]
        assert c.status()["last_poll"]["ok"] is False
        assert not c.poll_now()
        c.reschedule()
        time.sleep(0.2)
        assert calls == [None]
    finally:
        stop.set()
        c.join(5)

    # A restart stays in needs_login without contacting Apple.
    calls.clear()
    c2 = Collector(c.data_dir, c.db_path, config.ConfigStore(c.data_dir), threading.Event(), service_factory=factory)
    c2.start()
    try:
        assert _wait(lambda: c2.status()["state"] == "needs_login")
        assert calls == []
    finally:
        c2.stop.set()
        c2.join(5)


def test_network_error_keeps_api_and_backs_off(tmp_path):
    from pyicloud.exceptions import PyiCloudConnectionException

    api = FakeApi()
    made = []

    def factory(*a):
        made.append(a)
        return api

    def broken(locate=True):
        raise PyiCloudConnectionException("Request failed to iCloud")

    api.devices.refresh = broken
    c, store, stop = _collector(tmp_path, factory)
    c.start()
    try:
        assert _wait(lambda: c.status()["state"] == "idle")
        before = time.time() * 1000
        assert c.poll_now()
        assert _wait(lambda: c.status()["state"] == "retrying")
        st = c.status()
        assert st["reason"] == "network" and st["message"].startswith("Couldn't reach Apple")
        assert 55_000 < st["next_poll_at"] - before < 65_000
        assert c._api is api and len(made) == 1
        polls = db.recent_polls(db.connect(c.db_path), 5)
        assert polls[0]["error"] == st["message"]
    finally:
        stop.set()
        c.join(5)


def test_unknown_error_is_friendly(tmp_path):
    def factory(*a):
        raise RuntimeError("secret internals")

    c, store, stop = _collector(tmp_path, factory)
    c.start()
    try:
        assert _wait(lambda: c.status()["state"] == "idle")
        c.poll_now()
        assert _wait(lambda: (c.status()["last_poll"] or {}).get("ok") is False)
        assert "secret" not in c.status()["last_poll"]["error"]
        assert c.status()["reason"] == "unknown"
    finally:
        stop.set()
        c.join(5)


def test_schedule_is_wall_clock_aligned(tmp_path):
    c, store, stop = _collector(tmp_path, lambda *a: FakeApi(), interval_minutes=15)
    c.clock = lambda: 3_600_100.0
    assert c._aligned_next() == 3_600_900_000  # the next quarter hour
    store.update(interval_minutes=60)
    assert c._aligned_next() == 3_603_600_000


def test_success_and_retention_prune(tmp_path):
    c, store, stop = _collector(tmp_path, lambda *a: FakeApi(FakeManager(ts=db.now_ms())), retention_days=30)
    conn = db.connect(c.db_path)
    with conn:
        db.upsert_device(conn, {"id": "a", "name": "x", "model": None, "display_name": None, "device_class": None,
                                "owner": None, "is_family": 0, "state": None, "battery_level": None}, 1)
        old = {col: None for col in db.FIX_COLUMNS}
        old.update(device_id="a", ts=db.now_ms() - 40 * db.DAY_MS, lat=0, lon=0, polled_at=1)
        db.insert_fix(conn, old)
        db.record_poll(conn, db.now_ms() - 100 * db.DAY_MS, True)
    c.start()
    try:
        assert _wait(lambda: c.status()["state"] == "idle")
        c.poll_now()
        assert _wait(lambda: (c.status()["last_poll"] or {}).get("ok") is True)
        assert c.status()["last_success_at"]
        assert c.status()["next_poll_at"] > db.now_ms()
        assert _wait(lambda: conn.execute("SELECT MIN(ts) FROM fixes").fetchone()[0] > db.now_ms() - 31 * db.DAY_MS)
        assert conn.execute("SELECT COUNT(*) FROM polls").fetchone()[0] == 1
    finally:
        stop.set()
        c.join(5)


def test_set_service_makes_idle_without_polling(tmp_path):
    c, store, stop = _collector(tmp_path, lambda *a: FakeApi())
    api = FakeApi()
    c.set_service(api)
    st = c.status()
    assert st["state"] == "idle" and st["next_poll_at"] > db.now_ms()
    assert api.devices.calls == []
    c.set_service(None)
    assert c.status()["state"] == "not_configured"
