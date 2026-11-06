"""Glue: drives the agent through a backtest or a live demo session."""
from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict
from datetime import date
from pathlib import Path

from .agent.prompts import cycle_prompt, review_prompt
from .agent.tools import ToolExecutor, decision_due
from .broker.base import Broker
from .config import Config
from .journal import Journal
from .market import fmt_time
from .risk import RiskManager
from .stats import format_report, performance

log = logging.getLogger(__name__)
STOP_FILE = Path("STOP")  # create this file to stop a live session gracefully


class Session:
    def __init__(self, cfg: Config, broker: Broker, journal: Journal, mode: str, agent_kind: str,
                 label: str = "", agent_factory=None):
        self.cfg = cfg
        self.broker = broker
        self.journal = journal
        self.mode = mode
        self.backtest = mode == "backtest"
        self.hide_dates = self.backtest and cfg.backtest.hide_dates
        self.risk = RiskManager(cfg.risk, broker, cfg.commission_per_lot)
        cfg_dump = asdict(cfg)
        cfg_dump["mt5"].pop("password", None)
        self.run_id = journal.start_run(mode, agent_kind, label, cfg_dump)
        self.executor = ToolExecutor(broker, self.risk, journal, self.run_id, cfg.watchlist, self.hide_dates)
        self.agent = agent_factory(self.executor) if agent_factory else None
        self.cycles = 0
        self._last_day: date | None = None

    # ---- bookkeeping ------------------------------------------------------------------------
    def sync_closed(self) -> None:
        for ticket in self.journal.open_tickets(self.run_id):
            ct = self.broker.closed_trade(ticket)
            if ct is not None:
                self.journal.record_close(self.run_id, ct)

    def _t(self, t):
        return fmt_time(t, self.hide_dates)

    # ---- cycles -----------------------------------------------------------------------------
    def decision_cycle(self) -> None:
        self.risk.roll_day()
        ex = self.executor
        prompt = cycle_prompt(
            time_str=self._t(self.broker.now()),
            decision_tf=self.cfg.decision_timeframe,
            account=json.dumps(ex.t_get_account_status(), ensure_ascii=False),
            positions=json.dumps(ex.t_get_open_positions(), default=str, ensure_ascii=False),
            overview=ex.t_get_market_overview(),
            notes=self.journal.notes(self.run_id, self.cfg.agent.notes_in_prompt),
        )
        res = self.agent.run_cycle(prompt, allow_trading=True)
        self.cycles += 1
        a = self.broker.account()
        self.journal.record_equity(self.run_id, self.broker.now(), a.balance, a.equity)
        self.journal.log_event(self.run_id, self.broker.now(), "cycle", res.text,
                               {"rounds": res.rounds, "stop_reason": res.stop_reason, "usage": res.usage,
                                "tools": [{k: c[k] for k in ("name", "input", "error")} for c in res.tool_calls]})
        if res.tool_calls or res.text:
            log.info("[%s] cycle %d: %s", self._t(self.broker.now()), self.cycles,
                     ", ".join(f"{c['name']}{'!' if c['error'] else ''}" for c in res.tool_calls) or "no tools")

    def daily_review(self, day: date) -> None:
        if not self.cfg.agent.daily_review or getattr(self.agent, "name", "") != "claude":
            return
        df = self.journal.trades(self.run_id)
        if df.empty:
            return
        day_s = str(day)
        todays = df[df["open_time"].str.startswith(day_s) | df["close_time"].fillna("").str.startswith(day_s)]
        if todays.empty:
            return
        cols = ["ticket", "symbol", "direction", "setup", "entry", "stop_loss", "take_profit",
                "exit_price", "r_multiple", "close_reason", "reasoning", "close_reasoning"]
        trades_text = todays[cols].to_csv(index=False)
        label = "previous trading day" if self.hide_dates else day_s
        res = self.agent.run_cycle(
            review_prompt(label, trades_text, self.executor.t_get_account_status()), allow_trading=False)
        self.journal.log_event(self.run_id, self.broker.now(), "review", res.text,
                               {"rounds": res.rounds, "usage": res.usage})

    def _on_new_bar(self) -> None:
        self.sync_closed()
        today = self.broker.now().date()
        if self._last_day is not None and today != self._last_day:
            a = self.broker.account()
            self.journal.record_equity(self.run_id, self.broker.now(), a.balance, a.equity)
            self.daily_review(self._last_day)
        self._last_day = today
        self.risk.roll_day()

    # ---- backtest ---------------------------------------------------------------------------
    def run_backtest(self) -> dict:
        b = self.broker
        max_cycles = self.cfg.backtest.max_cycles
        a = b.account()
        self.journal.record_equity(self.run_id, b.now(), a.balance, a.equity)
        while True:
            self._on_new_bar()
            if decision_due(b.now(), self.cfg.decision_timeframe) and any(
                    b.has_bar_now(s) for s in self.cfg.watchlist):
                self.decision_cycle()
                if max_cycles and self.cycles >= max_cycles:
                    break
            if not b.step():
                break
        b.close_all("end_of_test")
        self.sync_closed()
        a = b.account()
        self.journal.record_equity(self.run_id, b.now(), a.balance, a.equity)
        return self.report()

    # ---- live -------------------------------------------------------------------------------
    def run_live(self, poll_seconds: int = 15) -> None:  # pragma: no cover - needs MT5
        ref = self.cfg.watchlist[0]
        last_bar = None
        log.info("Live session started (run %d). Create a file named STOP to stop.", self.run_id)
        try:
            while not STOP_FILE.exists():
                self._on_new_bar()
                bars = self.broker.get_bars(ref, self.cfg.decision_timeframe, 1)
                bar_time = bars.index[-1] if len(bars) else None
                if bar_time is not None and bar_time != last_bar:
                    if last_bar is not None:  # skip the bar that was already closed at start-up
                        self.decision_cycle()
                        a = self.broker.account()
                        self.journal.record_equity(self.run_id, self.broker.now(), a.balance, a.equity)
                    last_bar = bar_time
                time.sleep(poll_seconds)
        finally:
            self.sync_closed()
            self.broker.shutdown()
            print(self.report_text())

    # ---- reporting --------------------------------------------------------------------------
    def report(self) -> dict:
        trades = self.journal.trades(self.run_id)
        eq = self.journal.equity_curve(self.run_id)
        init = eq["balance"].iloc[0] if not eq.empty else None
        return performance(trades, eq, init)

    def report_text(self) -> str:
        return format_report(f"run {self.run_id} ({self.mode})", self.report(),
                             self.journal.usage_totals(self.run_id))
