"""SQLite (WAL) storage for repeater telemetry."""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

from telemetry.battery import estimate_percent

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS repeaters (
    key          TEXT PRIMARY KEY,
    public_id    TEXT NOT NULL UNIQUE,
    name         TEXT NOT NULL,
    public_key   TEXT,
    path         TEXT,
    first_seen   INTEGER NOT NULL,
    last_success INTEGER,
    last_attempt INTEGER
);

CREATE TABLE IF NOT EXISTS readings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    repeater_key    TEXT NOT NULL REFERENCES repeaters(key),
    name            TEXT NOT NULL,
    ts              INTEGER NOT NULL,
    battery_voltage REAL,
    battery_percent REAL,
    battery_percent_source TEXT,
    temperature_c   REAL,
    humidity        REAL,
    pressure        REAL,
    attempt         INTEGER NOT NULL DEFAULT 1,
    raw_lpp         TEXT
);

CREATE INDEX IF NOT EXISTS idx_readings_key_ts ON readings (repeater_key, ts);
CREATE INDEX IF NOT EXISTS idx_readings_ts ON readings (ts);

CREATE TABLE IF NOT EXISTS poll_attempts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    repeater_key TEXT NOT NULL,
    name         TEXT NOT NULL,
    ts           INTEGER NOT NULL,
    success      INTEGER NOT NULL,
    attempts     INTEGER NOT NULL,
    error        TEXT
);

CREATE INDEX IF NOT EXISTS idx_attempts_ts ON poll_attempts (ts);

CREATE TABLE IF NOT EXISTS stats (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    repeater_key    TEXT NOT NULL REFERENCES repeaters(key),
    name            TEXT NOT NULL,
    ts              INTEGER NOT NULL,
    battery_mv      INTEGER,
    uptime_s        INTEGER,
    airtime_ms      INTEGER,
    rx_airtime_ms   INTEGER,
    noise_floor_dbm INTEGER,
    last_rssi_dbm   INTEGER,
    last_snr_db     REAL,
    tx_queue_len    INTEGER,
    nb_sent         INTEGER,
    nb_recv         INTEGER,
    sent_flood      INTEGER,
    sent_direct     INTEGER,
    recv_flood      INTEGER,
    recv_direct     INTEGER,
    direct_dups     INTEGER,
    flood_dups      INTEGER,
    full_evts       INTEGER,
    recv_errors     INTEGER,
    raw_json        TEXT
);

CREATE INDEX IF NOT EXISTS idx_stats_key_ts ON stats (repeater_key, ts);
CREATE INDEX IF NOT EXISTS idx_stats_ts ON stats (ts);
"""

# Grafana reads this view for the "current state" panels.
LATEST_VIEW = """
CREATE VIEW IF NOT EXISTS latest_readings AS
SELECT r.*
FROM readings r
JOIN (
    SELECT repeater_key, MAX(ts) AS ts FROM readings GROUP BY repeater_key
) m ON m.repeater_key = r.repeater_key AND m.ts = r.ts;

