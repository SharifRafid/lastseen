"""Polls iCloud Find My on a schedule and records every device's location fix."""

from __future__ import annotations

import logging
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any, Callable

from lastseen import config, db
from lastseen.icloud import NeedsLogin, classify, open_service

log = logging.getLogger(__name__)

# After asking every device to locate itself, Apple fills in the new fixes over
# the next seconds; iCloud.com re-polls on a similar cadence.
SETTLE_SECONDS = 45
SETTLE_POLL_SECONDS = 5.0
BACKOFF_SECONDS = [60, 120, 300, 600]
# Waits are sliced so a stop request is noticed promptly and a laptop waking
# from sleep catches up on the wall clock.
WAIT_SLICE_SECONDS = 1.0


def _awaiting_fix(content: dict[str, Any], previous_ts: Any, family_ids: set[str]) -> bool:
    features = content.get("features") or {}
    # Offline devices never answer, so only wait on ones that are reachable.
    if not features.get("LOC") or content.get("deviceStatus") != "200":
        return False
    # Family devices whose owner isn't sharing location never get a fix.
    if content.get("prsId") in family_ids and not content.get("fmlyShare"):
        return False
    loc = content.get("location") or {}
    # Macs never set locationFinished, so a changed timestamp also counts.
    return not loc.get("locationFinished") and loc.get("timeStamp") == previous_ts


def _family(manager: Any) -> dict[str, str]:
    """prsId -> display name for Family Sharing members."""
    members = (manager.user_info or {}).get("membersInfo") or {}
    names = {}
    for prs_id, info in members.items():
        info = info or {}
        full = " ".join(p for p in (info.get("firstName"), info.get("lastName")) if p)
        names[prs_id] = full or "Family member"
    return names


def _contents(manager: Any) -> list[dict[str, Any]]:
    return [d.data for d in list(manager.devices.values())]


def _locate_all(manager: Any, settle: float, stop: threading.Event) -> None:
    """Ask every device for a fresh fix and wait (up to `settle` s) for the answers."""
    before = {c["id"]: (c.get("location") or {}).get("timeStamp") for c in _contents(manager)}
    manager.refresh(locate=True)
    family_ids = set(_family(manager))
    deadline = time.monotonic() + settle
    ignored: set[str] = set()
    locating: set[str] = set()
    first = True
    while True:
        for c in _contents(manager):
            # A device that started locating and stopped without a new fix gave up.
            if c.get("isLocating"):
                locating.add(c["id"])
            elif c["id"] in locating:
                ignored.add(c["id"])
        pending = [
            c for c in _contents(manager)
            if c["id"] not in ignored and _awaiting_fix(c, before.get(c["id"]), family_ids)
        ]
        remaining = deadline - time.monotonic()
        if not pending or remaining <= 0:
            return
        log.debug("Waiting on fixes from %d devices", len(pending))
        if stop.wait(min(SETTLE_POLL_SECONDS, remaining)):
            return
        try:
            manager.refresh(locate=False)
        except Exception as exc:
            log.warning("Refresh while waiting for fixes failed (%s); keeping what arrived", type(exc).__name__)
            return
        if first:
            # Devices with no location before or after the locate are unlikely
            # to produce one; don't hold the poll for them.
            ignored |= {
                c["id"] for c in _contents(manager)
                if before.get(c["id"]) is None and not c.get("location")
            }
            first = False


def _state(content: dict[str, Any], loc: dict[str, Any], is_family: bool) -> str:
    if loc.get("latitude") is not None and loc.get("longitude") is not None:
        return "located"
    if is_family and not content.get("fmlyShare"):
        return "not_sharing"
    if content.get("deviceStatus") != "200":
        return "offline"
    return "no_location"


