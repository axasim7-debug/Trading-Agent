"""MCP server: exposes the trading desk (market data, calendar, risk-checked orders, journal) as tools.

This is how the agent runs on a Claude subscription with no API key: Claude Code (or Claude Desktop)
is the brain, this server is its eyes and hands. Claude Code starts it automatically from `.mcp.json`.

    python -m trading_agent.mcp_server                 # MT5 demo account (Windows)
    python -m trading_agent.mcp_server --broker sim    # frozen simulator snapshot, for trying the tools

Every order still goes through the RiskManager: the model cannot bypass the hard limits.
"""
from __future__ import annotations

import argparse
import json
import os
import threading
from typing import Annotated

from pydantic import Field

from .agent.tools import ToolExecutor
from .config import load_config
from .econ_calendar import EconomicCalendar
from .journal import Journal
from .risk import RiskManager

Sym = Annotated[str, Field(description="Watchlist symbol, e.g. EURUSD")]


def build_desk(config_path: str, broker_kind: str) -> ToolExecutor:
    cfg = load_config(config_path)
    if broker_kind == "mt5":
        from .broker.mt5 import MT5Broker
        broker = MT5Broker(cfg)
        label = f"mt5-{cfg.mt5.login or 'terminal'}"
    else:
        from .broker import SimBroker
        from .data import load_symbols
        bt = cfg.backtest
        data = load_symbols(bt.data_dir, cfg.watchlist, bt.base_timeframe)
        broker = SimBroker(data, {s: cfg.spec(s) for s in data}, bt.base_timeframe, bt.initial_balance,
                           cfg.commission_per_lot, start=bt.start or bt.end)
        label = "sim-snapshot"
    journal = Journal(cfg.db_path)
    run_id = journal.get_or_create_run("live" if broker_kind == "mt5" else "sim", "claude-code", label)
    calendar = EconomicCalendar(cache_path=os.path.join("cache", "calendar_thisweek.json"))
    risk = RiskManager(cfg.risk, broker, cfg.commission_per_lot, journal=journal, run_id=run_id,
                       calendar=calendar if broker_kind == "mt5" else None)
    return ToolExecutor(broker, risk, journal, run_id, cfg.watchlist, hide_dates=False, calendar=calendar,
                        default_management=cfg.watchdog.default_management,
                        stats_path=cfg.research.stats_path)


