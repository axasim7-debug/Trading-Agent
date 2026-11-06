"""Hard risk limits. These live in code, not in the prompt: the agent cannot talk its way past them."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date

from .broker.base import Broker
from .config import RiskConfig
from .models import Direction, Position


@dataclass
class OrderPlan:
    ok: bool
    volume: float = 0.0
    risk_amount: float = 0.0
    entry_estimate: float = 0.0
    stop_distance: float = 0.0
    reward_risk: float | None = None
    reasons: list[str] = field(default_factory=list)


class RiskManager:
    def __init__(self, cfg: RiskConfig, broker: Broker, commission_per_lot: float = 0.0,
                 journal=None, run_id: int | None = None, calendar=None):
        """`journal`/`run_id` make daily state survive restarts (each Claude Code session starts a
        fresh process). `calendar` (EconomicCalendar) enables the news blackout."""
        self.cfg = cfg
        self.broker = broker
        self.commission_per_lot = commission_per_lot
        self.journal = journal
        self.run_id = run_id
        self.calendar = calendar
        self.day: date | None = None
        self.day_start_equity = 0.0
        self.trades_today = 0
        self.roll_day()

    # ---- daily state ----------------------------------------------------------------------
    def roll_day(self) -> bool:
        """Reset daily counters when the broker date changes. Returns True on a new day."""
        today = self.broker.now().date()
        if self.journal is not None:
            key = f"day_start_equity:{today}"
            eq = self.journal.get_kv(self.run_id, key)
            if eq is None:
                eq = self.broker.account().equity
                self.journal.set_kv(self.run_id, key, eq)
            self.day_start_equity = eq
            self.trades_today = self.journal.trades_opened_on(self.run_id, str(today))
            new = today != self.day
            self.day = today
            return new
        if today != self.day:
            self.day = today
            self.day_start_equity = self.broker.account().equity
            self.trades_today = 0
            return True
        return False

    def register_open(self) -> None:
        if self.journal is None:
            self.trades_today += 1
        # with a journal the count is read back from the trades table

    def daily_pnl(self) -> float:
        return self.broker.account().equity - self.day_start_equity

    def status(self) -> dict:
        pnl = self.daily_pnl()
        base = self.day_start_equity or 1.0
        return {
            "day_start_equity": round(self.day_start_equity, 2),
            "daily_pnl": round(pnl, 2),
            "daily_pnl_pct": round(100 * pnl / base, 3),
            "daily_target_pct": self.cfg.daily_profit_target_pct,
            "daily_loss_limit_pct": self.cfg.max_daily_loss_pct,
            "trades_today": self.trades_today,
            "max_trades_per_day": self.cfg.max_trades_per_day,
            "open_risk_pct": round(100 * self.open_risk() / base, 3),
            "max_total_open_risk_pct": self.cfg.max_total_open_risk_pct,
            "max_risk_per_trade_pct": self.cfg.max_risk_per_trade_pct,
            "new_trades_blocked": self.block_reason(),
            "currency_exposure_pct": {k: round(100 * v / base, 2)
                                      for k, v in self.currency_exposure().items() if abs(v) > 0},
            "max_currency_exposure_pct": self.cfg.max_currency_exposure_pct,
        }

    def block_reason(self) -> str | None:
        base = self.day_start_equity or 1.0
        pnl_pct = 100 * self.daily_pnl() / base
        if pnl_pct <= -self.cfg.max_daily_loss_pct:
            return f"daily loss limit reached ({pnl_pct:.2f}%)"
        if self.cfg.stop_trading_at_target and pnl_pct >= self.cfg.daily_profit_target_pct:
            return f"daily profit target reached ({pnl_pct:.2f}%) and stop_trading_at_target is on"
        if self.trades_today >= self.cfg.max_trades_per_day:
            return "max trades per day reached"
        return None

    # ---- risk maths -----------------------------------------------------------------------
    def loss_per_lot(self, symbol: str, stop_distance: float) -> float:
        spec = self.broker.symbol_spec(symbol)
        return self.broker.quote_to_account(symbol, stop_distance * spec.contract_size) + self.commission_per_lot

    def position_risk(self, p: Position) -> float:
        """Money still at risk if the stop is hit (0 once the stop is at/through break-even)."""
        if p.stop_loss is None:
            return float("inf")
        dist = (p.entry_price - p.stop_loss) * p.direction.sign
        if dist <= 0:
            return 0.0
        spec = self.broker.symbol_spec(p.symbol)
        return self.broker.quote_to_account(p.symbol, dist * spec.contract_size * p.volume)

    def open_risk(self) -> float:
        return sum(self.position_risk(p) for p in self.broker.positions())

    def currency_exposure(self, extra: tuple[str, Direction, float] | None = None) -> dict[str, float]:
        """Net money-at-risk per currency. Long EURUSD = long EUR / short USD."""
        exp: dict[str, float] = {}
        items = [(p.symbol, p.direction, self.position_risk(p)) for p in self.broker.positions()]
        if extra:
            items.append(extra)
        for sym, d, risk in items:
            if risk == float("inf"):
                continue
            spec = self.broker.symbol_spec(sym)
            exp[spec.base] = exp.get(spec.base, 0.0) + d.sign * risk
            exp[spec.quote] = exp.get(spec.quote, 0.0) - d.sign * risk
        return exp

    def news_block(self, symbol: str) -> str | None:
        if self.calendar is None:
            return None
        now_utc = self.broker.now() - self.broker.utc_offset()
        try:
            ev = self.calendar.blackout(symbol, now_utc, self.cfg.news_blackout_minutes_before,
                                        self.cfg.news_blackout_minutes_after, self.cfg.news_blackout_min_impact)
        except Exception as e:  # calendar unavailable: fail safe, say so
            return f"economic calendar unavailable ({e}); cannot verify news blackout"
        if ev:
            return (f"news blackout: {ev.currency} {ev.impact}-impact '{ev.title}' at "
                    f"{(ev.time_utc + self.broker.utc_offset()):%H:%M} server time")
        return None

    # ---- checks ---------------------------------------------------------------------------
    def plan_order(self, symbol: str, direction: Direction, stop_loss: float,
                   take_profit: float | None, risk_percent: float) -> OrderPlan:
        self.roll_day()
        reasons: list[str] = []
        c = self.cfg
        spec = self.broker.symbol_spec(symbol)
        bid, ask = self.broker.quote(symbol)
        entry = ask if direction is Direction.BUY else bid
        spread = ask - bid

        if (r := self.block_reason()):
            reasons.append(f"new trades blocked: {r}")
        if not (0 < risk_percent <= c.max_risk_per_trade_pct):
            reasons.append(f"risk_percent must be in (0, {c.max_risk_per_trade_pct}]")
        if stop_loss is None:
            reasons.append("stop_loss is required")
            return OrderPlan(False, reasons=reasons)

        # stop/target must be on the correct side of the exit price
        exit_side = bid if direction is Direction.BUY else ask
        stop_ok = stop_loss < exit_side if direction is Direction.BUY else stop_loss > exit_side
        if not stop_ok:
            reasons.append(f"stop_loss {stop_loss} is on the wrong side of the market (bid {bid}, ask {ask})")
        stop_distance = abs(entry - stop_loss)
        rr = None
        if take_profit is None:
            if c.require_take_profit:
                reasons.append("take_profit is required")
        else:
            tp_ok = take_profit > entry if direction is Direction.BUY else take_profit < entry
            if not tp_ok:
                reasons.append(f"take_profit {take_profit} is on the wrong side of entry {entry}")
            elif stop_distance > 0:
                rr = abs(take_profit - entry) / stop_distance
                if rr < c.min_reward_risk:
                    reasons.append(f"reward:risk {rr:.2f} is below the minimum {c.min_reward_risk}")

        if stop_distance > 0 and spread > c.max_spread_to_stop_ratio * stop_distance:
            reasons.append(
                f"stop too tight for the spread: spread {spread / spec.pip:.1f} pips vs stop "
                f"{stop_distance / spec.pip:.1f} pips (max ratio {c.max_spread_to_stop_ratio})")

        positions = self.broker.positions()
        if len(positions) >= c.max_open_positions:
            reasons.append(f"max open positions ({c.max_open_positions}) reached")
        if sum(p.symbol == symbol for p in positions) >= c.max_positions_per_symbol:
            reasons.append(f"max positions per symbol ({c.max_positions_per_symbol}) reached for {symbol}")

        equity = self.broker.account().equity
        risk_amount = equity * max(risk_percent, 0) / 100
        volume = 0.0
        actual_risk = 0.0
        if stop_ok and stop_distance > 0:
            lpl = self.loss_per_lot(symbol, stop_distance)
            raw = risk_amount / lpl
            volume = math.floor(raw / spec.volume_step + 1e-9) * spec.volume_step
            volume = round(min(volume, spec.volume_max), 8)
            if volume < spec.volume_min:
                reasons.append(
                    f"stop too wide: even the minimum volume {spec.volume_min} would risk "
                    f"{spec.volume_min * lpl:.2f} > allowed {risk_amount:.2f}")
            actual_risk = volume * lpl
            max_open = equity * c.max_total_open_risk_pct / 100
            if self.open_risk() + actual_risk > max_open + 1e-9:
                reasons.append(
                    f"total open risk would exceed {c.max_total_open_risk_pct}% of equity")
            cap = equity * c.max_currency_exposure_pct / 100
            for ccy, v in self.currency_exposure((symbol, direction, actual_risk)).items():
                if abs(v) > cap + 1e-9:
                    reasons.append(f"net {ccy} exposure would be {100 * v / equity:+.2f}% of equity "
                                   f"(max {c.max_currency_exposure_pct}%) - correlated with open trades")
        if (nb := self.news_block(symbol)):
            reasons.append(nb)

        return OrderPlan(not reasons, volume, actual_risk, entry, stop_distance, rr, reasons)

    def check_modify(self, p: Position, new_sl: float | None, new_tp: float | None) -> list[str]:
        reasons = []
        bid, ask = self.broker.quote(p.symbol)
        exit_side = bid if p.direction is Direction.BUY else ask
        if new_sl is not None:
            if p.stop_loss is not None and (new_sl - p.stop_loss) * p.direction.sign < 0:
                reasons.append("stop_loss can only be tightened (moved toward profit), never widened")
            ok = new_sl < exit_side if p.direction is Direction.BUY else new_sl > exit_side
            if not ok:
                reasons.append(f"stop_loss {new_sl} is on the wrong side of the market (bid {bid}, ask {ask})")
        if new_tp is not None:
            ok = new_tp > exit_side if p.direction is Direction.BUY else new_tp < exit_side
            if not ok:
                reasons.append(f"take_profit {new_tp} is on the wrong side of the market")
        return reasons