def collect_once(
    api: Any,
    conn: sqlite3.Connection,
    settle: float = SETTLE_SECONDS,
    stop: threading.Event | None = None,
) -> tuple[int, int]:
    """Run one poll; returns (devices seen, new fixes stored)."""
    polled_at = db.now_ms()
    manager = api.devices
    _locate_all(manager, settle, stop or threading.Event())
    family = _family(manager)
    contents = _contents(manager)

    new = 0
    with conn:
        for c in contents:
            prs_id = c.get("prsId")
            is_family = prs_id in family
            loc = c.get("location") or {}
            db.upsert_device(
                conn,
                {
                    "id": c["id"],
                    "name": c.get("name"),
                    "model": c.get("deviceModel"),
                    "display_name": c.get("deviceDisplayName"),
                    "device_class": c.get("deviceClass"),
                    "owner": family.get(prs_id) if is_family else None,
                    "is_family": int(is_family),
                    "state": _state(c, loc, is_family),
                    "battery_level": c.get("batteryLevel"),
                },
                polled_at,
            )
            if loc.get("latitude") is None or loc.get("longitude") is None or loc.get("timeStamp") is None:
                continue
            new += db.insert_fix(
                conn,
                {
                    "device_id": c["id"],
                    "ts": int(loc["timeStamp"]),
                    "lat": loc["latitude"],
                    "lon": loc["longitude"],
                    "h_accuracy": loc.get("horizontalAccuracy"),
                    "v_accuracy": loc.get("verticalAccuracy"),
                    "altitude": loc.get("altitude"),
                    "floor": loc.get("floorLevel"),
                    "position_type": loc.get("positionType"),
                    "is_old": int(bool(loc.get("isOld"))),
                    "is_inaccurate": int(bool(loc.get("isInaccurate"))),
                    "battery_level": c.get("batteryLevel"),
                    "battery_status": c.get("batteryStatus"),
                    "device_status": c.get("deviceStatus"),
                    "low_power_mode": int(bool(c.get("lowPowerMode"))),
                    "polled_at": polled_at,
                },
            )
        db.record_poll(conn, polled_at, True, len(contents), new)
    log.info("Checked %d devices, %d new locations", len(contents), new)
    return len(contents), new


