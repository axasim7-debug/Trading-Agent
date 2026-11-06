"""Keeps the journal in sync with the broker, and measures the quality of the agent's decisions.

* sync(): pending orders filled / cancelled, positions closed (with MAE/MFE), notifications.
* Shadow book: every playbook signal the agent was shown is evaluated as if it had been taken, so we
  can compare "signals taken" vs "signals skipped" - does the agent's discretion add value?
* Calibration: the agent states a win probability for each trade; we check whether e.g. its
  "60%" trades really win about 60% of the time (Brier score).
* Excursions: how far trades went against/for us before closing (stop and target placement).
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

from . import management as mg
from .models import Direction
from .research import simulate_signals


class Sync:
    def __init__(self, broker, journal, run_id, notifier=None, default_management=None, max_hold_hours=72):
        self.broker = broker
        self.journal = journal
        self.run_id = run_id
        self.notifier = notifier
        self.default_management = default_management
        self.max_hold_hours = max_hold_hours

    def _notify(self, text):
        if self.notifier is not None:
            self.notifier.send(text)

    def run(self) -> dict:
        out = {"filled": [], "closed": [], "cancelled": []}
        live_orders = {o.ticket for o in self.broker.pending_orders()}
        positions = {p.ticket: p for p in self.broker.positions()}
        for o in self.journal.orders(self.run_id, "pending"):
            t = o["ticket"]
            if t in live_orders:
                continue
            if t in positions:
                p = positions[t]
                self.journal.set_order_status(self.run_id, t, "filled")
                self.journal.record_open(self.run_id, t, o["symbol"], o["direction"], p.volume, p.entry_price,
                                         o["stop_loss"], o["take_profit"], o["risk"], p.open_time, o["setup"],
                                         o["reasoning"], o["win_probability"], entry_type=o["order_type"])
                if self.journal.get_kv(self.run_id, mg.plan_key(t)) is None:
                    plan = mg.default_plan(p, self.default_management)
                    plan.initial_stop = o["stop_loss"]
                    self.journal.set_kv(self.run_id, mg.plan_key(t), plan.to_dict())
                self.journal.mark_signal_taken(self.run_id, o["symbol"], o["setup"],
                                               1 if o["direction"] == "buy" else -1, t)
                out["filled"].append(t)
                self._notify(f"✅ تنفيذ أمر معلّق #{t}: {o['direction']} {o['symbol']} @ {p.entry_price} "
                             f"| وقف {o['stop_loss']} | هدف {o['take_profit']} | {o['setup']}")
            else:
                ct = self.broker.closed_trade(t)
                if ct is not None:   # filled and already closed between two syncs
                    self.journal.set_order_status(self.run_id, t, "filled")
                    self.journal.record_open(self.run_id, t, o["symbol"], o["direction"], ct.volume, ct.entry_price,
                                             o["stop_loss"], o["take_profit"], o["risk"], ct.open_time,
                                             o["setup"], o["reasoning"], o["win_probability"], o["order_type"])
                else:
                    expired = o["expires"] and str(self.broker.now())[:16] >= o["expires"]
                    self.journal.set_order_status(self.run_id, t, "expired" if expired else "cancelled")
                    out["cancelled"].append(t)

        for ticket in self.journal.open_tickets(self.run_id):
            ct = self.broker.closed_trade(ticket)
            if ct is None:
                continue
            if self.journal.record_close(self.run_id, ct):
                out["closed"].append(ticket)
                self._excursions(ticket, ct)
                row = self.journal.trades(self.run_id)
                r = row[row.ticket == ticket]["r_multiple"].iloc[0] if not row.empty else None
                icon = "🟢" if ct.profit > 0 else "🔴"
                self._notify(f"{icon} إغلاق #{ticket} {ct.symbol}: {ct.profit:+.2f} ({'' if r is None else f'{r:+.2f}R'}) "
                             f"سبب: {ct.close_reason}")
        return out

    def _excursions(self, ticket, ct) -> None:
        raw = self.journal.get_kv(self.run_id, mg.plan_key(ticket))
        trades = self.journal.trades(self.run_id)
        row = trades[trades.ticket == ticket]
        if row.empty:
            return
        stop = raw["initial_stop"] if raw else row["stop_loss"].iloc[0]
        entry = ct.entry_price
        dist = abs(entry - stop) if stop else 0
        if not dist:
            return
        hours = (ct.close_time - ct.open_time).total_seconds() / 3600
        try:
            bars = self.broker.get_bars(ct.symbol, "M15", int(min(hours * 4 + 12, 3000)))
        except Exception:
            return
        win = bars[(bars.index >= pd.Timestamp(ct.open_time).floor("15min")) & (bars.index < pd.Timestamp(ct.close_time))]
        if win.empty:
            return
        if ct.direction is Direction.BUY:
            mfe, mae = (win["high"].max() - entry) / dist, (entry - win["low"].min()) / dist
        else:
            mfe, mae = (entry - win["low"].min()) / dist, (win["high"].max() - entry) / dist
        self.journal.set_excursions(self.run_id, ticket, round(float(mae), 3), round(float(mfe), 3))

    # ---- shadow book -----------------------------------------------------------------------
    def evaluate_signals(self) -> int:
        now = self.broker.now()
        done = 0
        for s in self.journal.signals(self.run_id, evaluated=False):
            bar_time = datetime.fromisoformat(s["bar_time"])
            decision = bar_time + timedelta(hours=1)
            if now <= decision:
                continue
            hours = (now - bar_time).total_seconds() / 3600
            try:
                base = self.broker.get_bars(s["symbol"], "M15", int(min(hours * 4 + 8, 3000)))
            except Exception:
                continue
            sig = pd.DataFrame([{"setup": s["setup"], "direction": s["direction"], "stop": s["stop"],
                                 "target": s["target"], "session": s["session"], "regime": s["regime"]}],
                               index=[pd.Timestamp(bar_time)])
            res = simulate_signals(sig, base, self.broker.symbol_spec(s["symbol"]), 0.0, self.max_hold_hours)
            if res.empty:
                continue
            r = res.iloc[0]
            final = r["reason"] != "time" or now >= decision + timedelta(hours=self.max_hold_hours)
            if final:
                self.journal.set_signal_outcome(s["id"], float(r["r"]))
                done += 1
        return done


def calibration_report(trades: pd.DataFrame) -> dict:
    t = trades.dropna(subset=["profit", "win_probability"]) if "win_probability" in trades else pd.DataFrame()
    if t.empty:
        return {"n": 0, "note": "no closed trades with a stated win_probability yet"}
    won = (t["profit"] > 0).astype(float)
    p = t["win_probability"].astype(float)
    bins = pd.cut(p, [0, 0.4, 0.5, 0.6, 0.7, 1.0], include_lowest=True)
    table = (pd.DataFrame({"bin": bins.astype(str), "p": p, "won": won}).groupby("bin", observed=True)
             .agg(n=("won", "size"), stated=("p", "mean"), realised=("won", "mean")).round(3).reset_index())
    return {"n": len(t), "brier_score": round(float(((p - won) ** 2).mean()), 4),
            "brier_if_always_50pct": 0.25, "avg_stated": round(float(p.mean()), 3),
            "avg_realised": round(float(won.mean()), 3), "by_bin": table.to_dict("records"),
            "reading": "Lower Brier is better. If stated >> realised, you are overconfident."}


def discretion_report(signals: list[dict], trades: pd.DataFrame) -> dict:
    ev = [s for s in signals if s["evaluated"]]
    if not ev:
        return {"note": "no evaluated playbook signals yet"}
    df = pd.DataFrame(ev)
    taken, skipped = df[df.taken == 1], df[df.taken == 0]
    actual = trades.set_index("ticket")["r_multiple"] if not trades.empty else pd.Series(dtype=float)
    taken_actual = [actual.get(t) for t in taken["ticket"] if t in actual.index and pd.notna(actual.get(t))]
    def summ(x):
        x = pd.Series(x, dtype=float)
        return {"n": int(len(x)), "avg_r": round(float(x.mean()), 3) if len(x) else None,
                "win_rate": round(float((x > 0).mean()), 3) if len(x) else None}
    return {"signals_evaluated": len(df),
            "taken_shadow": summ(taken["outcome_r"]), "skipped_shadow": summ(skipped["outcome_r"]),
            "taken_actual_result": summ(taken_actual),
            "reading": ("If skipped signals perform as well as taken ones, your filtering adds nothing. "
                        "If actual < shadow on taken signals, your management (exits) is costing R.")}


def excursion_report(trades: pd.DataFrame) -> dict:
    t = trades.dropna(subset=["mae_r", "mfe_r", "r_multiple"]) if "mae_r" in trades else pd.DataFrame()
    if t.empty:
        return {"note": "no excursion data yet"}
    win, loss = t[t.r_multiple > 0], t[t.r_multiple <= 0]
    return {"n": len(t),
            "winners_avg_mae_r": round(float(win.mae_r.mean()), 3) if len(win) else None,
            "losers_avg_mfe_r": round(float(loss.mfe_r.mean()), 3) if len(loss) else None,
            "losers_that_were_up_1R_pct": round(100 * float((loss.mfe_r >= 1).mean()), 1) if len(loss) else None,
            "winners_that_nearly_stopped_pct": round(100 * float((win.mae_r >= 0.8).mean()), 1) if len(win) else None,
            "avg_mfe_r_all": round(float(t.mfe_r.mean()), 3),
            "reading": ("Many losers that were +1R first -> manage earlier (break-even/partials). "
                        "Winners rarely going below -0.5R -> stops may be wider than needed.")}