CREATE VIEW IF NOT EXISTS latest_stats AS
SELECT s.*
FROM stats s
JOIN (
    SELECT repeater_key, MAX(ts) AS ts FROM stats GROUP BY repeater_key
) m ON m.repeater_key = s.repeater_key AND m.ts = s.ts;
"""

# Maps meshcore's req_status_sync() payload keys to our column names. Units
# per the MeshCore firmware: airtime/rx_airtime in ms, uptime in seconds,
# noise_floor/last_rssi in dBm, last_snr in dB, bat in mV.
_STATUS_FIELDS: dict[str, str] = {
    "bat": "battery_mv",
    "uptime": "uptime_s",
    "airtime": "airtime_ms",
    "rx_airtime": "rx_airtime_ms",
    "noise_floor": "noise_floor_dbm",
    "last_rssi": "last_rssi_dbm",
    "last_snr": "last_snr_db",
    "tx_queue_len": "tx_queue_len",
    "nb_sent": "nb_sent",
    "nb_recv": "nb_recv",
    "sent_flood": "sent_flood",
    "sent_direct": "sent_direct",
    "recv_flood": "recv_flood",
    "recv_direct": "recv_direct",
    "direct_dups": "direct_dups",
    "flood_dups": "flood_dups",
    "full_evts": "full_evts",
    "recv_errors": "recv_errors",
}


def _extract(lpp: Iterable[dict[str, Any]] | None) -> dict[str, float | str | None]:
    """Pull the interesting fields out of a parsed Cayenne LPP frame.

    If the frame has no reported battery percentage, one is estimated from
    voltage (see telemetry.battery) and flagged via battery_percent_source.
    """
    values: dict[str, float | str | None] = {
        "battery_voltage": None,
        "battery_percent": None,
        "battery_percent_source": None,
        "temperature_c": None,
        "humidity": None,
        "pressure": None,
    }
    if not lpp:
        return values

    by_type = {
        "voltage": "battery_voltage",
        "percentage": "battery_percent",
        "temperature": "temperature_c",
        "humidity": "humidity",
        "barometer": "pressure",
    }
    for entry in lpp:
        if not isinstance(entry, dict):
            continue
        field = by_type.get(str(entry.get("type")))
        if field is None or values[field] is not None:
            continue
        value = entry.get("value")
        if isinstance(value, (int, float)):
            values[field] = float(value)

    if values["battery_percent"] is not None:
        values["battery_percent_source"] = "device"
    else:
        estimated = estimate_percent(values["battery_voltage"])
        if estimated is not None:
            values["battery_percent"] = estimated
            values["battery_percent_source"] = "estimated"

    return values


class Database:
    """Thin asyncio wrapper around a WAL-mode SQLite connection.

    All statements run on a worker thread so the event loop that drives the
    radio is never blocked by disk I/O.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._conn: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        await asyncio.to_thread(self._connect_sync)

    def _connect_sync(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self._path, isolation_level=None, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(SCHEMA)
        conn.executescript(LATEST_VIEW)
        self._migrate_sync(conn)
        self._conn = conn
        logger.info("SQLite ready at %s (WAL)", self._path)

    @staticmethod
    def _migrate_sync(conn: sqlite3.Connection) -> None:
        """Add columns introduced after a database's initial creation."""
        reading_cols = {row["name"] for row in conn.execute("PRAGMA table_info(readings)")}
        if "battery_percent_source" not in reading_cols:
            conn.execute("ALTER TABLE readings ADD COLUMN battery_percent_source TEXT")

        repeater_cols = {row["name"] for row in conn.execute("PRAGMA table_info(repeaters)")}
        if "public_id" not in repeater_cols:
            conn.execute("ALTER TABLE repeaters ADD COLUMN public_id TEXT")
            conn.execute("UPDATE repeaters SET public_id = lower(hex(randomblob(8)))")
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_repeaters_public_id "
                "ON repeaters (public_id)"
            )

    async def close(self) -> None:
        if self._conn is not None:
            conn, self._conn = self._conn, None
            await asyncio.to_thread(conn.close)

    def _require_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("database is not connected")
        return self._conn

    async def register_repeater(
        self, key: str, name: str, public_key: str | None, path: str | None
    ) -> None:
        now = int(time.time())
        public_id = secrets.token_hex(8)

        def _run() -> None:
            conn = self._require_conn()
            conn.execute(
                """
                INSERT INTO repeaters (key, public_id, name, public_key, path, first_seen)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    name = excluded.name,
                    public_key = excluded.public_key,
                    path = excluded.path
                """,
                (key, public_id, name, public_key, path, now),
            )

        async with self._lock:
            await asyncio.to_thread(_run)

    async def record_reading(
        self,
        key: str,
        name: str,
        lpp: list[dict[str, Any]] | None,
        attempt: int,
        ts: int | None = None,
    ) -> dict[str, float | str | None]:
        values = _extract(lpp)
        stamp = int(ts if ts is not None else time.time())

        def _run() -> None:
            conn = self._require_conn()
            conn.execute(
                """
                INSERT INTO readings (
                    repeater_key, name, ts, battery_voltage, battery_percent,
                    battery_percent_source, temperature_c, humidity, pressure,
                    attempt, raw_lpp
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    key,
                    name,
                    stamp,
                    values["battery_voltage"],
                    values["battery_percent"],
                    values["battery_percent_source"],
                    values["temperature_c"],
                    values["humidity"],
                    values["pressure"],
                    attempt,
                    json.dumps(lpp) if lpp is not None else None,
                ),
            )
            conn.execute("UPDATE repeaters SET last_success = ? WHERE key = ?", (stamp, key))

        async with self._lock:
            await asyncio.to_thread(_run)
        return values

    async def record_stats(
        self,
        key: str,
        name: str,
        status: dict[str, Any],
        ts: int | None = None,
    ) -> dict[str, Any]:
        """Store a req_status_sync() payload: uptime, airtime, packet counts, noise floor."""
        values = {col: status.get(field) for field, col in _STATUS_FIELDS.items()}
        stamp = int(ts if ts is not None else time.time())
        columns = list(values.keys())

        def _run() -> None:
            conn = self._require_conn()
            placeholders = ", ".join(["?"] * (len(columns) + 4))
            conn.execute(
                f"""
                INSERT INTO stats (
                    repeater_key, name, ts, {", ".join(columns)}, raw_json
                ) VALUES ({placeholders})
                """,
                (key, name, stamp, *values.values(), json.dumps(status)),
            )

        async with self._lock:
            await asyncio.to_thread(_run)
        return values

    async def record_attempt(
        self, key: str, name: str, success: bool, attempts: int, error: str | None
    ) -> None:
        stamp = int(time.time())

        def _run() -> None:
            conn = self._require_conn()
            conn.execute(
                """
                INSERT INTO poll_attempts (repeater_key, name, ts, success, attempts, error)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (key, name, stamp, 1 if success else 0, attempts, error),
            )
            conn.execute("UPDATE repeaters SET last_attempt = ? WHERE key = ?", (stamp, key))

        async with self._lock:
            await asyncio.to_thread(_run)

    async def purge(self, retention_days: int) -> int:
        if retention_days <= 0:
            return 0
        cutoff = int(time.time()) - retention_days * 86400

        def _run() -> int:
            conn = self._require_conn()
            deleted = conn.execute("DELETE FROM readings WHERE ts < ?", (cutoff,)).rowcount
            conn.execute("DELETE FROM poll_attempts WHERE ts < ?", (cutoff,))
            return deleted or 0

        async with self._lock:
            return await asyncio.to_thread(_run)

    async def public_snapshot(self, generated_at: int | None = None) -> dict[str, Any]:
        """Return sanitized current data and a manifest of retained UTC days."""
        stamp = int(generated_at if generated_at is not None else time.time())

        def _run() -> dict[str, Any]:
            conn = self._require_conn()
            current_rows = conn.execute(
                """
                SELECT
                    p.public_id AS id, p.name, p.last_attempt, p.last_success,
                    r.battery_voltage, r.battery_percent, r.battery_percent_source,
                    r.temperature_c, r.humidity, r.pressure,
                    s.uptime_s, s.airtime_ms, s.rx_airtime_ms,
                    s.noise_floor_dbm, s.last_rssi_dbm, s.last_snr_db,
                    s.tx_queue_len, s.nb_sent, s.nb_recv, s.sent_flood,
                    s.sent_direct, s.recv_flood, s.recv_direct, s.direct_dups,
                    s.flood_dups, s.full_evts, s.recv_errors,
                    a.success AS latest_attempt_success
                FROM repeaters p
                LEFT JOIN readings r ON r.id = (
                    SELECT id FROM readings
                    WHERE repeater_key = p.key ORDER BY ts DESC, id DESC LIMIT 1
                )
                LEFT JOIN stats s ON s.id = (
                    SELECT id FROM stats
                    WHERE repeater_key = p.key ORDER BY ts DESC, id DESC LIMIT 1
                )
                LEFT JOIN poll_attempts a ON a.id = (
                    SELECT id FROM poll_attempts
                    WHERE repeater_key = p.key ORDER BY ts DESC, id DESC LIMIT 1
                )
                ORDER BY p.name
                """
            ).fetchall()
            day_rows = conn.execute(
                """
                WITH events AS (
                    SELECT ts, 'reading' AS kind FROM readings
                    UNION ALL SELECT ts, 'stat' FROM stats
                    UNION ALL SELECT ts, 'attempt' FROM poll_attempts
                )
                SELECT date(ts, 'unixepoch') AS day,
                       SUM(kind = 'reading') AS readings,
                       SUM(kind = 'stat') AS stats,
                       SUM(kind = 'attempt') AS attempts
                FROM events
                GROUP BY day
                ORDER BY day
                """
            ).fetchall()

            repeaters = []
            for row in current_rows:
                item = dict(row)
                latest_success = item.pop("latest_attempt_success")
                item["online"] = bool(latest_success) if latest_success is not None else False
                repeaters.append(item)

            return {
                "current": {
                    "schema_version": 1,
                    "generated_at": datetime.fromtimestamp(stamp, UTC).isoformat(),
                    "repeaters": repeaters,
                },
                "summary": {
                    "schema_version": 1,
                    "generated_at": datetime.fromtimestamp(stamp, UTC).isoformat(),
                    "repeaters": [
                        {"id": row["id"], "name": row["name"]} for row in current_rows
                    ],
                    "days": [dict(row) for row in day_rows],
                },
            }

        async with self._lock:
            return await asyncio.to_thread(_run)

    async def public_day(self, day: str) -> dict[str, Any]:
        """Return sanitized telemetry for one UTC calendar day."""
        try:
            start = int(datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=UTC).timestamp())
        except ValueError as exc:
            raise ValueError(f"invalid UTC day: {day}") from exc
        end = start + 86400

        def _run() -> dict[str, Any]:
            conn = self._require_conn()
            readings = conn.execute(
                """
                SELECT p.public_id AS id, r.name, r.ts, r.battery_voltage,
                       r.battery_percent, r.battery_percent_source,
                       r.temperature_c, r.humidity, r.pressure, r.attempt
                FROM readings r
                JOIN repeaters p ON p.key = r.repeater_key
                WHERE r.ts >= ? AND r.ts < ?
                ORDER BY r.ts, r.name
                """,
                (start, end),
            ).fetchall()
            stats = conn.execute(
                """
                SELECT p.public_id AS id, s.name, s.ts, s.battery_mv, s.uptime_s,
                       s.airtime_ms, s.rx_airtime_ms, s.noise_floor_dbm,
                       s.last_rssi_dbm, s.last_snr_db, s.tx_queue_len,
                       s.nb_sent, s.nb_recv, s.sent_flood, s.sent_direct,
                       s.recv_flood, s.recv_direct, s.direct_dups, s.flood_dups,
                       s.full_evts, s.recv_errors
                FROM stats s
                JOIN repeaters p ON p.key = s.repeater_key
                WHERE s.ts >= ? AND s.ts < ?
                ORDER BY s.ts, s.name
                """,
                (start, end),
            ).fetchall()
            attempts = conn.execute(
                """
                SELECT p.public_id AS id, a.name, a.ts, a.success, a.attempts
                FROM poll_attempts a
                JOIN repeaters p ON p.key = a.repeater_key
                WHERE a.ts >= ? AND a.ts < ?
                ORDER BY a.ts, a.name
                """,
                (start, end),
            ).fetchall()
            return {
                "schema_version": 1,
                "day": day,
                "readings": [dict(row) for row in readings],
                "stats": [dict(row) for row in stats],
                "attempts": [
                    {**dict(row), "success": bool(row["success"])} for row in attempts
                ],
            }

        async with self._lock:
            return await asyncio.to_thread(_run)