class Collector(threading.Thread):
    """Owns the iCloud session and polls on wall-clock-aligned boundaries."""

    def __init__(
        self,
        data_dir: Path,
        db_path: Path,
        cfg: config.ConfigStore,
        stop: threading.Event,
        service_factory: Callable[..., Any] = open_service,
        clock: Callable[[], float] = time.time,
        settle: float = SETTLE_SECONDS,
    ):
        super().__init__(name="collector", daemon=True)
        self.data_dir, self.db_path, self.cfg, self.stop = data_dir, db_path, cfg, stop
        self.service_factory, self.clock, self.settle = service_factory, clock, settle
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._poll_requested = False
        self._api: Any = None
        self._generation = 0
        self._failures = 0
        self._next_poll_at: int | None = None
        self._state, self._reason, self._message = "not_configured", None, None
        self._since = self._now()
        conn = db.connect(db_path)
        try:
            polls = db.recent_polls(conn, 1)
            self._last_poll = self._poll_dict(polls[0]) if polls else None
            self._last_success_at = db.last_success(conn)
        finally:
            conn.close()

    # --- called from other threads ---

    def set_service(self, api: Any) -> None:
        """Hand over a freshly signed-in session (or None after signing out)."""
        with self._lock:
            self._api = api
            self._generation += 1
            self._failures = 0
            self._next_poll_at = None
            if api is not None:
                self._set_state("idle")
                self._next_poll_at = self._aligned_next()
            else:
                self._set_state("not_configured")
        self._wake.set()

    def poll_now(self) -> bool:
        with self._lock:
            if self._state in ("polling", "not_configured", "needs_login"):
                return False
            self._poll_requested = True
        self._wake.set()
        return True

    def reschedule(self) -> None:
        with self._lock:
            if self._state == "idle":
                self._next_poll_at = self._aligned_next()
        self._wake.set()

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "state": self._state,
                "reason": self._reason,
                "message": self._message,
                "since": self._since,
                "next_poll_at": self._next_poll_at if self._state in ("idle", "retrying") else None,
                "last_poll": dict(self._last_poll) if self._last_poll else None,
                "last_success_at": self._last_success_at,
            }

    # --- collector thread ---

    def _now(self) -> int:
        return int(self.clock() * 1000)

    def _interval_ms(self) -> int:
        minutes = self.cfg.get("interval_minutes") or 15
        return max(5, min(1440, int(minutes))) * 60_000

    def _aligned_next(self) -> int:
        interval = self._interval_ms()
        return (self._now() // interval + 1) * interval

    def _set_state(self, state: str, reason: str | None = None, message: str | None = None, since: int | None = None) -> None:
        if (state, reason) != (self._state, self._reason):
            self._since = since or self._now()
        self._state, self._reason, self._message = state, reason, message

    @staticmethod
    def _poll_dict(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "at": row["polled_at"], "ok": bool(row["ok"]), "devices": row["devices"],
            "new_fixes": row["new_fixes"], "error": None if row["ok"] else row["error"],
        }

    def run(self) -> None:
        conn = db.connect(self.db_path)
        try:
            while not self.stop.is_set():
                try:
                    self._step(conn)
                except Exception:
                    log.exception("Collector loop error")
                    self.stop.wait(30)
        finally:
            conn.close()

    def _wait_for_wake(self) -> None:
        while not self.stop.is_set() and not self._wake.wait(WAIT_SLICE_SECONDS):
            pass
        self._wake.clear()

    def _step(self, conn: sqlite3.Connection) -> None:
        cfg = self.cfg.data
        if not cfg.get("apple_id"):
            with self._lock:
                self._api = None
                self._set_state("not_configured")
                self._next_poll_at = None
            self._wait_for_wake()
            return
        if cfg.get("needs_login"):
            nl = cfg["needs_login"]
            with self._lock:
                self._api = None
                self._set_state("needs_login", nl.get("reason"), nl.get("message"), nl.get("since"))
                self._next_poll_at = None
            self._wait_for_wake()
            return
        with self._lock:
            if self._state in ("not_configured", "needs_login"):
                self._set_state("idle")
            if self._next_poll_at is None:
                self._next_poll_at = self._aligned_next()
            due = self._next_poll_at

        while not self.stop.is_set() and not self._wake.is_set():
            remaining = (due - self._now()) / 1000
            if remaining <= 0:
                break
            self._wake.wait(min(WAIT_SLICE_SECONDS, remaining))
        if self.stop.is_set():
            return
        with self._lock:
            woke = self._wake.is_set()
            self._wake.clear()
            requested, self._poll_requested = self._poll_requested, False
            if woke and not requested and (self._next_poll_at is None or self._next_poll_at > self._now()):
                return  # signed in/out or settings changed: re-evaluate
        self._poll(conn, cfg)

    def _poll(self, conn: sqlite3.Connection, cfg: dict[str, Any]) -> None:
        with self._lock:
            generation, api = self._generation, self._api
            self._set_state("polling", self._reason, self._message)
        started = self._now()
        apple_id = cfg["apple_id"]
        try:
            if api is None:
                password = config.load_password(self.data_dir, apple_id)
                api = self.service_factory(apple_id, password, self.data_dir / "session")
                if api.requires_2fa or api.requires_2sa:
                    raise NeedsLogin()
                with self._lock:
                    if generation == self._generation:
                        self._api = api
            devices, new = collect_once(api, conn, self.settle, self.stop)
        except Exception as exc:
            self._failed(conn, exc, started, generation)
            return
        try:
            db.prune(conn, self._now(), int(self.cfg.get("retention_days") or 0))
        except sqlite3.Error:
            log.exception("Pruning old history failed")
        with self._lock:
            self._last_poll = {"at": started, "ok": True, "devices": devices, "new_fixes": new, "error": None}
            self._last_success_at = started
            if generation == self._generation:
                self._failures = 0
                self._set_state("idle")
                self._next_poll_at = self._aligned_next()

    def _failed(self, conn: sqlite3.Connection, exc: Exception, started: int, generation: int) -> None:
        kind, reason, message = classify(exc)
        try:
            with conn:
                db.record_poll(conn, started, False, error=message)
        except sqlite3.Error:
            log.exception("Couldn't record the failed check")
        with self._lock:
            self._last_poll = {"at": started, "ok": False, "devices": 0, "new_fixes": 0, "error": message}
            if generation != self._generation:
                return
            if kind == "needs_login":
                # Never retry on our own: repeated sign-ins spam 2FA prompts
                # and can get the Apple Account locked.
                self._api = None
                self._set_state("needs_login", reason, message, started)
                self._next_poll_at = None
            elif kind == "retry":
                self._failures += 1
                self._set_state("retrying", reason, message)
                if self._failures <= len(BACKOFF_SECONDS):
                    delay = min(BACKOFF_SECONDS[self._failures - 1] * 1000, self._interval_ms())
                    self._next_poll_at = self._now() + delay
                else:
                    self._next_poll_at = self._aligned_next()
            else:
                if reason == "unknown":
                    self._api = None  # rebuilt from the cached session next time
                self._failures = 0
                self._set_state("idle", reason, message)
                self._next_poll_at = self._aligned_next()
        if kind == "needs_login":
            log.error("Apple signed lastseen out (%s); waiting for you to sign in again", reason)
            self.cfg.update(needs_login={"reason": reason, "message": message, "since": started})
