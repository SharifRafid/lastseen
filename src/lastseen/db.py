"""SQLite storage for device location history."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from pathlib import Path
import sqlite3
import time
from typing import Any

from lastseen.config import chmod_private

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    id            TEXT PRIMARY KEY,
    name          TEXT,
    model         TEXT,
    display_name  TEXT,
    device_class  TEXT,
    owner         TEXT,
    is_family     INTEGER,
    state         TEXT,     -- from the latest poll
    state_at      INTEGER,
    battery_level REAL,
    first_seen    INTEGER,
    last_seen     INTEGER
);

-- One row per distinct location fix Apple reported for a device.
-- `ts` is Apple's fix timestamp (ms since epoch); a fix seen twice is stored once.
CREATE TABLE IF NOT EXISTS fixes (
    device_id        TEXT NOT NULL REFERENCES devices(id),
    ts               INTEGER NOT NULL,
    lat              REAL NOT NULL,
    lon              REAL NOT NULL,
    h_accuracy       REAL,
    v_accuracy       REAL,
    altitude         REAL,
    floor            INTEGER,
    position_type    TEXT,
    is_old           INTEGER,
    is_inaccurate    INTEGER,
    battery_level    REAL,
    battery_status   TEXT,
    device_status    TEXT,
    low_power_mode   INTEGER,
    polled_at        INTEGER NOT NULL,
    PRIMARY KEY (device_id, ts)
);
CREATE INDEX IF NOT EXISTS fixes_ts ON fixes(ts);

CREATE TABLE IF NOT EXISTS polls (
    polled_at     INTEGER PRIMARY KEY,
    ok            INTEGER NOT NULL,
    devices       INTEGER,
    new_fixes     INTEGER,
    error         TEXT
);
"""

FIX_COLUMNS = [
    "device_id", "ts", "lat", "lon", "h_accuracy", "v_accuracy", "altitude", "floor",
    "position_type", "is_old", "is_inaccurate", "battery_level", "battery_status",
    "device_status", "low_power_mode", "polled_at",
]
API_FIX_COLUMNS = ["device_id", "ts", "lat", "lon", "h_accuracy", "battery_level", "is_old", "position_type"]

DAY_MS = 86_400_000
POLL_RETENTION_DAYS = 90


def now_ms() -> int:
    return int(time.time() * 1000)


def init(path: Path) -> None:
    """Create the database if needed. Run once at startup."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
    finally:
        conn.close()
    for p in (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
        if p.exists():
            chmod_private(p, 0o600)


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def upsert_device(conn: sqlite3.Connection, dev: dict[str, Any], seen: int) -> None:
    conn.execute(
        """
        INSERT INTO devices (id, name, model, display_name, device_class, owner,
                             is_family, state, state_at, battery_level, first_seen, last_seen)
        VALUES (:id, :name, :model, :display_name, :device_class, :owner,
                :is_family, :state, :seen, :battery_level, :seen, :seen)
        ON CONFLICT(id) DO UPDATE SET
            name = excluded.name, model = excluded.model,
            display_name = excluded.display_name, device_class = excluded.device_class,
            owner = excluded.owner, is_family = excluded.is_family,
            state = excluded.state, state_at = excluded.state_at,
            battery_level = excluded.battery_level, last_seen = excluded.last_seen
        """,
        {**dev, "seen": seen},
    )


def insert_fix(conn: sqlite3.Connection, fix: dict[str, Any]) -> bool:
    """Insert a fix; returns False when it was already stored."""
    cols = ", ".join(FIX_COLUMNS)
    params = ", ".join(f":{c}" for c in FIX_COLUMNS)
    cur = conn.execute(f"INSERT OR IGNORE INTO fixes ({cols}) VALUES ({params})", fix)
    return cur.rowcount == 1


def record_poll(
    conn: sqlite3.Connection,
    polled_at: int,
    ok: bool,
    devices: int = 0,
    new_fixes: int = 0,
    error: str | None = None,
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO polls VALUES (?, ?, ?, ?, ?)",
        (polled_at, int(ok), devices, new_fixes, error),
    )


def list_devices(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT d.id, d.name, d.display_name, d.device_class, d.owner,
               d.is_family,
               d.state, d.state_at, d.battery_level, d.first_seen, d.last_seen,
               COUNT(f.ts) AS fix_count, MAX(f.ts) AS last_fix
        FROM devices d LEFT JOIN fixes f ON f.device_id = d.id
        GROUP BY d.id
        ORDER BY d.is_family, d.owner, d.name
        """
    )
    return [{**dict(r), "is_family": bool(r["is_family"])} for r in rows]


