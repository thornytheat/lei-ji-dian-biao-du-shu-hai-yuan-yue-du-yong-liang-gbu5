"""SQLite 访问层：读数、换表记录、表计台账、还原批次、事件。

命名卷 metercalc-data 挂到 /app/data；重建数据：docker compose down -v 后重跑。
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import date
from pathlib import Path

from . import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS meters (
    meter_id   TEXT PRIMARY KEY,
    name       TEXT NOT NULL DEFAULT '',
    location   TEXT NOT NULL DEFAULT '',
    full_scale REAL NOT NULL DEFAULT 99999.9,
    ct_ratio   REAL NOT NULL DEFAULT 1.0
);
CREATE TABLE IF NOT EXISTS readings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    meter_id    TEXT NOT NULL,
    read_at     TEXT NOT NULL,
    reading_kwh REAL NOT NULL,
    kind        TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    UNIQUE (meter_id, read_at, kind)
);
CREATE TABLE IF NOT EXISTS changes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    meter_id    TEXT NOT NULL,
    change_date TEXT NOT NULL,
    payload     TEXT NOT NULL,
    UNIQUE (meter_id, change_date)
);
CREATE TABLE IF NOT EXISTS runs (
    run_id     TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    month      TEXT NOT NULL,
    meters     TEXT NOT NULL,
    payload    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    at      TEXT NOT NULL,
    kind    TEXT NOT NULL,
    summary TEXT NOT NULL,
    detail  TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS leases (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    meter_id    TEXT NOT NULL,
    tenant_id   TEXT NOT NULL,
    tenant_name TEXT NOT NULL DEFAULT '',
    start_date  TEXT NOT NULL,
    end_date    TEXT NOT NULL DEFAULT '2999-12-31',
    payload     TEXT NOT NULL,
    UNIQUE (meter_id, tenant_id, start_date)
);
CREATE TABLE IF NOT EXISTS handovers (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    meter_id    TEXT NOT NULL,
    handover_at TEXT NOT NULL,
    kind        TEXT NOT NULL,
    reading_kwh REAL NOT NULL,
    payload     TEXT NOT NULL DEFAULT '{}',
    UNIQUE (meter_id, handover_at, kind)
);
CREATE TABLE IF NOT EXISTS billing_runs (
    run_id     TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    month      TEXT NOT NULL,
    status     TEXT NOT NULL,
    meters     TEXT NOT NULL,
    payload    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bills (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id  TEXT NOT NULL,
    run_id      TEXT NOT NULL,
    meter_id    TEXT NOT NULL,
    tenant_id   TEXT NOT NULL,
    month       TEXT NOT NULL,
    kind        TEXT NOT NULL,
    amount_kwh  REAL NOT NULL,
    UNIQUE (version_id, meter_id, tenant_id, month, kind)
);
CREATE TABLE IF NOT EXISTS billing_versions (
    version_id   TEXT PRIMARY KEY,
    created_at   TEXT NOT NULL,
    run_id       TEXT NOT NULL,
    month        TEXT NOT NULL,
    status       TEXT NOT NULL,
    input_sig    TEXT NOT NULL,
    payload      TEXT NOT NULL
);
"""


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    path = Path(db_path or settings.DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


@contextmanager
def session(db_path: Path | None = None):
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


# --------------------------------------------------------------------------- 表计台账


def upsert_meters(conn: sqlite3.Connection, meters: list[dict]) -> None:
    for meter in meters:
        conn.execute(
            """
            INSERT INTO meters (meter_id, name, location, full_scale, ct_ratio)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(meter_id) DO UPDATE SET
                name = excluded.name,
                location = excluded.location,
                full_scale = excluded.full_scale,
                ct_ratio = excluded.ct_ratio
            """,
            (
                meter["meter_id"],
                meter.get("name", ""),
                meter.get("location", ""),
                float(meter.get("full_scale") or settings.DEFAULT_FULL_SCALE),
                float(meter.get("ct_ratio") or 1.0),
            ),
        )


def all_meters(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM meters ORDER BY meter_id").fetchall()
    return [
        {
            "meter_id": row["meter_id"],
            "name": row["name"],
            "location": row["location"],
            "full_scale": float(row["full_scale"]),
            "ct_ratio": float(row["ct_ratio"]),
        }
        for row in rows
    ]


# --------------------------------------------------------------------------- 读数


def replace_readings(conn: sqlite3.Connection, rows: list[dict], now: str) -> int:
    conn.execute("DELETE FROM readings")
    conn.executemany(
        "INSERT INTO readings (meter_id, read_at, reading_kwh, kind, imported_at) VALUES (?, ?, ?, ?, ?)",
        [
            (row["meter_id"], row["read_at"].isoformat(), float(row["reading_kwh"]), row["kind"], now)
            for row in rows
        ],
    )
    return len(rows)


def all_readings(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT meter_id, read_at, reading_kwh, kind FROM readings ORDER BY meter_id, read_at, kind"
    ).fetchall()
    return [
        {
            "meter_id": row["meter_id"],
            "read_at": date.fromisoformat(row["read_at"]),
            "reading_kwh": float(row["reading_kwh"]),
            "kind": row["kind"],
        }
        for row in rows
    ]


def count_readings(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS c FROM readings").fetchone()
    return int(row["c"])


def reading_span(conn: sqlite3.Connection) -> dict:
    row = conn.execute("SELECT MIN(read_at) AS lo, MAX(read_at) AS hi FROM readings").fetchone()
    return {"date_from": row["lo"], "date_to": row["hi"]}


# --------------------------------------------------------------------------- 换表记录


def replace_changes(conn: sqlite3.Connection, changes: list[dict]) -> int:
    conn.execute("DELETE FROM changes")
    for change in changes:
        conn.execute(
            "INSERT INTO changes (meter_id, change_date, payload) VALUES (?, ?, ?)",
            (change["meter_id"], change["change_date"], json.dumps(change, ensure_ascii=False)),
        )
    return len(changes)


def all_changes(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT payload FROM changes ORDER BY change_date").fetchall()
    return [json.loads(row["payload"]) for row in rows]


def count_changes(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS c FROM changes").fetchone()
    return int(row["c"])


# --------------------------------------------------------------------------- 还原批次


def save_run(conn: sqlite3.Connection, payload: dict) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO runs (run_id, created_at, month, meters, payload) VALUES (?, ?, ?, ?, ?)",
        (
            payload["run_id"],
            payload["created_at"],
            payload.get("month") or "",
            ",".join(payload.get("selected") or []),
            json.dumps(payload, ensure_ascii=False),
        ),
    )


def load_run(conn: sqlite3.Connection, run_id: str) -> dict | None:
    row = conn.execute("SELECT payload FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    return json.loads(row["payload"]) if row else None


def latest_run(conn: sqlite3.Connection, month: str | None = None) -> dict | None:
    if month:
        row = conn.execute(
            "SELECT payload FROM runs WHERE month = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (month,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT payload FROM runs ORDER BY created_at DESC, rowid DESC LIMIT 1"
        ).fetchone()
    return json.loads(row["payload"]) if row else None


def list_runs(conn: sqlite3.Connection, limit: int = 10) -> list[dict]:
    rows = conn.execute(
        "SELECT run_id, created_at, month, meters FROM runs ORDER BY created_at DESC, rowid DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [
        {
            "run_id": row["run_id"],
            "created_at": row["created_at"],
            "month": row["month"],
            "meters": row["meters"].split(",") if row["meters"] else [],
        }
        for row in rows
    ]


# --------------------------------------------------------------------------- 租约 / 交接读数


def replace_leases(conn: sqlite3.Connection, leases: list[dict]) -> int:
    conn.execute("DELETE FROM leases")
    for lease in leases:
        conn.execute(
            "INSERT INTO leases (meter_id, tenant_id, tenant_name, start_date, end_date, payload)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                lease["meter_id"],
                lease["tenant_id"],
                lease.get("tenant_name", ""),
                lease["start_date"].isoformat()
                if isinstance(lease["start_date"], date)
                else str(lease["start_date"]),
                lease["end_date"].isoformat()
                if isinstance(lease["end_date"], date)
                else str(lease["end_date"]),
                json.dumps(lease, ensure_ascii=False, default=str),
            ),
        )
    return len(leases)


def all_leases(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT payload FROM leases ORDER BY meter_id, start_date, tenant_id"
    ).fetchall()
    leases = [json.loads(row["payload"]) for row in rows]
    for lease in leases:
        if not isinstance(lease["start_date"], date):
            lease["start_date"] = date.fromisoformat(lease["start_date"])
        if not isinstance(lease["end_date"], date):
            lease["end_date"] = date.fromisoformat(lease["end_date"])
    return leases


def count_leases(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) AS c FROM leases").fetchone()["c"])


def replace_handovers(conn: sqlite3.Connection, handovers: list[dict]) -> int:
    conn.execute("DELETE FROM handovers")
    conn.executemany(
        "INSERT INTO handovers (meter_id, handover_at, kind, reading_kwh, payload)"
        " VALUES (?, ?, ?, ?, ?)",
        [
            (
                row["meter_id"],
                row["handover_at"].isoformat()
                if isinstance(row["handover_at"], date)
                else str(row["handover_at"]),
                row["kind"],
                float(row["reading_kwh"]),
                json.dumps(row, ensure_ascii=False, default=str),
            )
            for row in handovers
        ],
    )
    return len(handovers)


def all_handovers(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT meter_id, handover_at, kind, reading_kwh FROM handovers"
        " ORDER BY meter_id, handover_at, kind"
    ).fetchall()
    return [
        {
            "meter_id": row["meter_id"],
            "handover_at": date.fromisoformat(row["handover_at"]),
            "kind": row["kind"],
            "reading_kwh": float(row["reading_kwh"]),
        }
        for row in rows
    ]


def count_handovers(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) AS c FROM handovers").fetchone()["c"])


# --------------------------------------------------------------------------- 账单批次 / 发布版本


def save_billing_run(conn: sqlite3.Connection, payload: dict) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO billing_runs (run_id, created_at, month, status, meters, payload)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (
            payload["run_id"],
            payload["created_at"],
            payload.get("month") or "",
            payload.get("status", "rejected"),
            ",".join(payload.get("selected") or []),
            json.dumps(payload, ensure_ascii=False, default=str),
        ),
    )


def load_billing_run(conn: sqlite3.Connection, run_id: str) -> dict | None:
    row = conn.execute("SELECT payload FROM billing_runs WHERE run_id = ?", (run_id,)).fetchone()
    return json.loads(row["payload"]) if row else None


def get_billing_run_by_idem(conn: sqlite3.Connection, idem_key: str) -> dict | None:
    rows = conn.execute("SELECT payload FROM billing_runs ORDER BY rowid").fetchall()
    for row in rows:
        payload = json.loads(row["payload"])
        if payload.get("idempotency_key") == idem_key:
            return payload
    return None


def list_billing_runs(conn: sqlite3.Connection, limit: int = 20) -> list[dict]:
    rows = conn.execute(
        "SELECT run_id, created_at, month, status, meters FROM billing_runs"
        " ORDER BY created_at DESC, rowid DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [
        {
            "run_id": row["run_id"],
            "created_at": row["created_at"],
            "month": row["month"],
            "status": row["status"],
            "meters": row["meters"].split(",") if row["meters"] else [],
        }
        for row in rows
    ]


def insert_bills(conn: sqlite3.Connection, version_id: str, run_id: str, bill_rows: list[dict]) -> int:
    conn.executemany(
        "INSERT INTO bills (version_id, run_id, meter_id, tenant_id, month, kind, amount_kwh)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (
                version_id,
                run_id,
                row["meter_id"],
                row["tenant_id"],
                row["month"],
                row["kind"],
                float(row["amount_kwh"]),
            )
            for row in bill_rows
        ],
    )
    return len(bill_rows)


def save_billing_version(conn: sqlite3.Connection, payload: dict) -> None:
    conn.execute(
        "INSERT INTO billing_versions (version_id, created_at, run_id, month, status, input_sig, payload)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            payload["version_id"],
            payload["created_at"],
            payload["run_id"],
            payload.get("month", ""),
            payload["status"],
            payload.get("input_sig", ""),
            json.dumps(payload, ensure_ascii=False, default=str),
        ),
    )


