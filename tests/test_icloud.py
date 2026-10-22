from __future__ import annotations

import pytest
from pyicloud import exceptions as e
import requests

from conftest import FakeLoginService
from lastseen.icloud import MESSAGES, LoginError, NeedsLogin, _apply_timeout, classify


class Resp:
    status_code = 500
    text = ""


def _failed_login_caused_by(cause):
    try:
        try:
            raise cause
        except Exception as inner:
            raise e.PyiCloudFailedLoginException("Failed to initiate srp authentication.", inner) from inner
    except e.PyiCloudFailedLoginException as exc:
        return exc


@pytest.mark.parametrize("exc, kind, reason", [
    (e.PyiCloudAccountLockedException(-20209, Resp()), "needs_login", "account_locked"),
    (e.PyiCloudFailedLoginException("Invalid email/password combination."), "needs_login", "invalid_credentials"),
    (e.PyiCloudFailedLoginException("No password set"), "needs_login", "session_expired"),
    (_failed_login_caused_by(e.PyiCloudAPIResponseException("Service Unavailable", 503)), "retry", "apple_unavailable"),
    (_failed_login_caused_by(e.PyiCloudConnectionException("Request failed")), "retry", "network"),
    (e.PyiCloud2FARequiredException("a", Resp()), "needs_login", "session_expired"),
    (e.PyiCloud2SARequiredException("a"), "needs_login", "session_expired"),
    (e.PyiCloudAuthRequiredException("a", Resp()), "needs_login", "session_expired"),
    (e.PyiCloudNoStoredPasswordAvailableException(), "needs_login", "session_expired"),
    (NeedsLogin(), "needs_login", "session_expired"),
    (e.PyiCloudAcceptTermsException(), "needs_login", "accept_terms"),
    (e.PyiCloudServiceNotActivatedException("x", 1), "fail", "needs_web_access"),
    (e.PyiCloudServiceUnavailable("x"), "fail", "needs_web_access"),
    (e.PyiCloudNoDevicesException(), "fail", "no_devices"),
    (e.PyiCloudConnectionException("x"), "retry", "network"),
    (requests.exceptions.ReadTimeout(), "retry", "network"),
    (OSError("x"), "retry", "network"),
    (TimeoutError(), "retry", "network"),
    (e.PyiCloudAPIResponseException("x", 421), "needs_login", "session_expired"),
    (e.PyiCloudAPIResponseException("x", 450), "needs_login", "session_expired"),
    (e.PyiCloudAPIResponseException("x", 401), "needs_login", "session_expired"),
    (e.PyiCloudAPIResponseException("x", 502), "retry", "apple_unavailable"),
    (e.PyiCloudAPIResponseException("x", "500"), "retry", "apple_unavailable"),
    (e.PyiCloudAPIResponseException("x", 400), "fail", "unknown"),
    (ValueError("internal detail"), "fail", "unknown"),
])
def test_classify(exc, kind, reason):
    assert classify(exc) == (kind, reason, MESSAGES[reason])


def test_timeout_is_applied():
    seen = {}

    class Session:
        def request(self, method, url, **kw):
            seen.update(kw)

    s = Session()
    _apply_timeout(s)
    s.request("GET", "https://example.invalid")
    assert seen["timeout"] == (10, 60)
    s.request("GET", "https://example.invalid", timeout=5)
    assert seen["timeout"] == 5


def test_login_code_flow(app):
    flow, svc = app.login, app.service
    res = flow.start("me@example.com", "pw")
    assert res == {"step": "code", "method": "trusted_device", "notice": None}
    with pytest.raises(LoginError) as err:
        flow.submit_code("12345")
    assert err.value.code == "wrong_code" and svc.codes == []
    assert flow.submit_code("123456") == {"step": "done", "trusted": True}
    assert app.cfg.get("apple_id") == "me@example.com"
    assert app.cfg.get("credential_store") == "file"
    assert app.collector.status()["state"] == "idle"
    with pytest.raises(LoginError) as err:
        flow.start("other@example.com", "pw")
    assert err.value.status == 409


def test_login_wrong_codes_expire(app):
    app.login.start("me@example.com", "pw")
    for _ in range(2):
        with pytest.raises(LoginError) as err:
            app.login.submit_code("000000")
        assert err.value.code == "wrong_code"
    with pytest.raises(LoginError) as err:
        app.login.submit_code("000000")
    assert err.value.code == "code_expired"
    with pytest.raises(LoginError) as err:
        app.login.submit_code("123456")
    assert err.value.code == "no_pending_login"


def test_login_errors_are_mapped(tmp_path):
    from conftest import App

    def factory(*a):
        raise e.PyiCloudFailedLoginException("Invalid email/password combination.")

    a = App(tmp_path, factory=factory)
    try:
        with pytest.raises(LoginError) as err:
            a.login.start("me@example.com", "bad")
        assert (err.value.status, err.value.code) == (401, "invalid_credentials")
        with pytest.raises(LoginError) as err:
            a.login.start("", "")
        assert err.value.code == "missing_fields"
    finally:
        a.close()


def test_security_key_step(app):
    app.service = FakeLoginService(method="security_key")
    res = app.login.start("me@example.com", "pw")
    assert res == {"step": "security_key", "key_names": ["YubiKey"], "supported": True}
    with pytest.raises(LoginError) as err:
        app.login.security_key()
    assert err.value.code == "security_key_not_found"


def test_relogin_allowed_while_needs_login(app):
    app.cfg.update(apple_id="me@example.com", needs_login={"reason": "session_expired", "message": "m", "since": 1})
    assert app.login.start("ME@example.com", "pw")["step"] == "code"
    with pytest.raises(LoginError):
        app.login.start("someone@example.com", "pw")


def test_logout_keeps_history(app):
    app.login.start("me@example.com", "pw")
    app.login.submit_code("123456")
    (app.data_dir / "session").mkdir(exist_ok=True)
    app.login.logout()
    assert app.cfg.get("apple_id") is None
    assert not (app.data_dir / "session").exists()
    assert not (app.data_dir / "credentials.json").exists()
    assert app.collector.status()["state"] == "not_configured"
    assert app.db_path.exists()


def test_resend_is_spaced_out(tmp_path):
    import threading

    from lastseen import config, db
    from lastseen.collector import Collector
    from lastseen.icloud import RESEND_COOLDOWN, LoginFlow

    now = [1000.0]
    calls = []

    def factory(apple_id, password, session_dir):
        calls.append(apple_id)
        return FakeLoginService()

    db.init(tmp_path / "h.db")
    cfg = config.ConfigStore(tmp_path)
    collector = Collector(tmp_path, tmp_path / "h.db", cfg, threading.Event(), service_factory=factory)
    flow = LoginFlow(tmp_path, cfg, collector, service_factory=factory, clock=lambda: now[0])
    assert flow.start("me@example.com", "pw")["step"] == "code"
    with pytest.raises(LoginError) as err:
        flow.resend()
    assert err.value.code == "resend_too_soon" and len(calls) == 1
    now[0] += RESEND_COOLDOWN
    assert flow.resend()["step"] == "code" and len(calls) == 2