def create_server(desk: ToolExecutor):
    from mcp.server.mcpserver import MCPServer

    srv = MCPServer(
        "trading",
        instructions="Trading desk for a MetaTrader 5 DEMO account. Orders are sized and checked by a hard "
                     "risk manager. Read CLAUDE.md for the trading constitution and routines.",
    )

    lock = threading.Lock()  # tools run in worker threads; MT5 and SQLite need serial access

    def call(name: str, **args) -> str:
        with lock:
            try:
                # Keep the journal in sync with stops/targets hit since the last call.
                for ticket in desk.journal.open_tickets(desk.run_id):
                    ct = desk.broker.closed_trade(ticket)
                    if ct is not None:
                        desk.journal.record_close(desk.run_id, ct)
                args = {k: v for k, v in args.items() if v is not None}
                text, is_err = desk.execute(name, args)
            except Exception as e:  # never crash the server; tell the model what went wrong
                return f"ERROR: {type(e).__name__}: {e}"
        return f"ERROR: {text}" if is_err else text

    @srv.tool()
    def get_account_status() -> str:
        """Balance/equity, today's P/L vs target and loss limit, trades today, open risk, currency
        exposure, and whether new trades are blocked."""
        return call("get_account_status")

    @srv.tool()
    def get_session_info() -> str:
        """Broker server time, UTC time, open FX sessions, weekend flag."""
        return call("get_session_info")

    @srv.tool()
    def get_market_overview(symbols: list[str] | None = None) -> str:
        """One line per symbol: price, spread, D1/H4/H1 trend, H1 RSI/ADX/ATR, D1 ATR, 24h change."""
        return call("get_market_overview", symbols=symbols)

    @srv.tool()
    def get_candles(symbol: Sym,
                    timeframe: Annotated[str, Field(description="M15, H1, H4 or D1")],
                    count: Annotated[int, Field(ge=10, le=300)] = 100,
                    include_indicators: bool = True) -> str:
        """Completed OHLC bars as CSV (forming bar excluded), with EMA20/50/200, RSI14, ATR14, ADX14,
        MACD histogram and Bollinger bands."""
        return call("get_candles", symbol=symbol, timeframe=timeframe, count=count,
                    include_indicators=include_indicators)

    @srv.tool()
    def get_economic_calendar(currencies: list[str] | None = None,
                              hours_ahead: Annotated[float, Field(ge=1, le=168)] = 24,
                              min_impact: Annotated[str, Field(description="Low, Medium or High")] = "Medium") -> str:
        """Scheduled macro releases with impact, forecast and previous values (this week)."""
        return call("get_economic_calendar", currencies=currencies, hours_ahead=hours_ahead,
                    min_impact=min_impact)

    @srv.tool()
    def get_open_positions() -> str:
        """Open positions: entry, stop, target, volume, pips, floating P/L, money still at risk."""
        return call("get_open_positions")

    @srv.tool()
    def open_trade(symbol: Sym,
                   direction: Annotated[str, Field(description="buy or sell")],
                   stop_loss: Annotated[float, Field(description="Absolute price of the invalidation level")],
                   risk_percent: Annotated[float, Field(description="Percent of equity lost if the stop is hit")],
                   setup: Annotated[str, Field(description="Playbook setup tag")],
                   reasoning: Annotated[str, Field(description="Thesis, confluence, catalyst, invalidation, target logic")],
                   take_profit: Annotated[float | None, Field(description="Absolute price (optional)")] = None) -> str:
        """Open a market order. Size is computed from risk_percent and the stop distance. The risk manager
        may reject it (limits, exposure, news blackout); a rejection is final for that idea."""
        return call("open_trade", symbol=symbol, direction=direction, stop_loss=stop_loss,
                    risk_percent=risk_percent, setup=setup, reasoning=reasoning, take_profit=take_profit)

    @srv.tool()
    def modify_trade(ticket: int, reasoning: str, stop_loss: float | None = None,
                     take_profit: float | None = None) -> str:
        """Tighten the stop (never widen) and/or move the target of an open position."""
        return call("modify_trade", ticket=ticket, reasoning=reasoning, stop_loss=stop_loss,
                    take_profit=take_profit)

    @srv.tool()
    def close_trade(ticket: int, reasoning: str) -> str:
        """Close an open position at market."""
        return call("close_trade", ticket=ticket, reasoning=reasoning)

    @srv.tool()
    def get_trade_history(limit: Annotated[int, Field(ge=1, le=100)] = 20) -> str:
        """Recent closed trades with original reasoning and outcome in R, plus stats per setup tag."""
        return call("get_trade_history", limit=limit)

    @srv.tool()
    def save_note(note: Annotated[str, Field(max_length=600)]) -> str:
        """Save a durable, evidence-based lesson to persistent memory."""
        return call("save_note", note=note)

    @srv.tool()
    def get_notes(limit: Annotated[int, Field(ge=1, le=50)] = 15) -> str:
        """Your saved lessons, most recent last."""
        with lock:
            return json.dumps(desk.journal.notes(desk.run_id, limit), ensure_ascii=False)

    @srv.tool()
    def save_plan(scope: Annotated[str, Field(description="daily or weekly")],
                  plan: Annotated[str, Field(max_length=8000)]) -> str:
        """Save the daily or weekly trading plan."""
        return call("save_plan", scope=scope, plan=plan)

    @srv.tool()
    def get_plan() -> str:
        """Current daily and weekly plans."""
        return call("get_plan")

    @srv.tool()
    def log_cycle(kind: Annotated[str, Field(description="cycle, daily_prep, review or weekly_review")],
                  summary: Annotated[str, Field(max_length=4000)]) -> str:
        """Record the outcome of this routine (call once at the end of every routine)."""
        return call("log_cycle", kind=kind, summary=summary)

    @srv.tool()
    def get_recent_cycles(limit: Annotated[int, Field(ge=1, le=30)] = 6) -> str:
        """Summaries from recent routines, for continuity between sessions."""
        return call("get_recent_cycles", limit=limit)

    @srv.tool()
    def scan_setups(symbols: list[str] | None = None) -> str:
        """Run the coded playbook scanners on the last completed H1 bar. Each signal comes with its
        historical edge card (win rate, average R, out-of-sample result, validated flag)."""
        return call("scan_setups", symbols=symbols)

    @srv.tool()
    def get_edge_stats(setup: str | None = None, symbol: str | None = None) -> str:
        """Historical statistics of the playbook setups (from `python -m trading_agent research`)."""
        return call("get_edge_stats", setup=setup, symbol=symbol)

    @srv.tool()
    def set_alert(symbol: Sym,
                  condition: Annotated[str, Field(description="above or below")],
                  price: float,
                  note: Annotated[str, Field(description="What you will check when it triggers")],
                  expires_hours: Annotated[float, Field(ge=0.5, le=168)] = 24) -> str:
        """Price alert watched by the always-on watchdog. When it triggers, you are woken up
        immediately with this note, instead of waiting for the next hourly cycle."""
        return call("set_alert", symbol=symbol, condition=condition, price=price, note=note,
                    expires_hours=expires_hours)

    @srv.tool()
    def list_alerts() -> str:
        """Active and triggered (not yet dismissed) alerts."""
        return call("list_alerts")

    @srv.tool()
    def dismiss_alert(alert_id: int) -> str:
        """Dismiss an alert once handled (or cancel an active one)."""
        return call("dismiss_alert", alert_id=alert_id)

    @srv.tool()
    def set_trade_management(ticket: int,
                             break_even_at_r: Annotated[float | None, Field(description="Move stop to entry at this R (null = never)")] = None,
                             partial_at_r: Annotated[float | None, Field(description="Close part of the position at this R")] = None,
                             partial_fraction: Annotated[float | None, Field(description="Fraction closed at partial_at_r, e.g. 0.5")] = None,
                             trail_atr_mult: Annotated[float | None, Field(description="Trail the stop at close - k*ATR")] = None,
                             trail_timeframe: Annotated[str | None, Field(description="M15, H1, H4 or D1")] = None,
                             trail_after_r: Annotated[float | None, Field(description="Start trailing after this R")] = None,
                             time_stop_hours: Annotated[float | None, Field(description="Close after this many hours")] = None) -> str:
        """Set how the watchdog manages an open position between your cycles (only the given fields change).
        Stops are only ever tightened."""
        return call("set_trade_management", ticket=ticket, break_even_at_r=break_even_at_r,
                    partial_at_r=partial_at_r, partial_fraction=partial_fraction, trail_atr_mult=trail_atr_mult,
                    trail_timeframe=trail_timeframe, trail_after_r=trail_after_r, time_stop_hours=time_stop_hours)

    return srv


def main(argv=None):
    ap = argparse.ArgumentParser(description="Trading desk MCP server")
    ap.add_argument("--config", default=os.environ.get("TRADING_CONFIG", "config.yaml"))
    ap.add_argument("--broker", choices=["mt5", "sim"], default=os.environ.get("TRADING_BROKER", "mt5"))
    args = ap.parse_args(argv)
    # Resolve relative paths (journal, data, cache) against the project folder, whoever launched us.
    cfg_path = os.path.abspath(args.config)
    os.chdir(os.path.dirname(cfg_path))
    desk = build_desk(cfg_path, args.broker)
    try:
        create_server(desk).run("stdio")
    finally:
        desk.broker.shutdown()


if __name__ == "__main__":
    main()
