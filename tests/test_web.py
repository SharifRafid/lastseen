from __future__ import annotations

import base64
import gzip
import hashlib
import json
import re

import pytest

from lastseen import db, web


def _seed(app, n=3, device_name="iPhone", owner=None, altitude=None):
    conn = db.connect(app.db_path)
    with conn:
        db.upsert_device(conn, {"id": "a", "name": device_name, "model": None, "display_name": "iPhone 17",
                                "device_class": "iPhone", "owner": owner, "is_family": 0, "state": "located",
                                "battery_level": 0.5}, 1)
        for i in range(n):
            fix = {c: None for c in db.FIX_COLUMNS}
            fix.update(device_id="a", ts=1_700_000_000_000 + i * 60_000, lat=1.0 + i, lon=2.0, polled_at=1,
                       altitude=altitude, position_type="=cmd()" if i == 0 else "GPS")
            db.insert_fix(conn, fix)
    conn.close()


def test_health_and_head(app):
    status, data = app.client.get("/api/health")
    assert status == 200 and data["app"] == "lastseen"
    status, headers, body = app.client.request("HEAD", "/api/health")
    assert status == 200 and body == b""
    status, _, _ = app.client.request("HEAD", "/api/devices")
    assert status == 405
    status, headers, _ = app.client.request("PUT", "/api/devices")
    assert status == 405 and headers["content-type"].startswith("application/json")


def test_index_security_headers_and_csp_hash(app):
    status, headers, body = app.client.request("GET", "/")
    assert status == 200
    script = re.search(rb"<script>(.*?)</script>", body, re.S).group(1)
    digest = base64.b64encode(hashlib.sha256(script).digest()).decode()
    assert f"'sha256-{digest}'" in headers["content-security-policy"]
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-frame-options"] == "DENY"
    assert headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert headers["cache-control"] == "no-cache"
    assert "access-control-allow-origin" not in headers
    _, h2, _ = app.client.request("GET", "/api/devices")
    assert h2["cache-control"] == "no-store"


def test_missing_inline_script_raises():
    with pytest.raises(RuntimeError):
        web.csp_for(b"<html><script src='x'></script></html>")


def test_unknown_paths(app):
    assert app.client.get("/api/nope")[0] == 404
    status, headers, _ = app.client.request("GET", "/nope")
    assert status == 404 and headers["content-type"].startswith("text/plain")


def test_host_check(app):
    status, data = app.client.json("GET", "/api/status", headers={"Host": "evil.example"})
    assert status == 403 and data["error"] == "bad_host"
    assert app.client.json("GET", "/api/status", headers={"Host": f"localhost:{app.client.port}"})[0] == 200
    app.ctx.allowed_hosts = {"lastseen.home"}
    assert app.client.json("GET", "/api/status", headers={"Host": "lastseen.home"})[0] == 200


def test_post_requires_json_and_same_origin(app):
    status, _, body = app.client.request("POST", "/api/poll", raw=b"x", headers={"Content-Type": "text/plain"})
    assert status == 415 and json.loads(body)["error"] == "json_required"
    status, data = app.client.json("POST", "/api/poll", {}, headers={"Origin": "http://evil.example"})
    assert status == 403 and data["error"] == "bad_origin"
    port = app.client.port
    status, data = app.client.json("POST", "/api/poll", {}, headers={"Origin": f"http://127.0.0.1:{port}"})
    assert data["error"] == "not_configured"
    status, _, _ = app.client.request("POST", "/api/poll", raw=b"x" * 70000, headers={"Content-Type": "application/json"})
    assert status == 413


def test_status_shapes(app):
    status, data = app.client.get("/api/status")
    assert status == 200
    assert data["auth"] == {"password_set": False, "required": False, "logged_in": True,
                            "setup_token_required": False, "can_remove_password": True}
    assert data["account"]["configured"] is False
    assert data["collector"]["state"] == "not_configured"
    assert data["settings"] == {"interval_minutes": 15, "retention_days": 0}
    assert data["bind"]["local"] is True and "supported" in data["service"]
    assert data["data"]["fixes"] == 0


