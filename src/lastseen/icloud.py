"""iCloud sign-in: building the pyicloud service, error mapping and the onboarding flow.

This is the only module that imports pyicloud, and it does so lazily so that
commands like `lastseen status` start fast.
"""

from __future__ import annotations

import logging
from pathlib import Path
import re
import shutil
import threading
import time
from typing import TYPE_CHECKING, Any, Callable

from lastseen import config

if TYPE_CHECKING:
    from pyicloud import PyiCloudService

    from lastseen.collector import Collector

log = logging.getLogger(__name__)

HTTP_TIMEOUT = (10, 60)  # connect, read

MESSAGES = {
    "account_locked": "Apple has locked this Apple Account. Unlock it at iforgot.apple.com, then sign in again.",
    "invalid_credentials": "Apple rejected the Apple ID or password.",
    "session_expired": "Apple signed lastseen out. Sign in again to keep recording.",
    "accept_terms": "Apple needs you to accept new iCloud terms. Sign in at icloud.com once, then sign in here again.",
    "needs_web_access": (
        "Find My on iCloud.com isn't available for this account. If Advanced Data Protection is on, "
        "enable 'Access iCloud Data on the Web'."
    ),
    "no_devices": "No devices were found on this Apple Account.",
    "network": "Couldn't reach Apple. Will try again soon.",
    "apple_unavailable": "Apple's service had a problem. Will try again soon.",
    "unknown": "Something went wrong. See the log for details.",
}


class NeedsLogin(Exception):
    """The saved session can't be used without signing in again."""


def open_service(apple_id: str, password: str | None, session_dir: Path) -> PyiCloudService:
    """Sign in using the cached session, or SRP with `password` when it has expired."""
    from pyicloud import PyiCloudService

    config.ensure_private_dir(session_dir)
    # authenticate=False with an explicit password keeps pyicloud from reading
    # the keyring on its own.
    api = PyiCloudService(
        apple_id, password, cookie_directory=str(session_dir),
        with_family=True, refresh_interval=None, locate=False, authenticate=False,
    )
    _apply_timeout(api.session)
    api.authenticate()
    return api


def _apply_timeout(session: Any) -> None:
    """pyicloud sends requests without a timeout; a stalled socket would hang forever."""
    for name in ("request", "request_raw"):
        original = getattr(session, name, None)
        if original is None:
            continue

        def wrapped(*args: Any, _original: Callable[..., Any] = original, **kwargs: Any) -> Any:
            if kwargs.get("timeout") is None:
                kwargs["timeout"] = HTTP_TIMEOUT
            return _original(*args, **kwargs)

        setattr(session, name, wrapped)


def _code(exc: BaseException) -> int | None:
    try:
        return int(getattr(exc, "code", None))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def classify(exc: BaseException) -> tuple[str, str, str]:
    """Map an exception to (kind, reason, friendly message).

    kind is "needs_login" (a person must sign in again), "retry" (transient) or
    "fail". Only the friendly message is ever stored or shown.
    """
    import requests
    from pyicloud import exceptions as e

    def result(kind: str, reason: str) -> tuple[str, str, str]:
        if reason == "unknown":
            log.error("Unexpected error", exc_info=exc)
        else:
            log.warning("%s: %s", reason, type(exc).__name__)
        return kind, reason, MESSAGES[reason]

    if isinstance(exc, e.PyiCloudAccountLockedException):
        return result("needs_login", "account_locked")
    if isinstance(exc, e.PyiCloudFailedLoginException):
        cause: BaseException | None = exc.__cause__ or exc.__context__
        for _ in range(10):
            if cause is None:
                break
            if isinstance(cause, e.PyiCloudConnectionException):
                return result("retry", "network")
            if isinstance(cause, e.PyiCloudAPIResponseException) and (_code(cause) or 0) >= 500:
                return result("retry", "apple_unavailable")
            cause = cause.__cause__ or cause.__context__
        if "No password" in str(exc):
            return result("needs_login", "session_expired")
        return result("needs_login", "invalid_credentials")
    if isinstance(exc, (
        NeedsLogin, e.PyiCloud2FARequiredException, e.PyiCloud2SARequiredException,
        e.PyiCloudAuthRequiredException, e.PyiCloudNoStoredPasswordAvailableException,
    )):
        return result("needs_login", "session_expired")
    if isinstance(exc, e.PyiCloudAcceptTermsException):
        return result("needs_login", "accept_terms")
    if isinstance(exc, (e.PyiCloudServiceNotActivatedException, e.PyiCloudServiceUnavailable)):
        return result("fail", "needs_web_access")
    if isinstance(exc, e.PyiCloudNoDevicesException):
        return result("fail", "no_devices")
    if isinstance(exc, (e.PyiCloudConnectionException, requests.exceptions.RequestException, OSError, TimeoutError)):
        return result("retry", "network")
    if isinstance(exc, e.PyiCloudAPIResponseException):
        code = _code(exc)
        if code in (401, 421, 450):
            return result("needs_login", "session_expired")
        if code is not None and code >= 500:
            return result("retry", "apple_unavailable")
    return result("fail", "unknown")


