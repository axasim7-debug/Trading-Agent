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
from .diagnostics import Sync
from .notifier import Notifier
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
    notifier = Notifier(enabled=cfg.context.notifications and broker_kind == "mt5")
    return ToolExecutor(broker, risk, journal, run_id, cfg.watchlist, hide_dates=False, calendar=calendar,
                        default_management=cfg.watchdog.default_management,
                        stats_path=cfg.research.stats_path, notifier=notifier,
                        intermarket_symbols=cfg.context.intermarket_symbols,
                        oanda_practice=cfg.context.oanda_practice, cache_dir=cfg.context.cache_dir)


def create_server(desk: ToolExecutor):
    from mcp.server.mcpserver import Image, MCPServer

    srv = MCPServer(
        "trading",
        instructions="Trading desk for a MetaTrader 5 DEMO account. Orders are sized and checked by a hard "
                     "risk manager. Read CLAUDE.md for the trading constitution and routines.",
    )

    lock = threading.Lock()  # tools run in worker threads; MT5 and SQLite need serial access
    sync = Sync(desk.broker, desk.journal, desk.run_id, desk.notifier, desk.default_management)

    def call(name: str, **args) -> str:
        with lock:
            try:
                # Keep the journal in sync: pending fills, stops/targets hit since the last call.
                sync.run()
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
                   win_probability: Annotated[float, Field(ge=0.05, le=0.95, description="Your honest probability that this trade ends in profit (calibration is tracked)")],
                   take_profit: Annotated[float | None, Field(description="Absolute price (optional)")] = None) -> str:
        """Open a market order. Size is computed from risk_percent and the stop distance. The risk manager
        may reject it (limits, exposure, news blackout, drawdown); a rejection is final for that idea."""
        return call("open_trade", symbol=symbol, direction=direction, stop_loss=stop_loss,
                    risk_percent=risk_percent, setup=setup, reasoning=reasoning, take_profit=take_profit,
                    win_probability=win_probability)

    @srv.tool()
    def place_pending_order(symbol: Sym,
                            direction: Annotated[str, Field(description="buy or sell")],
                            entry_type: Annotated[str, Field(description="limit (buy below / sell above price) or stop (buy above / sell below price)")],
                            entry_price: float,
                            stop_loss: float,
                            risk_percent: float,
                            setup: str,
                            reasoning: str,
                            win_probability: Annotated[float, Field(ge=0.05, le=0.95)],
                            take_profit: float | None = None,
                            expiry_hours: Annotated[float, Field(ge=0.5, le=120)] = 24) -> str:
        """Place a limit or stop entry at your level instead of chasing price. Sized now from the stop
        distance; counts toward open risk and exposure while pending. Expires automatically."""
        return call("place_pending_order", symbol=symbol, direction=direction, entry_type=entry_type,
                    entry_price=entry_price, stop_loss=stop_loss, risk_percent=risk_percent, setup=setup,
                    reasoning=reasoning, take_profit=take_profit, expiry_hours=expiry_hours,
                    win_probability=win_probability)

    @srv.tool()
    def list_pending_orders() -> str:
        """Pending limit/stop orders that have not filled yet."""
        return call("list_pending_orders")

    @srv.tool()
    def cancel_pending_order(ticket: int, reasoning: str) -> str:
        """Cancel a pending order (e.g. the setup was invalidated before fill)."""
        return call("cancel_pending_order", ticket=ticket, reasoning=reasoning)

    @srv.tool()
    def get_market_structure(symbol: Sym,
                             timeframe: Annotated[str, Field(description="M15, H1, H4 or D1")] = "H4") -> str:
        """Swing structure (HH/HL), support/resistance zones with touch counts and distance in ATR,
        previous day/week high-low, Asia range, ADR and % of ADR used today."""
        return call("get_market_structure", symbol=symbol, timeframe=timeframe)

    @srv.tool()
    def view_chart(symbol: Sym,
                   timeframe: Annotated[str, Field(description="M15, H1, H4 or D1")] = "H1",
                   count: Annotated[int, Field(ge=30, le=300)] = 120,
                   show_levels: bool = True, bollinger: bool = False):
        """Look at the chart as an image: candles, EMA20/50/200, RSI, key structure levels, your
        positions (entry/SL/TP), pending orders and alerts."""
        with lock:
            try:
                png = desk.chart_png(symbol, timeframe, count, show_levels, bollinger)
            except Exception as e:
                return f"ERROR: {type(e).__name__}: {e}"
        return Image(data=png, format="png")

    @srv.tool()
    def get_intermarket() -> str:
        """Related markets and macro: US 2y/10y yields and curve, broad dollar, VIX, oil, S&P 500 (FRED,
        daily), plus live prices of related instruments on your broker if configured."""
        return call("get_intermarket")

    @srv.tool()
    def get_positioning(symbol: Sym) -> str:
        """Who is positioned where: CFTC COT (leveraged funds / managed money) for both currencies,
        and OANDA retail long/short % (contrarian when extreme)."""
        return call("get_positioning", symbol=symbol)

    @srv.tool()
    def get_performance_diagnostics() -> str:
        """Is your judgement adding value? Calibration of your win probabilities (Brier score),
        signals taken vs skipped (shadow book), MAE/MFE of trades, drawdown and risk multiplier."""
        return call("get_performance_diagnostics")

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