def test_local_password_and_cookie(app):
    c = app.client
    assert c.post("/api/access-password", {"password": "short"})[1]["error"] == "weak_password"
    assert c.post("/api/access-password", {"password": "correct horse"}) == (200, {"ok": True})
    first_cookie = c.cookie
    assert c.get("/api/devices")[0] == 200
    c.cookie = None
    status, data = c.get("/api/devices")
    assert status == 401 and data["error"] == "auth_required"
    reduced = c.get("/api/status")[1]
    assert set(reduced) == {"version", "auth", "docker"} and reduced["auth"]["password_set"] is True
    assert c.post("/api/session/login", {"password": "nope"})[1]["error"] == "wrong_password"
    assert c.post("/api/session/login", {"password": "correct horse"})[0] == 200
    assert c.get("/api/devices")[0] == 200
    # Changing the password invalidates old cookies.
    assert c.post("/api/access-password", {"password": "battery staple", "current_password": "wrong"})[0] == 401
    assert c.post("/api/access-password", {"password": "battery staple", "current_password": "correct horse"})[0] == 200
    assert c.get("/api/devices")[0] == 200
    c.cookie = first_cookie
    assert c.get("/api/devices")[0] == 401
    c.post("/api/session/login", {"password": "battery staple"})
    assert c.post("/api/access-password", {"password": None, "current_password": "battery staple"})[0] == 200
    c.cookie = None
    assert c.get("/api/devices")[0] == 200


def test_rate_limit(app):
    c = app.client
    c.post("/api/access-password", {"password": "correct horse"})
    c.cookie = None
    for _ in range(5):
        assert c.post("/api/session/login", {"password": "nope"})[0] == 401
    status, data = c.post("/api/session/login", {"password": "correct horse"})
    assert status == 429 and data["error"] == "too_many_attempts"


def test_remote_requires_setup_token(remote_app):
    from lastseen import config

    c = remote_app.client
    status, data = c.get("/api/status")
    assert data["auth"] == {"password_set": False, "required": True, "logged_in": False, "setup_token_required": True}
    assert c.get("/api/devices")[0] == 401
    assert c.json("GET", "/api/status", headers={"Host": "evil.example"})[0] == 200  # no host check off-loopback
    token = config.setup_token(remote_app.data_dir, create=True)
    status, data = c.post("/api/access-password", {"password": "correct horse", "setup_token": "wrong"})
    assert status == 403 and data["error"] == "bad_setup_token"
    assert c.post("/api/access-password", {"password": "correct horse", "setup_token": token})[0] == 200
    assert not (remote_app.data_dir / "setup-token").exists()
    assert c.get("/api/devices")[0] == 200
    status, data = c.post("/api/access-password", {"password": None, "current_password": "correct horse"})
    assert status == 403 and data["error"] == "cannot_remove_remote"
    assert c.get("/api/status")[1]["auth"]["can_remove_password"] is False


def test_fixes_validation_truncation_and_gzip(app):
    _seed(app, n=5)
    c = app.client
    status, data = c.get("/api/fixes?device=a&limit=2")
    assert data["truncated"] is True and data["limit"] == 2
    assert [f["ts"] for f in data["fixes"]] == [1_700_000_180_000, 1_700_000_240_000]
    assert set(data["fixes"][0]) == {"device_id", "ts", "lat", "lon", "h_accuracy", "battery_level", "is_old", "position_type"}
    for bad in ("start=abc", "end=-1", "limit=1e5", "start=" + "9" * 30):
        status, data = c.get(f"/api/fixes?{bad}")
        assert status == 400 and data["error"] == "bad_param"
    assert c.get("/api/fixes?" + "&".join(["device=x"] * 201))[0] == 400
    status, data = c.get("/api/fixes?start=1700000060000&end=1700000120000")
    assert len(data["fixes"]) == 2 and data["truncated"] is False
    _seed(app, n=60)
    status, headers, body = c.request("GET", "/api/fixes", headers={"Accept-Encoding": "gzip"})
    assert headers.get("content-encoding") == "gzip"
    assert len(json.loads(gzip.decompress(body))["fixes"]) == 60


