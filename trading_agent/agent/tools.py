"""Tools the agent can call, and their executor. Every order goes through the RiskManager."""
from __future__ import annotations

import json
from typing import Any

import pandas as pd

from datetime import timedelta

from .. import management as mg
from ..broker.base import Broker
from ..journal import Journal
from ..market import candles_text, fmt_time, market_overview
from ..models import TIMEFRAMES, Direction
from ..playbook import scan_now
from ..research import edge_card, load_stats
from ..risk import RiskManager

TF_ENUM = ["M15", "H1", "H4", "D1"]

TOOL_SCHEMAS: list[dict] = [
    {
        "name": "get_account_status",
        "description": "Account balance/equity, today's P/L versus the daily target and loss limit, "
                       "trades taken today, open risk, and whether new trades are currently blocked.",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "get_market_overview",
        "description": "One-line snapshot per symbol: price, spread, D1/H4/H1 trend labels, H1 RSI/ADX/ATR, "
                       "D1 ATR and 24h change. Use it to decide which charts deserve a closer look.",
        "input_schema": {
            "type": "object",
            "properties": {"symbols": {"type": "array", "items": {"type": "string"},
                                       "description": "Subset of the watchlist; omit for all."}},
            "additionalProperties": False,
        },
    },
    {
        "name": "get_candles",
        "description": "Completed OHLC bars for one symbol and timeframe as CSV, optionally with "
                       "EMA20/50/200, RSI14, ATR14, ADX14, MACD histogram and Bollinger bands. "
                       "The bar that is still forming is never included.",
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "timeframe": {"type": "string", "enum": TF_ENUM},
                "count": {"type": "integer", "minimum": 10, "maximum": 300},
                "include_indicators": {"type": "boolean"},
            },
            "required": ["symbol", "timeframe", "count"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_open_positions",
        "description": "Open positions with entry, stop, target, volume, floating P/L and money still at risk.",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "open_trade",
        "description": "Open a market order. Position size is computed for you from risk_percent and the "
                       "stop distance. The risk manager may reject the order; rejections are final for "
                       "that request - read the reason before trying anything else.",
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "direction": {"type": "string", "enum": ["buy", "sell"]},
                "stop_loss": {"type": "number", "description": "Absolute price level."},
                "take_profit": {"type": "number", "description": "Absolute price level (optional)."},
                "risk_percent": {"type": "number", "description": "Percent of equity to risk if the stop is hit."},
                "setup": {"type": "string", "description": "Short tag for the setup type, e.g. 'trend-pullback-H1'."},
                "reasoning": {"type": "string", "description": "Thesis, confluence, and what would invalidate it."},
                "win_probability": {"type": "number", "minimum": 0.05, "maximum": 0.95,
                                    "description": "Your honest probability that the trade ends in profit."},
            },
            "required": ["symbol", "direction", "stop_loss", "risk_percent", "setup", "reasoning"],
            "additionalProperties": False,
        },
    },
    {
        "name": "modify_trade",
        "description": "Move the stop (tighten only) and/or the target of an open position.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticket": {"type": "integer"},
                "stop_loss": {"type": "number"},
                "take_profit": {"type": "number"},
                "reasoning": {"type": "string"},
            },
            "required": ["ticket", "reasoning"],
            "additionalProperties": False,
        },
    },
    {
        "name": "close_trade",
        "description": "Close an open position at market.",
        "input_schema": {
            "type": "object",
            "properties": {"ticket": {"type": "integer"}, "reasoning": {"type": "string"}},
            "required": ["ticket", "reasoning"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_trade_history",
        "description": "Your recent closed trades (with your original reasoning and the outcome in R) "
                       "plus aggregate stats per setup tag.",
        "input_schema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 100}},
            "additionalProperties": False,
        },
    },
    {
        "name": "save_note",
        "description": "Save a short lesson or observation to your persistent memory. Recent notes are shown "
                       "to you at the start of every cycle. Save only durable, evidence-based lessons.",
        "input_schema": {
            "type": "object",
            "properties": {"note": {"type": "string", "maxLength": 600}},
            "required": ["note"],
            "additionalProperties": False,
        },
    },
]

