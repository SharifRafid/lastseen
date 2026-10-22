from __future__ import annotations

import sqlite3

from lastseen import db


def test_init_is_idempotent(tmp_path):
    path = tmp_path / "history.db"
    db.init(path)
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO devices (id, name, owner, is_family, first_seen, last_seen) VALUES ('a', 'iPhone', NULL, 0, 1, 2)")
    conn.execute("INSERT INTO devices (id, name, owner, is_family, first_seen, last_seen) VALUES ('b', 'iPad', 'Sam', 1, 1, 2)")
    conn.execute("INSERT INTO fixes (device_id, ts, lat, lon, polled_at) VALUES ('a', 10, 1, 2, 10)")
    conn.commit()
    conn.close()

    db.init(path)
    conn = db.connect(path)
    devices = db.list_devices(conn)
    assert [d["id"] for d in devices] == ["a", "b"]
    assert devices[0]["is_family"] is False and devices[1]["is_family"] is True
    assert devices[0]["fix_count"] == 1 and devices[0]["state"] is None
    assert db.counts(conn) == {"devices": 2, "fixes": 1, "since": 10}


def test_fresh_db_and_empty_queries(tmp_path):
    path = tmp_path / "sub" / "history.db"
    db.init(path)
    conn = db.connect(path)
    assert db.query_fixes(conn, None, None, None, 10) == ([], False)
    assert db.fix_span(conn, ["x"], 1, 2) == (None, None)
    assert db.counts(conn) == {"devices": 0, "fixes": 0, "since": None}
