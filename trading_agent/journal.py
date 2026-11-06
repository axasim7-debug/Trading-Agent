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
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER, symbol TEXT, condition TEXT, price REAL,
    note TEXT, created TEXT, expires TEXT, status TEXT, triggered_at TEXT, trigger_price REAL);
CREATE TABLE IF NOT EXISTS orders (
    run_id INTEGER, ticket INTEGER, symbol TEXT, direction TEXT, order_type TEXT, price REAL,
    volume REAL, stop_loss REAL, take_profit REAL, risk REAL, created TEXT, expires TEXT,
    status TEXT, setup TEXT, reasoning TEXT, win_probability REAL, PRIMARY KEY (run_id, ticket));
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER, bar_time TEXT, symbol TEXT, setup TEXT,
    direction INTEGER, ref_price REAL, stop REAL, target REAL, session TEXT, regime TEXT,
    validated INTEGER, taken INTEGER DEFAULT 0, ticket INTEGER, outcome_r REAL, evaluated INTEGER DEFAULT 0,
    UNIQUE (run_id, bar_time, symbol, setup, direction));
CREATE TABLE IF NOT EXISTS kv (
    run_id INTEGER, key TEXT, value TEXT, updated TEXT, PRIMARY KEY (run_id, key));
"""


def _ts(t: datetime) -> str:
    return t.isoformat(sep=" ", timespec="minutes")


class Journal:
    def __init__(self, path: str = "journal.sqlite"):
        self.db = sqlite3.connect(path, check_same_thread=False, timeout=30)  # callers serialise access
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Add columns introduced after a journal was first created."""
        cols = {r["name"] for r in self.db.execute("PRAGMA table_info(trades)")}
        for name, typ in (("win_probability", "REAL"), ("mae_r", "REAL"), ("mfe_r", "REAL"),
                          ("entry_type", "TEXT")):
            if name not in cols:
                self.db.execute(f"ALTER TABLE trades ADD COLUMN {name} {typ}")
        self.db.commit()

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

    # ---- price alerts (set by the agent, checked by the watchdog) ----------------------------
    def add_alert(self, run_id, symbol, condition, price, note, created, expires) -> int:
        cur = self.db.execute(
            "INSERT INTO alerts (run_id, symbol, condition, price, note, created, expires, status)"
            " VALUES (?,?,?,?,?,?,?, 'active')",
            (run_id, symbol, condition, price, note, _ts(created), _ts(expires)))
        self.db.commit()
        return cur.lastrowid

    def alerts(self, run_id, status: str | None = None) -> list[dict]:
        q, params = "SELECT * FROM alerts WHERE run_id=?", [run_id]
        if status:
            q += " AND status=?"
            params.append(status)
        return [dict(r) for r in self.db.execute(q + " ORDER BY id", params)]

    def set_alert_status(self, alert_id, status, triggered_at=None, price=None):
        self.db.execute("UPDATE alerts SET status=?, triggered_at=COALESCE(?, triggered_at),"
                        " trigger_price=COALESCE(?, trigger_price) WHERE id=?",
                        (status, _ts(triggered_at) if triggered_at else None, price, alert_id))
        self.db.commit()

    def trades_opened_on(self, run_id, day: str) -> int:
        row = self.db.execute("SELECT COUNT(*) AS n FROM trades WHERE run_id=? AND open_time LIKE ?",
                              (run_id, f"{day}%")).fetchone()
        return row["n"]

    def runs(self) -> pd.DataFrame:
        return pd.read_sql("SELECT id, started_at, mode, agent, label FROM runs ORDER BY id", self.db)

    # ---- trades ---------------------------------------------------------------------------
    def record_open(self, run_id, ticket, symbol, direction, volume, entry, sl, tp, risk,
                    open_time, setup, reasoning, win_probability=None, entry_type="market"):
        self.db.execute(
            "INSERT OR REPLACE INTO trades (run_id, ticket, symbol, direction, volume, entry, stop_loss,"
            " take_profit, initial_risk, open_time, setup, reasoning, win_probability, entry_type)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, ticket, symbol, direction, volume, entry, sl, tp, risk, _ts(open_time), setup, reasoning,
             win_probability, entry_type))
        self.db.commit()

    def set_excursions(self, run_id, ticket, mae_r, mfe_r):
        self.db.execute("UPDATE trades SET mae_r=?, mfe_r=? WHERE run_id=? AND ticket=?",
                        (mae_r, mfe_r, run_id, ticket))
        self.db.commit()

    # ---- pending orders --------------------------------------------------------------------
    def record_order(self, run_id, ticket, symbol, direction, order_type, price, volume, sl, tp, risk,
                     created, expires, setup, reasoning, win_probability):
        self.db.execute("INSERT OR REPLACE INTO orders VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 'pending', ?,?,?)",
                        (run_id, ticket, symbol, direction, order_type, price, volume, sl, tp, risk,
                         _ts(created), _ts(expires) if expires else None, setup, reasoning, win_probability))
        self.db.commit()

    def orders(self, run_id, status: str | None = "pending") -> list[dict]:
        q, params = "SELECT * FROM orders WHERE run_id=?", [run_id]
        if status:
            q += " AND status=?"
            params.append(status)
        return [dict(r) for r in self.db.execute(q, params)]

    def set_order_status(self, run_id, ticket, status):
        self.db.execute("UPDATE orders SET status=? WHERE run_id=? AND ticket=?", (status, run_id, ticket))
        self.db.commit()

    # ---- shadow book of playbook signals ---------------------------------------------------
    def record_signal(self, run_id, s: dict, validated: bool) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO signals (run_id, bar_time, symbol, setup, direction, ref_price, stop, target,"
            " session, regime, validated) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, _ts(s["bar_time"]), s["symbol"], s["setup"], s["dir"], s["ref_close"], s["suggested_stop"],
             s["suggested_target"], s["session"], s["regime"], int(validated)))
        self.db.commit()

    def mark_signal_taken(self, run_id, symbol, setup, direction: int, ticket) -> None:
        row = self.db.execute(
            "SELECT id FROM signals WHERE run_id=? AND symbol=? AND setup=? AND direction=? AND taken=0"
            " ORDER BY id DESC LIMIT 1", (run_id, symbol, setup, direction)).fetchone()
        if row:
            self.db.execute("UPDATE signals SET taken=1, ticket=? WHERE id=?", (ticket, row["id"]))
            self.db.commit()

    def signals(self, run_id, evaluated: bool | None = None) -> list[dict]:
        q, params = "SELECT * FROM signals WHERE run_id=?", [run_id]
        if evaluated is not None:
            q += " AND evaluated=?"
            params.append(int(evaluated))
        return [dict(r) for r in self.db.execute(q + " ORDER BY id", params)]

    def set_signal_outcome(self, signal_id, r):
        self.db.execute("UPDATE signals SET outcome_r=?, evaluated=1 WHERE id=?", (r, signal_id))
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

    def record_close(self, run_id, ct) -> bool:
        """Returns True if this call closed a trade that was open in the journal."""
        row = self.db.execute("SELECT initial_risk, close_time FROM trades WHERE run_id=? AND ticket=?",
                              (run_id, ct.ticket)).fetchone()
        if row and row["close_time"] is not None:
            return False
        risk = row["initial_risk"] if row else None
        r_mult = ct.profit / risk if risk else None
        self.db.execute(
            "UPDATE trades SET close_time=?, exit_price=?, profit=?, r_multiple=?, close_reason=?"
            " WHERE run_id=? AND ticket=?",
            (_ts(ct.close_time), ct.exit_price, ct.profit, r_mult, ct.close_reason, run_id, ct.ticket))
        self.db.commit()
        return row is not None

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
