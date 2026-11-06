"""SQLite journal: every run, trade, decision cycle, note and equity point."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime

import pandas as pd

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT, mode TEXT, agent TEXT, label TEXT, config TEXT);
CREATE TABLE IF NOT EXISTS trades (
    run_id INTEGER, ticket INTEGER, symbol TEXT, direction TEXT, volume REAL,
    entry REAL, stop_loss REAL, take_profit REAL, initial_risk REAL,
    open_time TEXT, close_time TEXT, exit_price REAL, profit REAL, r_multiple REAL,
    close_reason TEXT, setup TEXT, reasoning TEXT, close_reasoning TEXT,
    PRIMARY KEY (run_id, ticket));
CREATE TABLE IF NOT EXISTS events (
    run_id INTEGER, time TEXT, kind TEXT, text TEXT, data TEXT);
CREATE TABLE IF NOT EXISTS notes (
    run_id INTEGER, time TEXT, text TEXT);
CREATE TABLE IF NOT EXISTS equity (
    run_id INTEGER, time TEXT, balance REAL, equity REAL);
CREATE TABLE IF NOT EXISTS kv (
    run_id INTEGER, key TEXT, value TEXT, updated TEXT, PRIMARY KEY (run_id, key));
"""


def _ts(t: datetime) -> str:
    return t.isoformat(sep=" ", timespec="minutes")