def _where(ids: Sequence[str] | None, start: int | None, end: int | None, alias: str = "") -> tuple[str, list[Any]]:
    clauses, args = [], []
    if ids:
        clauses.append(f"{alias}device_id IN ({','.join('?' * len(ids))})")
        args += list(ids)
    if start is not None:
        clauses.append(f"{alias}ts >= ?")
        args.append(start)
    if end is not None:
        clauses.append(f"{alias}ts <= ?")
        args.append(end)
    return (" WHERE " + " AND ".join(clauses) if clauses else ""), args


def query_fixes(
    conn: sqlite3.Connection,
    ids: Sequence[str] | None,
    start: int | None,
    end: int | None,
    limit: int,
) -> tuple[list[dict[str, Any]], bool]:
    """The newest `limit` fixes in range, oldest first, and whether more exist."""
    where, args = _where(ids, start, end)
    rows = conn.execute(
        f"SELECT {', '.join(API_FIX_COLUMNS)} FROM fixes{where} ORDER BY ts DESC LIMIT ?",
        [*args, limit + 1],
    ).fetchall()
    truncated = len(rows) > limit
    return [dict(r) for r in reversed(rows[:limit])], truncated


def fix_span(conn: sqlite3.Connection, ids: Sequence[str] | None, start: int | None, end: int | None) -> tuple[int | None, int | None]:
    where, args = _where(ids, start, end)
    row = conn.execute(f"SELECT MIN(ts), MAX(ts) FROM fixes{where}", args).fetchone()
    return row[0], row[1]


def iter_fixes_full(
    conn: sqlite3.Connection,
    ids: Sequence[str] | None,
    start: int | None,
    end: int | None,
    by_device: bool = False,
) -> Iterator[dict[str, Any]]:
    """Every column of every fix in range, with the device's name and owner."""
    where, args = _where(ids, start, end, alias="f.")
    order = "f.device_id, f.ts" if by_device else "f.ts, f.device_id"
    cur = conn.execute(
        f"SELECT f.*, d.name AS device_name, d.owner AS owner FROM fixes f "
        f"LEFT JOIN devices d ON d.id = f.device_id{where} ORDER BY {order}",
        args,
    )
    for row in cur:
        yield dict(row)


def recent_polls(conn: sqlite3.Connection, limit: int = 20) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM polls ORDER BY polled_at DESC LIMIT ?", (limit,))
    return [dict(r) for r in rows]


def last_success(conn: sqlite3.Connection) -> int | None:
    return conn.execute("SELECT MAX(polled_at) FROM polls WHERE ok = 1").fetchone()[0]


def counts(conn: sqlite3.Connection) -> dict[str, Any]:
    devices = conn.execute("SELECT COUNT(*) FROM devices").fetchone()[0]
    fixes, since = conn.execute("SELECT COUNT(*), MIN(ts) FROM fixes").fetchone()
    return {"devices": devices, "fixes": fixes, "since": since}


def prune(conn: sqlite3.Connection, now: int, retention_days: int) -> None:
    with conn:
        if retention_days > 0:
            conn.execute("DELETE FROM fixes WHERE ts < ?", (now - retention_days * DAY_MS,))
        conn.execute("DELETE FROM polls WHERE polled_at < ?", (now - POLL_RETENTION_DAYS * DAY_MS,))


def delete_history(conn: sqlite3.Connection) -> None:
    with conn:
        conn.execute("DELETE FROM fixes")
        conn.execute("DELETE FROM polls")
        conn.execute("DELETE FROM devices")
    conn.execute("VACUUM")
