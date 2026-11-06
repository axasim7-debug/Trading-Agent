"""Deterministic trade management, executed by the watchdog between agent cycles.

Each position has a management plan (stored in the journal under `mgmt:<ticket>`): break-even,
partial take-profit, ATR trailing stop and a time stop, all expressed in R (multiples of the
initial risk). Stops are only ever tightened, and every change goes through the risk manager."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime

from .market import bars_with_indicators
from .models import Direction, Position


@dataclass
class ManagementPlan:
    entry: float
    initial_stop: float
    break_even_at_r: float | None = 1.0
    break_even_lock_r: float = 0.05        # move the stop slightly past entry to cover costs
    partial_at_r: float | None = None
    partial_fraction: float = 0.5
    trail_atr_mult: float | None = None
    trail_timeframe: str = "H1"
    trail_after_r: float = 1.0
    time_stop_hours: float | None = None
    done: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> "ManagementPlan":
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**known)

    def to_dict(self) -> dict:
        return asdict(self)


DEFAULTS = dict(break_even_at_r=1.0, partial_at_r=None, partial_fraction=0.5, trail_atr_mult=None,
                trail_timeframe="H1", trail_after_r=1.0, time_stop_hours=None)


def plan_key(ticket: int) -> str:
    return f"mgmt:{ticket}"


def default_plan(p: Position, defaults: dict | None = None) -> ManagementPlan:
    return ManagementPlan(entry=p.entry_price, initial_stop=p.stop_loss or p.entry_price,
                          **{**DEFAULTS, **(defaults or {})})


def apply(broker, risk, p: Position, plan: ManagementPlan, now: datetime) -> list[str]:
    """Apply the plan to one position. Returns human-readable actions taken."""
    actions: list[str] = []
    risk_dist = abs(plan.entry - plan.initial_stop)
    if risk_dist <= 0:
        return actions
    bid, ask = broker.quote(p.symbol)
    cur = bid if p.direction is Direction.BUY else ask
    sign = p.direction.sign
    r_now = (cur - plan.entry) * sign / risk_dist

    def tighten(new_sl: float, why: str) -> None:
        if p.stop_loss is not None and (new_sl - p.stop_loss) * sign <= 0:
            return  # not tighter
        if risk.check_modify(p, new_sl, None):
            return  # invalid (e.g. would be on the wrong side of price)
        res = broker.modify_position(p.ticket, new_sl, None)
        if res.ok:
            p.stop_loss = new_sl
            actions.append(f"#{p.ticket} {p.symbol}: stop -> {new_sl:.5f} ({why}, {r_now:+.2f}R)")

    if plan.time_stop_hours and (now - p.open_time).total_seconds() >= plan.time_stop_hours * 3600:
        res = broker.close_position(p.ticket)
        if res.ok:
            actions.append(f"#{p.ticket} {p.symbol}: closed by time stop ({plan.time_stop_hours}h, {r_now:+.2f}R)")
            return actions

    if plan.partial_at_r and not plan.done.get("partial") and r_now >= plan.partial_at_r:
        res = broker.close_partial(p.ticket, p.volume * plan.partial_fraction)
        plan.done["partial"] = True
        if res.ok:
            actions.append(f"#{p.ticket} {p.symbol}: partial close {res.volume} lots at {r_now:+.2f}R")

    if plan.break_even_at_r and not plan.done.get("break_even") and r_now >= plan.break_even_at_r:
        tighten(plan.entry + sign * plan.break_even_lock_r * risk_dist, "break-even")
        plan.done["break_even"] = True

    if plan.trail_atr_mult and r_now >= plan.trail_after_r:
        bars = bars_with_indicators(broker, p.symbol, plan.trail_timeframe, 3)
        if not bars.empty and bars["atr14"].notna().iloc[-1]:
            last = bars.iloc[-1]
            tighten(float(last["close"]) - sign * plan.trail_atr_mult * float(last["atr14"]),
                    f"ATR trail x{plan.trail_atr_mult} {plan.trail_timeframe}")
    return actions