def test_devices_and_polls(app):
    _seed(app)
    conn = db.connect(app.db_path)
    with conn:
        db.record_poll(conn, 5, False, error="KeyError('secret')")
        db.record_poll(conn, 6, False, error="Couldn't reach Apple. Will try again soon.")
    status, devices = app.client.get("/api/devices")
    assert devices[0]["id"] == "a" and devices[0]["fix_count"] == 3 and devices[0]["state"] == "located"
    status, polls = app.client.get("/api/polls?limit=5")
    assert polls[0]["error"] == "Couldn't reach Apple. Will try again soon."
    assert "secret" not in polls[1]["error"]


def test_csv_export(app):
    _seed(app, device_name="@SUM(A1)")
    status, headers, body = app.client.request("GET", "/api/export?format=csv&device=a")
    assert status == 200 and headers["content-type"] == "text/csv; charset=utf-8"
    assert re.match(r'attachment; filename="lastseen-\d{4}-\d{2}-\d{2}_to_\d{4}-\d{2}-\d{2}\.csv"', headers["content-disposition"])
    text = body.decode("utf-8")
    assert text.startswith("﻿device_name,owner,time_utc,device_id,ts")
    assert "'@SUM(A1)" in text and "'=cmd()" in text
    assert text.count("\n") == 4


def test_empty_csv_has_header(app):
    status, headers, body = app.client.request("GET", "/api/export?format=csv")
    assert 'filename="lastseen-empty.csv"' in headers["content-disposition"]
    assert body.decode("utf-8").strip() == "﻿device_name,owner,time_utc," + ",".join(db.FIX_COLUMNS)


def test_gpx_export(app):
    _seed(app, device_name="Bad\x01Name & <co>", owner="Sam", altitude=0.0)
    status, headers, body = app.client.request("GET", "/api/export?format=gpx")
    assert headers["content-type"] == "application/gpx+xml"
    import xml.dom.minidom

    doc = xml.dom.minidom.parseString(body)
    assert doc.getElementsByTagName("name")[0].firstChild.data == "BadName & <co>"
    assert len(doc.getElementsByTagName("ele")) == 3
    assert doc.getElementsByTagName("desc")[0].firstChild.data == "Sam"
    assert app.client.get("/api/export?format=kml")[0] == 400


def test_settings(app):
    c = app.client
    assert c.post("/api/settings", {"interval_minutes": 30, "retention_days": 90}) == (
        200, {"settings": {"interval_minutes": 30, "retention_days": 90}})
    for bad in ({"interval_minutes": 1}, {"interval_minutes": "15"}, {"retention_days": -1}, {"interval_minutes": True}):
        status, data = c.post("/api/settings", bad)
        assert status == 400 and data["error"] == "bad_setting"


def test_history_delete(app):
    _seed(app)
    assert app.client.post("/api/history/delete", {"confirm": "yes"})[1]["error"] == "confirm_required"
    assert app.client.post("/api/history/delete", {"confirm": "DELETE"}) == (200, {"ok": True})
    assert app.client.get("/api/devices")[1] == []


def test_onboarding_endpoints(app):
    c = app.client
    status, data = c.post("/api/icloud/login", {"apple_id": "me@example.com"})
    assert status == 400 and data["error"] == "missing_fields"
    assert c.post("/api/icloud/code", {"code": "123456"})[1]["error"] == "no_pending_login"
    status, data = c.post("/api/icloud/login", {"apple_id": "me@example.com", "password": "pw"})
    assert data == {"step": "code", "method": "trusted_device", "notice": None}
    assert c.post("/api/icloud/code", {"code": "000000"})[1]["error"] == "wrong_code"
    assert c.post("/api/icloud/code", {"code": "123456"}) == (200, {"step": "done", "trusted": True})
    status, data = c.get("/api/status")
    assert data["account"] == {"configured": True, "apple_id": "me@example.com", "credential_store": "file"}
    assert data["collector"]["state"] == "idle"
    assert c.post("/api/icloud/login", {"apple_id": "me@example.com", "password": "pw"})[1]["error"] == "already_signed_in"
    assert c.post("/api/icloud/logout")[0] == 200
    assert c.get("/api/status")[1]["account"]["configured"] is False


def test_poll_endpoint(app):
    c = app.client
    app.cfg.update(apple_id="me@example.com", needs_login={"reason": "session_expired", "message": "m", "since": 1})
    assert c.post("/api/poll")[1]["error"] == "needs_login"
    app.cfg.update(needs_login=None)
    app.collector.set_service(app.service)
    app.collector.start()
    assert c.post("/api/poll") == (202, {"ok": True})