# --- Web onboarding -----------------------------------------------------------

class LoginError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


_LOGIN_ERRORS = {
    "invalid_credentials": 401,
    "account_locked": 423,
    "accept_terms": 403,
    "needs_web_access": 403,
    "network": 502,
    "apple_unavailable": 503,
}
CODE_RE = re.compile(r"^\d{6}$")
PENDING_TTL = 600
SECURITY_KEY_WAIT = 60
RESEND_COOLDOWN = 30
UNTRUSTED_WARNING = (
    "Signed in, but Apple didn't trust this computer, so you may need to sign in again sooner."
)


def _login_error(exc: BaseException) -> LoginError:
    _, reason, message = classify(exc)
    if reason in _LOGIN_ERRORS:
        return LoginError(_LOGIN_ERRORS[reason], reason, message)
    return LoginError(500, "unknown", "Sign-in didn't complete. Please try again." if reason == "session_expired" else message)


class LoginFlow:
    """One pending Apple sign-in at a time, driven by the web onboarding."""

    def __init__(
        self,
        data_dir: Path,
        cfg: config.ConfigStore,
        collector: Collector,
        service_factory: Callable[..., Any] = open_service,
        docker: bool = False,
        clock: Callable[[], float] = time.time,
    ):
        self.data_dir = data_dir
        self.cfg = cfg
        self.collector = collector
        self.service_factory = service_factory
        self.docker = docker
        self.clock = clock
        self._lock = threading.Lock()
        self._pending: dict[str, Any] | None = None

    @property
    def session_dir(self) -> Path:
        return self.data_dir / "session"

    def _take_pending(self) -> dict[str, Any]:
        p = self._pending
        if p is None or self.clock() - p["at"] > PENDING_TTL:
            self._pending = None
            raise LoginError(400, "no_pending_login", "This sign-in has expired. Please start again.")
        return p

    def start(self, apple_id: Any, password: Any) -> dict[str, Any]:
        if not isinstance(apple_id, str) or not apple_id.strip() or not isinstance(password, str) or not password:
            raise LoginError(400, "missing_fields", "Enter your Apple ID and password.")
        apple_id = apple_id.strip()
        cfg = self.cfg.data
        current = cfg.get("apple_id")
        if current and (not cfg.get("needs_login") or current.lower() != apple_id.lower()):
            raise LoginError(409, "already_signed_in", "lastseen is already signed in. Sign out first to use another Apple Account.")
        with self._lock:
            return self._start(apple_id, password)

    def _start(self, apple_id: str, password: str) -> dict[str, Any]:
        self._pending = None
        try:
            api = self.service_factory(apple_id, password, self.session_dir)
            if api.requires_2fa:
                self._pending = {"apple_id": apple_id, "password": password, "api": api, "attempts": 0, "at": self.clock()}
                # pyicloud already asked Apple to deliver the code during sign-in.
                method = api.two_factor_delivery_method
                if method == "security_key":
                    return {"step": "security_key", "key_names": list(api.security_key_names or []), "supported": not self.docker}
                if method not in ("trusted_device", "sms"):
                    method = "unknown"
                return {"step": "code", "method": method, "notice": api.two_factor_delivery_notice}
            if api.requires_2sa:
                raise LoginError(
                    403, "unsupported_2sa",
                    "This Apple Account uses an old verification method; turn on two-factor authentication.",
                )
        except LoginError:
            raise
        except Exception as exc:
            raise _login_error(exc) from None
        return self._finish(apple_id, password, api, api.is_trusted_session)

    def submit_code(self, code: Any) -> dict[str, Any]:
        with self._lock:
            pending = self._take_pending()
            if not isinstance(code, str) or not CODE_RE.match(code.strip()):
                raise LoginError(400, "wrong_code", "Enter the 6-digit code.")
            api = pending["api"]
            try:
                ok = api.validate_2fa_code(code.strip())
            except Exception as exc:
                raise _login_error(exc) from None
            if not ok:
                pending["attempts"] += 1
                if pending["attempts"] >= 3:
                    self._pending = None
                    raise LoginError(400, "code_expired", "Too many wrong codes. Please sign in again to get a new code.")
                raise LoginError(400, "wrong_code", "That code didn't work. Check it and try again.")
            # validate_2fa_code already asked Apple to trust this session.
            return self._finish(pending["apple_id"], pending["password"], api, api.is_trusted_session)

    def resend(self) -> dict[str, Any]:
        # A fresh sign-in is what reliably makes Apple send a new code: pyicloud
        # won't ask again for a challenge it already requested a code for. Each
        # one is a password sign-in to Apple, so they are spaced out.
        with self._lock:
            pending = self._take_pending()
            wait = RESEND_COOLDOWN - (self.clock() - pending["at"])
            if wait > 0:
                raise LoginError(429, "resend_too_soon", f"Wait {int(wait) + 1} seconds before asking for another code.")
            return self._start(pending["apple_id"], pending["password"])

    def security_key(self) -> dict[str, Any]:
        with self._lock:
            pending = self._take_pending()
            if self.docker:
                raise LoginError(400, "security_key_unsupported", "Security keys can't be used from Docker.")
            api = pending["api"]
            try:
                devices = list(api.fido2_devices or [])
            except Exception:
                log.warning("Couldn't list security keys", exc_info=True)
                devices = []
            if not devices:
                raise LoginError(400, "security_key_not_found", "No security key found. Plug it into the computer running lastseen and try again.")
            from pyicloud.exceptions import PyiCloudSecurityKeyTimeoutException

            event = threading.Event()
            timer = threading.Timer(SECURITY_KEY_WAIT, event.set)
            timer.daemon = True
            timer.start()
            try:
                api.confirm_security_key(devices[0], event=event)
                if not api.is_trusted_session:
                    api.trust_session()
            except PyiCloudSecurityKeyTimeoutException:
                raise LoginError(408, "security_key_timeout", "The security key wasn't touched in time. Try again.") from None
            except Exception as exc:
                raise _login_error(exc) from None
            finally:
                timer.cancel()
            return self._finish(pending["apple_id"], pending["password"], api, api.is_trusted_session)

    def _finish(self, apple_id: str, password: str, api: Any, trusted: bool) -> dict[str, Any]:
        self._pending = None
        # Config first, so a keychain failure never loses a completed 2FA.
        self.cfg.update(apple_id=apple_id, needs_login=None)
        try:
            store = config.store_password(self.data_dir, apple_id, password)
        except OSError:
            log.exception("Couldn't save the Apple password")
            store = None
        self.cfg.update(credential_store=store)
        if not trusted:
            log.warning(UNTRUSTED_WARNING)
        log.info("Signed in to iCloud")
        self.collector.set_service(api)
        return {"step": "done", "trusted": bool(trusted)}

    def logout(self) -> None:
        """Forget the Apple Account. History is kept."""
        with self._lock:
            self._pending = None
            apple_id = self.cfg.get("apple_id")
            self.cfg.update(apple_id=None, needs_login=None, credential_store=None)
            self.collector.set_service(None)
            config.forget_password(self.data_dir, apple_id)
            shutil.rmtree(self.session_dir, ignore_errors=True)
