"""SQLite persistence: rounds, latest samples, active alerts and events.

Every scrape round is persisted in a single transaction together with the
alert events and active-alert changes it produced, so the database never
shows a half-applied round. Event ids come from an AUTOINCREMENT column and
are therefore stable across restarts.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable

from .alerts import EVENT_RESOLVED, RuleTransition, canonical_labels
from .textparse import Sample

SCHEMA = """
CREATE TABLE IF NOT EXISTS rounds (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    target_id    TEXT NOT NULL,
    ts           REAL NOT NULL,          -- wall clock of the round
    ok           INTEGER NOT NULL,       -- 1 success / 0 failure
    error        TEXT,                   -- failure reason when ok = 0
    sample_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_rounds_target ON rounds(target_id, id);

CREATE TABLE IF NOT EXISTS samples (
    target_id   TEXT NOT NULL,
    metric      TEXT NOT NULL,
    labels_key  TEXT NOT NULL,           -- canonical label JSON
    labels_json TEXT NOT NULL,
    value       REAL NOT NULL,
    PRIMARY KEY (target_id, metric, labels_key)
);

CREATE TABLE IF NOT EXISTS active_alerts (
    rule_id     TEXT NOT NULL,
    labels_key  TEXT NOT NULL,           -- canonical label JSON
    target_id   TEXT NOT NULL,
    labels_json TEXT NOT NULL,
    value       REAL NOT NULL,           -- last observed value
    fired_ts    REAL NOT NULL,           -- wall clock when it started firing
    PRIMARY KEY (rule_id, labels_key)
);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    target_id   TEXT NOT NULL,
    rule_id     TEXT NOT NULL,
    labels_json TEXT NOT NULL,
    event       TEXT NOT NULL,           -- 'firing' | 'resolved'
    reason      TEXT,                    -- resolution reason, NULL for firing
    value       REAL NOT NULL
);
"""


class Store:
    def __init__(self, path: str):
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------------
    # writes

    def resolve_firing_on_restart(self, reason: str, ts_wall: float) -> list[sqlite3.Row]:
        """Resolve every alert left firing by a previous run, in one txn."""
        rows = self._conn.execute(
            "SELECT * FROM active_alerts ORDER BY rule_id, labels_key"
        ).fetchall()
        if not rows:
            return []
        with self._conn:  # commits on success, rolls back on exception
            for row in rows:
                self._conn.execute(
                    "INSERT INTO events(ts, target_id, rule_id, labels_json, event, reason, value)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (ts_wall, row["target_id"], row["rule_id"], row["labels_json"],
                     EVENT_RESOLVED, reason, row["value"]),
                )
            self._conn.execute("DELETE FROM active_alerts")
        return rows

    def persist_round(
        self,
        *,
        target_id: str,
        ok: bool,
        error: str | None,
        samples: Iterable[Sample],
        transitions: Iterable[RuleTransition],
        ts_wall: float,
    ) -> int:
        """Persist one scrape round and its alert transitions atomically."""
        sample_list = list(samples)
        with self._conn:
            cur = self._conn.execute(
                "INSERT INTO rounds(target_id, ts, ok, error, sample_count) VALUES (?,?,?,?,?)",
                (target_id, ts_wall, int(ok), error, len(sample_list) if ok else 0),
            )
            round_id = cur.lastrowid
            if ok:
                # Replace the target's sample snapshot; a failed round keeps
                # the previous snapshot and never publishes partial data.
                self._conn.execute("DELETE FROM samples WHERE target_id = ?", (target_id,))
                self._conn.executemany(
                    "INSERT INTO samples(target_id, metric, labels_key, labels_json, value)"
                    " VALUES (?,?,?,?,?)",
                    [
                        (target_id, s.metric, canonical_labels(s.labels_dict),
                         canonical_labels(s.labels_dict), s.value)
                        for s in sample_list
                    ],
                )
            for tr in transitions:
                for ev in tr.events:
                    self._conn.execute(
                        "INSERT INTO events(ts, target_id, rule_id, labels_json, event, reason, value)"
                        " VALUES (?,?,?,?,?,?,?)",
                        (ev.ts_wall, ev.target_id, ev.rule_id, ev.labels_json,
                         ev.event, ev.reason, ev.value),
                    )
                for key, (labels_json, value, fired_wall) in tr.upserts.items():
                    self._conn.execute(
                        "INSERT INTO active_alerts(rule_id, labels_key, target_id, labels_json, value, fired_ts)"
                        " VALUES (?,?,?,?,?,?)"
                        " ON CONFLICT(rule_id, labels_key)"
                        " DO UPDATE SET value = excluded.value",
                        (tr.rule_id, key, tr.target_id, labels_json, value, fired_wall),
                    )
                for key in tr.deletes:
                    self._conn.execute(
                        "DELETE FROM active_alerts WHERE rule_id = ? AND labels_key = ?",
                        (tr.rule_id, key),
                    )
        return round_id

    # ------------------------------------------------------------------
    # queries

    def target_health(self, target_id: str) -> dict:
        last = self._conn.execute(
            "SELECT id, ts, ok, error, sample_count FROM rounds"
            " WHERE target_id = ? ORDER BY id DESC LIMIT 1",
            (target_id,),
        ).fetchone()
        last_ok = self._conn.execute(
            "SELECT ts FROM rounds WHERE target_id = ? AND ok = 1"
            " ORDER BY id DESC LIMIT 1",
            (target_id,),
        ).fetchone()
        stats = self._conn.execute(
            "SELECT COUNT(*) AS total, COALESCE(SUM(ok = 0), 0) AS failed"
            " FROM rounds WHERE target_id = ?",
            (target_id,),
        ).fetchone()
        return {
            "last_round_id": last["id"] if last else None,
            "last_ok": bool(last["ok"]) if last else None,
            "last_error": last["error"] if last else None,
            "last_ts": last["ts"] if last else None,
            "last_sample_count": last["sample_count"] if last else None,
            "last_success_ts": last_ok["ts"] if last_ok else None,
            "rounds_total": stats["total"],
            "rounds_failed": stats["failed"],
        }

    def list_samples(self, target_id: str, metric: str | None = None) -> list[sqlite3.Row]:
        if metric is None:
            return self._conn.execute(
                "SELECT metric, labels_json, value FROM samples"
                " WHERE target_id = ? ORDER BY metric, labels_key",
                (target_id,),
            ).fetchall()
        return self._conn.execute(
            "SELECT metric, labels_json, value FROM samples"
            " WHERE target_id = ? AND metric = ? ORDER BY labels_key",
            (target_id, metric),
        ).fetchall()

    def list_active_alerts(self) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT rule_id, target_id, labels_json, value, fired_ts"
            " FROM active_alerts ORDER BY rule_id, labels_key"
        ).fetchall()

    def list_events(self, after_id: int, limit: int) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT id, ts, target_id, rule_id, labels_json, event, reason, value"
            " FROM events WHERE id > ? ORDER BY id ASC LIMIT ?",
            (after_id, limit),
        ).fetchall()