def test_service_endpoints_unsupported(app, monkeypatch):
    monkeypatch.setenv("LASTSEEN_IN_DOCKER", "1")
    status, data = app.client.post("/api/service/install")
    assert status == 400 and data["error"] == "unsupported"
    assert app.client.get("/api/status")[1]["service"]["supported"] is False


def test_internal_errors_are_json(app, monkeypatch):
    def boom(conn):
        raise RuntimeError("secret")

    monkeypatch.setattr(db, "list_devices", boom)
    status, data = app.client.get("/api/devices")
    assert status == 500 and data == {"error": "internal", "message": "Something went wrong. See the log."}


def test_health_identifies_data_folder(app):
    from lastseen import config

    data = app.client.get("/api/health")[1]
    assert data["instance"] == config.instance_id(app.data_dir)
    assert str(app.data_dir) not in json.dumps(data)


def test_reset_password_on_remote_instance_leaves_a_setup_link(remote_app, caplog):
    """`lastseen reset-password` (default --host) must not lock out a network-bound lastseen."""
    from lastseen import cli, config

    c = remote_app.client
    token = config.setup_token(remote_app.data_dir, create=True)
    assert c.post("/api/access-password", {"password": "correct horse", "setup_token": token})[0] == 200
    assert not (remote_app.data_dir / "setup-token").exists()
    assert cli.cmd_reset_password(cli.parse_args(["reset-password", "--data-dir", str(remote_app.data_dir)])) == 0
    c.cookie = None
    status = c.get("/api/status")[1]
    assert status["auth"]["setup_token_required"] is True and status["docker"] is False
    new_token = config.setup_token(remote_app.data_dir)
    assert new_token
    assert c.post("/api/access-password", {"password": "battery staple", "setup_token": new_token})[0] == 200


def test_missing_setup_token_is_created_and_logged(remote_app, caplog):
    from lastseen import config

    assert config.setup_token(remote_app.data_dir) is None
    with caplog.at_level("WARNING"):
        remote_app.client.get("/api/status")
    token = config.setup_token(remote_app.data_dir)
    assert token and f"?setup={token}" in caplog.text


def test_proxied_requests_are_treated_as_remote(app):
    from lastseen import config

    app.ctx.allowed_hosts = {"lastseen.example.com"}
    c = app.client
    proxied = {"Host": "lastseen.example.com", "X-Forwarded-For": "203.0.113.9"}
    status, data = c.json("GET", "/api/status", headers=proxied)
    assert data["auth"]["required"] is True and data["auth"]["setup_token_required"] is True
    assert c.json("GET", "/api/devices", headers=proxied)[0] == 401
    # The first password through the proxy needs the setup token.
    status, data = c.json("POST", "/api/access-password", {"password": "correct horse"}, headers=proxied)
    assert status == 403 and data["error"] == "bad_setup_token"
    token = config.setup_token(app.data_dir)
    assert c.json("POST", "/api/access-password", {"password": "correct horse", "setup_token": token}, headers=proxied)[0] == 200
    status, data = c.json("POST", "/api/access-password", {"password": None, "current_password": "correct horse"}, headers=proxied)
    assert status == 403 and data["error"] == "cannot_remove_remote"
    # Direct loopback requests can still remove it.
    status, data = c.json("POST", "/api/access-password", {"password": None, "current_password": "correct horse"})
    assert status == 200


def test_rate_limit_behind_proxy_is_per_client(app):
    app.ctx.allowed_hosts = {"lastseen.example.com"}
    c = app.client
    c.post("/api/access-password", {"password": "correct horse"})
    c.cookie = None

    def login(pw, ip):
        return c.json("POST", "/api/session/login", {"password": pw},
                      headers={"Host": "lastseen.example.com", "X-Forwarded-For": f"10.0.0.1, {ip}"})

    for _ in range(5):
        assert login("nope", "203.0.113.66")[0] == 401
    assert login("correct horse", "203.0.113.66")[0] == 429
    assert login("correct horse", "198.51.100.7")[0] == 200


def test_server_does_not_share_ports_on_windows():
    import os

    assert web.Server.allow_reuse_address is (os.name != "nt")