# Extra tools for the live desk (Claude Code / MCP). Not offered in backtests:
# there is no historical calendar, and plans/summaries are part of the live routine.
LIVE_TOOL_SCHEMAS: list[dict] = [
    {
        "name": "get_economic_calendar",
        "description": "Scheduled macro releases (central bank decisions, NFP, CPI, PMIs...) with impact, "
                       "forecast and previous values. Times in broker server time and UTC.",
        "input_schema": {
            "type": "object",
            "properties": {
                "currencies": {"type": "array", "items": {"type": "string"},
                               "description": "e.g. ['USD','EUR']; omit for all currencies in the watchlist."},
                "hours_ahead": {"type": "number", "minimum": 1, "maximum": 168},
                "min_impact": {"type": "string", "enum": ["Low", "Medium", "High"]},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "get_session_info",
        "description": "Broker server time, UTC time, which FX sessions are open, and today's date.",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "save_plan",
        "description": "Save the daily or weekly trading plan (bias per market, key levels, scenarios, events "
                       "to avoid). Intraday cycles read it back with get_plan.",
        "input_schema": {
            "type": "object",
            "properties": {"scope": {"type": "string", "enum": ["daily", "weekly"]},
                           "plan": {"type": "string", "maxLength": 8000}},
            "required": ["scope", "plan"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_plan",
        "description": "Read the current daily and weekly trading plans.",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "log_cycle",
        "description": "Record the outcome of this routine in the journal (call once at the end of every "
                       "routine). Recent summaries are shown to later cycles for continuity.",
        "input_schema": {
            "type": "object",
            "properties": {"kind": {"type": "string", "enum": ["cycle", "daily_prep", "review", "weekly_review"]},
                           "summary": {"type": "string", "maxLength": 4000}},
            "required": ["kind", "summary"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_recent_cycles",
        "description": "Summaries written at the end of recent routines (what was seen, done, and watched).",
        "input_schema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 30}},
            "additionalProperties": False,
        },
    },
]

TRADING_TOOLS = {"open_trade", "modify_trade", "close_trade"}

SESSIONS_UTC = {"Sydney": (21, 6), "Tokyo": (0, 9), "London": (7, 16), "New York": (12, 21)}


class ToolError(Exception):
    pass


class ToolExecutor:
    def __init__(self, broker: Broker, risk: RiskManager, journal: Journal, run_id: int,
                 watchlist: list[str], hide_dates: bool, calendar=None,
                 default_management: dict | None = None, stats_path: str | None = None,
                 notifier=None, intermarket_symbols: dict | None = None, oanda_practice: bool = True,
                 cache_dir: str = "cache"):
        self.notifier = notifier
        self.intermarket_symbols = intermarket_symbols or {}
        self.oanda_practice = oanda_practice
        self.cache_dir = cache_dir
        self.calendar = calendar
        self.default_management = default_management
        self.stats_path = stats_path
        self.broker = broker
        self.risk = risk
        self.journal = journal
        self.run_id = run_id
        self.watchlist = watchlist
        self.hide_dates = hide_dates

    # ---- dispatch -------------------------------------------------------------------------
    def execute(self, name: str, args: dict[str, Any]) -> tuple[str, bool]:
        """Returns (result_text, is_error)."""
        fn = getattr(self, f"t_{name}", None)
        if fn is None:
            return f"Unknown tool: {name}", True
        try:
            result = fn(**(args or {}))
        except ToolError as e:
            return str(e), True
        except TypeError as e:
            return f"Invalid arguments for {name}: {e}", True
        except KeyError as e:
            return f"Unknown symbol or data missing: {e}", True
        if not isinstance(result, str):
            result = json.dumps(result, default=str, ensure_ascii=False)
        return result, False

    def _symbol(self, symbol: str) -> str:
        s = str(symbol).upper().strip()
        if s not in self.watchlist:
            raise ToolError(f"{symbol} is not in the watchlist {self.watchlist}")
        return s

    def _t(self, t):
        return fmt_time(t, self.hide_dates)

    # ---- read-only tools ------------------------------------------------------------------
    def t_get_account_status(self):
        a = self.broker.account()
        return {"time": self._t(self.broker.now()), "balance": round(a.balance, 2),
                "equity": round(a.equity, 2), "currency": a.currency, "is_demo": a.is_demo,
                "open_positions": len(self.broker.positions()), **self.risk.status()}

    def t_get_market_overview(self, symbols: list[str] | None = None):
        syms = [self._symbol(s) for s in symbols] if symbols else self.watchlist
        return market_overview(self.broker, syms, self.hide_dates)

    def t_get_candles(self, symbol, timeframe, count, include_indicators=True):
        if timeframe not in TF_ENUM:
            raise ToolError(f"timeframe must be one of {TF_ENUM}")
        count = max(10, min(int(count), 300))
        return candles_text(self.broker, self._symbol(symbol), timeframe, count,
                            bool(include_indicators), self.hide_dates)

    def t_get_open_positions(self):
        out = []
        for p in self.broker.positions():
            spec = self.broker.symbol_spec(p.symbol)
            bid, ask = self.broker.quote(p.symbol)
            cur = bid if p.direction is Direction.BUY else ask
            out.append({
                "ticket": p.ticket, "symbol": p.symbol, "direction": p.direction.value, "volume": p.volume,
                "entry": p.entry_price, "stop_loss": p.stop_loss, "take_profit": p.take_profit,
                "current_exit_price": cur,
                "pips": round((cur - p.entry_price) * p.direction.sign / spec.pip, 1),
                "floating_pl": round(p.profit, 2),
                "money_at_risk": round(self.risk.position_risk(p), 2),
                "opened": self._t(p.open_time),
            })
        return out or "No open positions."

    def t_get_trade_history(self, limit=20):
        df = self.journal.trades(self.run_id, closed_only=True)
        if df.empty:
            return "No closed trades yet."
        recent = df.tail(int(limit))
        rows = []
        for _, r in recent.iterrows():
            rows.append({
                "ticket": r.ticket, "symbol": r.symbol, "direction": r.direction, "setup": r.setup,
                "r_multiple": None if pd.isna(r.r_multiple) else round(r.r_multiple, 2),
                "profit": round(r.profit, 2), "close_reason": r.close_reason,
                "reasoning": (r.reasoning or "")[:400],
            })
        by_setup = (df.groupby("setup")["r_multiple"]
                    .agg(trades="count", avg_r="mean", win_rate=lambda s: (s > 0).mean())
                    .round(3).reset_index().to_dict("records"))
        return {"recent_trades": rows, "stats_by_setup": by_setup,
                "all_time": {"trades": len(df), "avg_r": round(df["r_multiple"].mean(), 3),
                             "win_rate": round((df["r_multiple"] > 0).mean(), 3),
                             "net_profit": round(df["profit"].sum(), 2)}}

    def t_save_note(self, note):
        self.journal.add_note(self.run_id, self.broker.now(), str(note)[:600])
        return "Saved."

    # ---- live desk tools -------------------------------------------------------------------
    def _now_utc(self):
        return self.broker.now() - self.broker.utc_offset()

    def t_get_economic_calendar(self, currencies=None, hours_ahead=24, min_impact="Medium"):
        if self.calendar is None:
            raise ToolError("Economic calendar is not available in this mode.")
        if currencies:
            ccys = {str(c).upper() for c in currencies}
        else:
            ccys = {c for s in self.watchlist for c in (s[:3], s[3:6])}
        try:
            evs = self.calendar.upcoming(self._now_utc(), ccys, float(hours_ahead), min_impact)
        except Exception as e:
            raise ToolError(f"Calendar feed unavailable: {e}. Use web search for the schedule.")
        off = self.broker.utc_offset()
        return {"now_server": self.broker.now().strftime("%a %Y-%m-%d %H:%M"),
                "events": [e.to_dict(off) for e in evs] or "No matching events."}

    def t_get_session_info(self):
        now_utc = self._now_utc()
        h = now_utc.hour + now_utc.minute / 60
        def is_open(a, b):
            return a <= h < b if a < b else (h >= a or h < b)
        open_ = [n for n, (a, b) in SESSIONS_UTC.items() if is_open(a, b)]
        weekend = now_utc.weekday() == 5 or (now_utc.weekday() == 6 and h < 21) or \
            (now_utc.weekday() == 4 and h >= 21)
        return {"server_time": self.broker.now().strftime("%a %Y-%m-%d %H:%M"),
                "utc_time": now_utc.strftime("%a %Y-%m-%d %H:%M"),
                "server_utc_offset_hours": self.broker.utc_offset().total_seconds() / 3600,
                "open_sessions": open_ or ["none (rollover/low liquidity)"],
                "fx_market_closed_for_weekend": weekend,
                "session_hours_utc": SESSIONS_UTC}

    def t_save_plan(self, scope, plan):
        if scope not in ("daily", "weekly"):
            raise ToolError("scope must be daily or weekly")
        now = self.broker.now()
        key = f"plan:daily:{now.date()}" if scope == "daily" else f"plan:weekly:{now.isocalendar()[1]}"
        self.journal.set_kv(self.run_id, key, {"saved": now.isoformat(timespec="minutes"), "plan": plan})
        return f"{scope} plan saved."

    def t_get_plan(self):
        now = self.broker.now()
        return {"daily": self.journal.get_kv(self.run_id, f"plan:daily:{now.date()}") or "No daily plan yet today.",
                "weekly": self.journal.get_kv(self.run_id, f"plan:weekly:{now.isocalendar()[1]}")
                or "No weekly plan yet this week."}

    def t_log_cycle(self, kind, summary):
        self.journal.log_event(self.run_id, self.broker.now(), kind, summary)
        if kind in ("daily_prep", "review", "weekly_review"):
            title = {"daily_prep": "🗓️ خطة اليوم", "review": "📊 مراجعة اليوم", "weekly_review": "📅 المراجعة الأسبوعية"}[kind]
            self._notify(f"{title}\n{summary[:3500]}")
        return "Logged."

    def t_get_recent_cycles(self, limit=6):
        return self.journal.events(self.run_id, ("cycle", "daily_prep", "review", "weekly_review"),
                                   int(limit)) or "No previous routines logged."

    # ---- watchdog: alerts and trade management --------------------------------------------
    def t_set_alert(self, symbol, condition, price, note, expires_hours=24):
        symbol = self._symbol(symbol)
        if condition not in ("above", "below"):
            raise ToolError("condition must be 'above' or 'below'")
        bid, _ = self.broker.quote(symbol)
        price = float(price)
        if (condition == "above" and bid >= price) or (condition == "below" and bid <= price):
            raise ToolError(f"Alert would trigger immediately (bid {bid}).")
        now = self.broker.now()
        aid = self.journal.add_alert(self.run_id, symbol, condition, price, str(note)[:300], now,
                                     now + timedelta(hours=float(expires_hours)))
        return {"alert_id": aid, "symbol": symbol, "condition": condition, "price": price,
                "note": "The watchdog will wake you when it triggers."}

    def t_list_alerts(self):
        rows = [a for a in self.journal.alerts(self.run_id) if a["status"] in ("active", "triggered")]
        return [{k: a[k] for k in ("id", "symbol", "condition", "price", "note", "status", "expires",
                                   "triggered_at", "trigger_price")} for a in rows] or "No active or triggered alerts."

    def t_dismiss_alert(self, alert_id):
        self.journal.set_alert_status(int(alert_id), "dismissed")
        return "Dismissed."

    def t_set_trade_management(self, ticket, **params):
        p = self.broker.position(int(ticket))
        if p is None:
            raise ToolError(f"No open position with ticket {ticket}")
        key = mg.plan_key(p.ticket)
        raw = self.journal.get_kv(self.run_id, key)
        plan = mg.ManagementPlan.from_dict(raw) if raw else mg.default_plan(p, self.default_management)
        allowed = {"break_even_at_r", "partial_at_r", "partial_fraction", "trail_atr_mult",
                   "trail_timeframe", "trail_after_r", "time_stop_hours"}
        for k, v in params.items():
            if k not in allowed:
                raise ToolError(f"Unknown management field {k}")
            setattr(plan, k, v)
        if plan.partial_fraction is not None and not (0 < plan.partial_fraction < 1):
            raise ToolError("partial_fraction must be between 0 and 1")
        if plan.trail_timeframe not in TF_ENUM:
            raise ToolError(f"trail_timeframe must be one of {TF_ENUM}")
        self.journal.set_kv(self.run_id, key, plan.to_dict())
        return {"ticket": p.ticket, "plan": plan.to_dict()}

    # ---- playbook research -----------------------------------------------------------------
    def t_scan_setups(self, symbols=None):
        syms = [self._symbol(s) for s in symbols] if symbols else self.watchlist
        stats = load_stats(self.stats_path) if self.stats_path else None
        found = scan_now(self.broker, syms)
        for s in found:
            s["edge"] = edge_card(stats, s["setup"], s["symbol"], s["session"], s["regime"])
            validated = any(isinstance(v, dict) and v.get("validated") for v in s["edge"].values())
            self.journal.record_signal(self.run_id, {**s, "dir": 1 if s["direction"] == "buy" else -1}, validated)
            s["bar_time"] = self._t(s["bar_time"])
        return found or "No playbook setup fired on the last completed H1 bar."

    def t_get_edge_stats(self, setup=None, symbol=None):
        stats = load_stats(self.stats_path) if self.stats_path else None
        if not stats:
            return "No research yet. Run `python -m trading_agent research` on historical data."
        rows = [r for r in stats["table"] if (not setup or r.get("setup") == setup)
                and (not symbol or r.get("symbol") in (None, str(symbol).upper()))]
        return {"data": f"{stats['data_from'][:10]} -> {stats['data_to'][:10]} (OOS from {stats['oos_from'][:10]})",
                "rules": stats["rules"], "rows": rows[:60]}

    # ---- trading tools --------------------------------------------------------------------
    def _notify(self, text: str) -> None:
        if self.notifier is not None:
            self.notifier.send(text)

    def t_open_trade(self, symbol, direction, stop_loss, risk_percent, setup, reasoning, take_profit=None,
                     win_probability=None):
        symbol = self._symbol(symbol)
        try:
            d = Direction(str(direction).lower())
        except ValueError:
            raise ToolError("direction must be 'buy' or 'sell'")
        plan = self.risk.plan_order(symbol, d, float(stop_loss),
                                    None if take_profit is None else float(take_profit), float(risk_percent))
        now = self.broker.now()
        if not plan.ok:
            self.journal.log_event(self.run_id, now, "rejected", "; ".join(plan.reasons),
                                   {"symbol": symbol, "direction": d.value, "stop_loss": stop_loss,
                                    "take_profit": take_profit, "risk_percent": risk_percent})
            raise ToolError("REJECTED by risk manager: " + "; ".join(plan.reasons))
        res = self.broker.open_position(symbol, d, plan.volume, float(stop_loss),
                                        None if take_profit is None else float(take_profit),
                                        comment=str(setup)[:31])
        if not res.ok:
            raise ToolError(f"Broker rejected the order: {res.message}")
        self.risk.register_open()
        wp = None if win_probability is None else max(0.05, min(0.95, float(win_probability)))
        self.journal.record_open(self.run_id, res.ticket, symbol, d.value, res.volume or plan.volume,
                                 res.price or plan.entry_estimate, float(stop_loss), take_profit,
                                 plan.risk_amount, now, setup, reasoning, wp)
        self.journal.mark_signal_taken(self.run_id, symbol, setup, d.sign, res.ticket)
        self._notify(f"📈 صفقة جديدة #{res.ticket}: {d.value} {symbol} @ {res.price} | وقف {stop_loss} | "
                     f"هدف {take_profit} | مخاطرة {plan.risk_amount:.2f} | {setup}\n{str(reasoning)[:600]}")
        pos = self.broker.position(res.ticket)
        if pos is not None:
            mplan = mg.default_plan(pos, self.default_management)
            self.journal.set_kv(self.run_id, mg.plan_key(res.ticket), mplan.to_dict())
        spec = self.broker.symbol_spec(symbol)
        return {"status": "filled", "ticket": res.ticket, "price": res.price, "volume": res.volume,
                "money_at_risk": round(plan.risk_amount, 2),
                "stop_pips": round(plan.stop_distance / spec.pip, 1),
                "reward_risk": None if plan.reward_risk is None else round(plan.reward_risk, 2)}

    def t_place_pending_order(self, symbol, direction, entry_type, entry_price, stop_loss, risk_percent,
                              setup, reasoning, take_profit=None, expiry_hours=24, win_probability=None):
        symbol = self._symbol(symbol)
        try:
            d = Direction(str(direction).lower())
        except ValueError:
            raise ToolError("direction must be 'buy' or 'sell'")
        if entry_type not in ("limit", "stop"):
            raise ToolError("entry_type must be 'limit' or 'stop'")
        tp = None if take_profit is None else float(take_profit)
        plan = self.risk.plan_order(symbol, d, float(stop_loss), tp, float(risk_percent),
                                    order_type=entry_type, entry_price=float(entry_price))
        now = self.broker.now()
        if not plan.ok:
            self.journal.log_event(self.run_id, now, "rejected", "; ".join(plan.reasons),
                                   {"symbol": symbol, "pending": entry_type, "price": entry_price})
            raise ToolError("REJECTED by risk manager: " + "; ".join(plan.reasons))
        expires = now + timedelta(hours=float(expiry_hours)) if expiry_hours else None
        res = self.broker.place_pending(symbol, d, entry_type, float(entry_price), plan.volume, float(stop_loss),
                                        tp, expires, comment=str(setup)[:31])
        if not res.ok:
            raise ToolError(f"Broker rejected the order: {res.message}")
        wp = None if win_probability is None else max(0.05, min(0.95, float(win_probability)))
        self.journal.record_order(self.run_id, res.ticket, symbol, d.value, entry_type, float(entry_price),
                                  plan.volume, float(stop_loss), tp, plan.risk_amount, now, expires, setup,
                                  reasoning, wp)
        self.risk.register_open()
        self._notify(f"⏳ أمر معلّق #{res.ticket}: {d.value} {entry_type} {symbol} @ {entry_price} | "
                     f"وقف {stop_loss} | هدف {tp} | {setup}")
        return {"status": "pending", "ticket": res.ticket, "volume": plan.volume,
                "money_at_risk_if_filled": round(plan.risk_amount, 2),
                "reward_risk": None if plan.reward_risk is None else round(plan.reward_risk, 2),
                "expires": self._t(expires) if expires else "GTC"}

    def t_list_pending_orders(self):
        return [o.to_dict() for o in self.broker.pending_orders()] or "No pending orders."

    def t_cancel_pending_order(self, ticket, reasoning):
        res = self.broker.cancel_pending(int(ticket))
        if not res.ok:
            raise ToolError(f"Could not cancel: {res.message}")
        self.journal.set_order_status(self.run_id, int(ticket), "cancelled")
        self.journal.log_event(self.run_id, self.broker.now(), "cancel", reasoning, {"ticket": int(ticket)})
        return "Cancelled."

    # ---- analysis ---------------------------------------------------------------------------
    def t_get_market_structure(self, symbol, timeframe="H4"):
        from ..structure import market_structure
        if timeframe not in TF_ENUM:
            raise ToolError(f"timeframe must be one of {TF_ENUM}")
        ms = market_structure(self.broker, self._symbol(symbol), timeframe, utc_offset=self.broker.utc_offset())
        ms.pop("all_zones_by_strength", None)
        return ms

    def chart_png(self, symbol, timeframe="H1", count=120, show_levels=True, bollinger=False) -> bytes:
        from ..charts import render_chart
        from ..structure import key_levels, market_structure
        symbol = self._symbol(symbol)
        levels = key_levels(market_structure(self.broker, symbol, "H4")) if show_levels else None
        return render_chart(self.broker, symbol, timeframe, int(count), levels=levels,
                            positions=[p for p in self.broker.positions() if p.symbol == symbol],
                            orders=[o for o in self.broker.pending_orders() if o.symbol == symbol],
                            alerts=[a for a in self.journal.alerts(self.run_id, "active") if a["symbol"] == symbol],
                            bollinger=bollinger)

    def t_get_intermarket(self):
        from ..intermarket import broker_intermarket, macro_snapshot
        return {"broker_instruments": broker_intermarket(self.broker, self.intermarket_symbols)
                or "none configured (config: intermarket_symbols)",
                "macro_fred": macro_snapshot(self.cache_dir)}

    def t_get_positioning(self, symbol):
        from ..intermarket import positioning
        return positioning(self._symbol(symbol), self.cache_dir, self.oanda_practice)

    def t_get_performance_diagnostics(self):
        from ..diagnostics import calibration_report, discretion_report, excursion_report
        trades = self.journal.trades(self.run_id, closed_only=True)
        return {"calibration": calibration_report(trades),
                "discretion_vs_shadow_book": discretion_report(self.journal.signals(self.run_id), trades),
                "excursions": excursion_report(trades),
                "risk_state": self.risk.period_state()}

    def t_modify_trade(self, ticket, reasoning, stop_loss=None, take_profit=None):
        p = self.broker.position(int(ticket))
        if p is None:
            raise ToolError(f"No open position with ticket {ticket}")
        sl = None if stop_loss is None else float(stop_loss)
        tp = None if take_profit is None else float(take_profit)
        if sl is None and tp is None:
            raise ToolError("Give stop_loss and/or take_profit")
        problems = self.risk.check_modify(p, sl, tp)
        if problems:
            raise ToolError("REJECTED by risk manager: " + "; ".join(problems))
        res = self.broker.modify_position(p.ticket, sl, tp)
        if not res.ok:
            raise ToolError(f"Broker rejected the change: {res.message}")
        self.journal.update_levels(self.run_id, p.ticket, sl, tp)
        self.journal.log_event(self.run_id, self.broker.now(), "modify", reasoning,
                               {"ticket": p.ticket, "stop_loss": sl, "take_profit": tp})
        return {"status": "modified", "ticket": p.ticket, "stop_loss": sl or p.stop_loss,
                "take_profit": tp or p.take_profit}

    def t_close_trade(self, ticket, reasoning):
        p = self.broker.position(int(ticket))
        if p is None:
            raise ToolError(f"No open position with ticket {ticket}")
        res = self.broker.close_position(p.ticket)
        if not res.ok:
            raise ToolError(f"Broker could not close: {res.message}")
        self.journal.set_close_reasoning(self.run_id, p.ticket, reasoning)
        return {"status": "closed", "ticket": p.ticket, "price": res.price}


def decision_due(now, timeframe: str) -> bool:
    """True when `now` is exactly on a boundary of `timeframe` (i.e. a bar just closed)."""
    td = TIMEFRAMES[timeframe]
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return (now - midnight) % td == pd.Timedelta(0)
