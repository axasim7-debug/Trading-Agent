"""Measures the historical edge of every playbook setup (no LLM involved).

For each signal: enter at the next bar's open (+spread on buys), exit at stop / target (stop wins
if both are touched in one bar) or after `max_hold_hours`. Results are net of spread and
commission, in R. Statistics are split in-sample / out-of-sample by time, and a setup slice is only
marked `validated` when it is positive in BOTH halves with enough trades.

    python -m trading_agent research          # writes cache/edge_stats.json
"""
from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from .indicators import add_indicators
from .playbook import find_signals

_AGG = {"open": "first", "high": "max", "low": "min", "close": "last", "tick_volume": "sum", "spread": "max"}


def resample(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    return df.resample(rule, label="left", closed="left").agg(_AGG).dropna(subset=["open"])


def simulate_signals(sig: pd.DataFrame, base: pd.DataFrame, spec, commission_per_lot: float,
                     max_hold_hours: int) -> pd.DataFrame:
    """Walk each signal forward on base (e.g. M15) bars. One open trade per setup at a time."""
    if sig.empty:
        return sig.assign(r=[])
    t_idx = base.index.values
    o, h, l, c = (base[k].to_numpy() for k in ("open", "high", "low", "close"))
    spr = np.where(base["spread"].to_numpy() > 0, base["spread"].to_numpy(), spec.default_spread_points) * spec.point
    busy_until: dict[str, np.datetime64] = {}
    rows = []
    for t, s in sig.iterrows():
        decision = np.datetime64(t + pd.Timedelta(hours=1))
        if busy_until.get(s.setup, np.datetime64("1970-01-01")) > decision:
            continue
        i = int(np.searchsorted(t_idx, decision, side="left"))
        if i >= len(t_idx):
            continue
        d = int(s.direction)
        entry = o[i] + (spr[i] if d > 0 else 0.0)
        stop, target = float(s.stop), float(s.target)
        risk = (entry - stop) * d
        if risk <= 0 or (target - entry) * d <= 0:
            continue
        end_t = decision + np.timedelta64(max_hold_hours, "h")
        exit_price, exit_i, reason = None, i, "time"
        j = i
        while j < len(t_idx) and t_idx[j] < end_t:
            s_j = spr[j]
            if d > 0:   # long exits at bid
                if l[j] <= stop:
                    exit_price, reason = (o[j] if o[j] <= stop else stop), "sl"
                elif h[j] >= target:
                    exit_price, reason = (o[j] if o[j] >= target else target), "tp"
            else:       # short exits at ask
                if h[j] + s_j >= stop:
                    exit_price, reason = (o[j] + s_j if o[j] + s_j >= stop else stop), "sl"
                elif l[j] + s_j <= target:
                    exit_price, reason = (o[j] + s_j if o[j] + s_j <= target else target), "tp"
            if exit_price is not None:
                exit_i = j
                break
            j += 1
        if exit_price is None:
            exit_i = min(j, len(t_idx)) - 1
            exit_price = c[exit_i] + (spr[exit_i] if d < 0 else 0.0)
        conv = 1.0 if spec.quote == "USD" else (1.0 / entry if spec.base == "USD" else 1.0)
        cost_r = commission_per_lot / (risk * spec.contract_size * conv)
        r = (exit_price - entry) * d / risk - cost_r
        busy_until[s.setup] = t_idx[exit_i]
        rows.append({"time": t, "setup": s.setup, "direction": d, "session": s.session, "regime": s.regime,
                     "entry": entry, "stop": stop, "target": target, "exit": exit_price, "reason": reason,
                     "r": r})
    return pd.DataFrame(rows)


def _summ(r: pd.Series) -> dict:
    n = len(r)
    if n == 0:
        return {"n": 0}
    out = {"n": n, "win_rate": round(float((r > 0).mean()), 3), "avg_r": round(float(r.mean()), 3),
           "total_r": round(float(r.sum()), 2)}
    if n >= 2 and r.std(ddof=1) > 0:
        t = r.mean() / (r.std(ddof=1) / math.sqrt(n))
        out["t"] = round(float(t), 2)
        out["p"] = round(0.5 * math.erfc(t / math.sqrt(2)), 4)
    return out


def edge_table(trades: pd.DataFrame, oos_start: pd.Timestamp, min_trades: int, min_oos: int) -> list[dict]:
    rows = []
    if trades.empty:
        return rows
    slices = [("setup", ["setup"]), ("setup+symbol", ["setup", "symbol"]),
              ("setup+session", ["setup", "session"]), ("setup+regime", ["setup", "regime"])]
    for level, keys in slices:
        for key, g in trades.groupby(keys):
            key = key if isinstance(key, tuple) else (key,)
            ins, oos = g[g["time"] < oos_start]["r"], g[g["time"] >= oos_start]["r"]
            all_ = _summ(g["r"])
            row = {"level": level, **dict(zip(keys, key)), **all_,
                   "is_avg_r": _summ(ins).get("avg_r"), "oos_n": len(oos), "oos_avg_r": _summ(oos).get("avg_r")}
            row["validated"] = bool(
                all_["n"] >= min_trades and row["oos_n"] >= min_oos and (row["is_avg_r"] or 0) > 0
                and (row["oos_avg_r"] or 0) > 0 and all_.get("p", 1) < 0.10)
            rows.append(row)
    return rows


def run_research(cfg, data: dict[str, pd.DataFrame]) -> dict:
    rc = cfg.research
    all_trades = []
    for sym, base in data.items():
        spec = cfg.spec(sym)
        h1 = add_indicators(resample(base, "1h"))
        h4 = add_indicators(resample(base, "4h"))
        d1 = add_indicators(resample(base, "1D"))
        sig = find_signals(h1, h4, d1)
        tr = simulate_signals(sig, base, spec, cfg.commission_per_lot, rc.max_hold_hours)
        if not tr.empty:
            all_trades.append(tr.assign(symbol=sym))
    trades = pd.concat(all_trades, ignore_index=True) if all_trades else pd.DataFrame(
        columns=["time", "setup", "symbol", "session", "regime", "r"])
    start = min(df.index[0] for df in data.values())
    end = max(df.index[-1] for df in data.values())
    oos_start = start + (end - start) * (1 - rc.oos_fraction)
    report = {
        "generated": datetime.now().isoformat(timespec="seconds"),
        "data_from": str(start), "data_to": str(end), "oos_from": str(oos_start),
        "symbols": list(data), "commission_per_lot": cfg.commission_per_lot,
        "max_hold_hours": rc.max_hold_hours,
        "rules": "validated = n>=min_trades, oos_n>=min_oos, avg R > 0 in-sample AND out-of-sample, p<0.10",
        "table": edge_table(trades, oos_start, rc.min_trades, rc.min_oos_trades),
    }
    path = Path(rc.stats_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
    trades.to_csv(path.with_name("research_trades.csv"), index=False)
    return report


def load_stats(path: str) -> dict | None:
    p = Path(path)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def edge_card(stats: dict | None, setup: str, symbol: str | None = None, session: str | None = None,
              regime: str | None = None) -> dict:
    """Most relevant historical statistics for a live signal."""
    if not stats:
        return {"status": "no research yet - run `python -m trading_agent research`"}
    card = {"data": f"{stats['data_from'][:10]} -> {stats['data_to'][:10]}"}
    want = [("setup", {}), ("setup+symbol", {"symbol": symbol}), ("setup+session", {"session": session}),
            ("setup+regime", {"regime": regime})]
    for level, extra in want:
        if any(v is None for v in extra.values()):
            continue
        for row in stats["table"]:
            if row["level"] == level and row.get("setup") == setup and all(row.get(k) == v for k, v in extra.items()):
                card[level] = {k: row.get(k) for k in ("n", "win_rate", "avg_r", "p", "oos_n", "oos_avg_r", "validated")}
    return card


def format_table(report: dict, level: str = "setup") -> str:
    rows = [r for r in report["table"] if r["level"] == level]
    if not rows:
        return "no trades"
    df = pd.DataFrame(rows).drop(columns="level")
    return df.to_string(index=False)