class Journal:
    def __init__(self, path: str = "journal.sqlite"):
        self.db = sqlite3.connect(path, check_same_thread=False)  # callers serialise access
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    # ---- runs -----------------------------------------------------------------------------
    def start_run(self, mode: str, agent: str, label: str = "", config: dict | None = None) -> int:
        cur = self.db.execute(
            "INSERT INTO runs (started_at, mode, agent, label, config) VALUES (?,?,?,?,?)",
            (datetime.now().isoformat(timespec="seconds"), mode, agent, label,
             json.dumps(config or {}, default=str)))
        self.db.commit()
        return cur.lastrowid

    def get_or_create_run(self, mode: str, agent: str, label: str, config: dict | None = None) -> int:
        """Persistent run (e.g. the live demo account) shared by every Claude Code session."""
        row = self.db.execute("SELECT id FROM runs WHERE mode=? AND label=? ORDER BY id DESC LIMIT 1",
                              (mode, label)).fetchone()
        return row["id"] if row else self.start_run(mode, agent, label, config)

    # ---- key/value state (daily start equity, trading plans, ...) --------------------------
    def get_kv(self, run_id, key, default=None):
        row = self.db.execute("SELECT value FROM kv WHERE run_id=? AND key=?", (run_id, key)).fetchone()
        return json.loads(row["value"]) if row else default

    def set_kv(self, run_id, key, value):
        self.db.execute("INSERT OR REPLACE INTO kv VALUES (?,?,?,?)",
                        (run_id, key, json.dumps(value, default=str, ensure_ascii=False),
                         datetime.now().isoformat(timespec="seconds")))
        self.db.commit()

    def trades_opened_on(self, run_id, day: str) -> int:
        row = self.db.execute("SELECT COUNT(*) AS n FROM trades WHERE run_id=? AND open_time LIKE ?",
                              (run_id, f"{day}%")).fetchone()
        return row["n"]

    def runs(self) -> pd.DataFrame:
        return pd.read_sql("SELECT id, started_at, mode, agent, label FROM runs ORDER BY id", self.db)

    # ---- trades ---------------------------------------------------------------------------
    def record_open(self, run_id, ticket, symbol, direction, volume, entry, sl, tp, risk,
                    open_time, setup, reasoning):
        self.db.execute(
            "INSERT OR REPLACE INTO trades (run_id, ticket, symbol, direction, volume, entry, stop_loss,"
            " take_profit, initial_risk, open_time, setup, reasoning) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, ticket, symbol, direction, volume, entry, sl, tp, risk, _ts(open_time), setup, reasoning))
        self.db.commit()

    def update_levels(self, run_id, ticket, sl=None, tp=None):
        if sl is not None:
            self.db.execute("UPDATE trades SET stop_loss=? WHERE run_id=? AND ticket=?", (sl, run_id, ticket))
        if tp is not None:
            self.db.execute("UPDATE trades SET take_profit=? WHERE run_id=? AND ticket=?", (tp, run_id, ticket))
        self.db.commit()

    def set_close_reasoning(self, run_id, ticket, text):
        self.db.execute("UPDATE trades SET close_reasoning=? WHERE run_id=? AND ticket=?", (text, run_id, ticket))
        self.db.commit()

    def record_close(self, run_id, ct) -> None:
        row = self.db.execute("SELECT initial_risk FROM trades WHERE run_id=? AND ticket=?",
                              (run_id, ct.ticket)).fetchone()
        risk = row["initial_risk"] if row else None
        r_mult = ct.profit / risk if risk else None
        self.db.execute(
            "UPDATE trades SET close_time=?, exit_price=?, profit=?, r_multiple=?, close_reason=?"
            " WHERE run_id=? AND ticket=?",
            (_ts(ct.close_time), ct.exit_price, ct.profit, r_mult, ct.close_reason, run_id, ct.ticket))
        self.db.commit()

    def open_tickets(self, run_id) -> list[int]:
        rows = self.db.execute("SELECT ticket FROM trades WHERE run_id=? AND close_time IS NULL", (run_id,))
        return [r["ticket"] for r in rows]

    def trades(self, run_id, closed_only=False, limit=None) -> pd.DataFrame:
        q = "SELECT * FROM trades WHERE run_id=?"
        if closed_only:
            q += " AND close_time IS NOT NULL"
        q += " ORDER BY open_time DESC" if limit else " ORDER BY open_time"
        if limit:
            q += f" LIMIT {int(limit)}"
        return pd.read_sql(q, self.db, params=(run_id,))

    # ---- notes / events / equity ----------------------------------------------------------
    def add_note(self, run_id, time, text):
        self.db.execute("INSERT INTO notes VALUES (?,?,?)", (run_id, _ts(time), text))
        self.db.commit()

    def notes(self, run_id, limit=15) -> list[dict]:
        rows = self.db.execute("SELECT time, text FROM notes WHERE run_id=? ORDER BY rowid DESC LIMIT ?",
                               (run_id, limit)).fetchall()
        return [dict(r) for r in reversed(rows)]

    def events(self, run_id, kinds: tuple[str, ...] = ("cycle",), limit=10) -> list[dict]:
        q = f"SELECT time, kind, text FROM events WHERE run_id=? AND kind IN ({','.join('?' * len(kinds))})" \
            " ORDER BY rowid DESC LIMIT ?"
        rows = self.db.execute(q, (run_id, *kinds, limit)).fetchall()
        return [dict(r) for r in reversed(rows)]

    def log_event(self, run_id, time, kind, text, data=None):
        self.db.execute("INSERT INTO events VALUES (?,?,?,?,?)",
                        (run_id, _ts(time), kind, text, json.dumps(data, default=str) if data else None))
        self.db.commit()

    def record_equity(self, run_id, time, balance, equity):
        self.db.execute("INSERT INTO equity VALUES (?,?,?,?)", (run_id, _ts(time), balance, equity))
        self.db.commit()

    def equity_curve(self, run_id) -> pd.DataFrame:
        return pd.read_sql("SELECT time, balance, equity FROM equity WHERE run_id=? ORDER BY rowid",
                           self.db, params=(run_id,))

    def usage_totals(self, run_id) -> dict:
        tot: dict[str, float] = {}
        for (data,) in self.db.execute("SELECT data FROM events WHERE run_id=? AND kind='cycle'", (run_id,)):
            for k, v in (json.loads(data or "{}").get("usage") or {}).items():
                if isinstance(v, (int, float)):
                    tot[k] = tot.get(k, 0) + v
        return tot