def get_billing_version(conn: sqlite3.Connection, version_id: str) -> dict | None:
    row = conn.execute(
        "SELECT payload FROM billing_versions WHERE version_id = ?", (version_id,)
    ).fetchone()
    return json.loads(row["payload"]) if row else None


def active_billing_version(conn: sqlite3.Connection, month: str | None = None) -> dict | None:
    if month:
        row = conn.execute(
            "SELECT payload FROM billing_versions WHERE status = 'active' AND month = ?"
            " ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (month,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT payload FROM billing_versions WHERE status = 'active'"
            " ORDER BY created_at DESC, rowid DESC LIMIT 1"
        ).fetchone()
    return json.loads(row["payload"]) if row else None


def list_billing_versions(conn: sqlite3.Connection, limit: int = 20) -> list[dict]:
    rows = conn.execute(
        "SELECT version_id, created_at, run_id, month, status, input_sig"
        " FROM billing_versions ORDER BY created_at DESC, rowid DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(row) for row in rows]


def deactivate_billing_versions(conn: sqlite3.Connection, month: str | None = None) -> int:
    if month:
        cur = conn.execute(
            "UPDATE billing_versions SET status = 'superseded' WHERE status = 'active' AND month = ?",
            (month,),
        )
    else:
        cur = conn.execute("UPDATE billing_versions SET status = 'superseded' WHERE status = 'active'")
    return cur.rowcount


def bills_of_version(conn: sqlite3.Connection, version_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT meter_id, tenant_id, month, kind, amount_kwh FROM bills"
        " WHERE version_id = ? ORDER BY meter_id, tenant_id, month, kind",
        (version_id,),
    ).fetchall()
    return [dict(row) for row in rows]


# --------------------------------------------------------------------------- 事件 / 元数据


def add_event(conn: sqlite3.Connection, kind: str, summary: str, detail: dict, now: str) -> None:
    conn.execute(
        "INSERT INTO events (at, kind, summary, detail) VALUES (?, ?, ?, ?)",
        (now, kind, summary, json.dumps(detail, ensure_ascii=False)),
    )


def list_events(conn: sqlite3.Connection, limit: int = 30) -> list[dict]:
    rows = conn.execute(
        "SELECT at, kind, summary, detail FROM events ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [
        {
            "at": row["at"],
            "kind": row["kind"],
            "summary": row["summary"],
            "detail": json.loads(row["detail"] or "{}"),
        }
        for row in rows
    ]


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
