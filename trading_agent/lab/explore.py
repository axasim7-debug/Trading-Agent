"""Exploratory anomaly scan: "what does price tend to do after X?"

Tests a library of conditions (time of day, day of week, big moves, RSI extremes, stretched from
EMA200, streaks, prior-day high/low breaks, Asia-range breaks, regime/session) against forward
returns at several horizons, per symbol and pooled. Protections against fooling ourselves:
* non-overlapping events (an event is only counted if it is >= horizon hours after the last one);
* Benjamini-Hochberg false-discovery-rate control across ALL tests run;
* the effect must have the same sign and be meaningful in-sample AND out-of-sample;
* the most recent `holdout_fraction` of the data is never looked at here.
Findings are hypotheses for strategies, not strategies: they ignore spread/stops/costs."""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from .features import FWD_HORIZONS


def conditions(f: pd.DataFrame) -> dict[str, pd.Series]:
    c = {}
    for h in range(24):
        c[f"hour=={h:02d}"] = f["hour"] == h
    for d, name in enumerate(["mon", "tue", "wed", "thu", "fri"]):
        c[f"dow=={name}"] = f["dow"] == d
    c["big_up_1h(>1.5atr)"] = f["ret_1h_atr"] > 1.5
    c["big_down_1h(<-1.5atr)"] = f["ret_1h_atr"] < -1.5
    c["big_up_24h(>3atr)"] = f["ret_24h_atr"] > 3
    c["big_down_24h(<-3atr)"] = f["ret_24h_atr"] < -3
    c["rsi<25"] = f["rsi14"] < 25
    c["rsi>75"] = f["rsi14"] > 75
    c["stretched_above_ema200(>4atr)"] = f["dist_ema200_atr"] > 4
    c["stretched_below_ema200(<-4atr)"] = f["dist_ema200_atr"] < -4
    c["streak_up>=5"] = f["streak"] >= 5
    c["streak_down>=5"] = f["streak"] <= -5
    first_above = (f["close"] > f["pd_high"]) & (f["close"].shift() <= f["pd_high"])
    first_below = (f["close"] < f["pd_low"]) & (f["close"].shift() >= f["pd_low"])
    c["break_prev_day_high"] = first_above
    c["break_prev_day_low"] = first_below
    london = f["hour"].between(7, 11)
    c["london_break_asia_high"] = london & (f["close"] > f["asia_high"]) & (f["close"].shift() <= f["asia_high"])
    c["london_break_asia_low"] = london & (f["close"] < f["asia_low"]) & (f["close"].shift() >= f["asia_low"])
    c["trending_regime&h1_up"] = (f["regime"] == "trending") & (f["close"] > f["ema50"])
    c["ranging_regime&touch_bb_up"] = (f["regime"] == "ranging") & (f["high"] >= f["bb_up"])
    c["ranging_regime&touch_bb_low"] = (f["regime"] == "ranging") & (f["low"] <= f["bb_low"])
    c["wide_range_bar(>2atr)"] = f["range_atr"] > 2
    return {k: v.fillna(False) for k, v in c.items()}


def _thin(times: pd.DatetimeIndex, hours: int) -> np.ndarray:
    """Keep events at least `hours` apart so forward windows do not overlap."""
    keep, last = [], None
    gap = np.timedelta64(hours, "h")
    for t in times.values:
        if last is None or t - last >= gap:
            keep.append(t)
            last = t
    return np.array(keep, dtype="datetime64[ns]")


def _t_p(x: np.ndarray) -> tuple[float, float]:
    n = len(x)
    if n < 3 or np.std(x, ddof=1) == 0:
        return 0.0, 1.0
    t = float(np.mean(x) / (np.std(x, ddof=1) / math.sqrt(n)))
    return t, math.erfc(abs(t) / math.sqrt(2))   # two-sided


def bh_qvalues(p: np.ndarray) -> np.ndarray:
    n = len(p)
    order = np.argsort(p)
    ranked = p[order] * n / (np.arange(n) + 1)
    q = np.minimum.accumulate(ranked[::-1])[::-1]
    out = np.empty(n)
    out[order] = np.minimum(q, 1.0)
    return out


def explore(features: dict[str, pd.DataFrame], holdout_fraction: float = 0.15, oos_fraction: float = 0.3,
            min_events: int = 40) -> pd.DataFrame:
    """features: symbol -> lab_features frame. Returns one row per (condition, horizon, symbol|ALL)."""
    start = min(f.index[0] for f in features.values())
    end = max(f.index[-1] for f in features.values())
    dev_end = start + (end - start) * (1 - holdout_fraction)
    oos_start = start + (dev_end - start) * (1 - oos_fraction)
    rows = []
    pooled: dict[tuple[str, int], list[tuple[np.datetime64, float]]] = {}
    for sym, f in features.items():
        f = f[f.index < dev_end]
        for name, mask in conditions(f).items():
            ev_times = f.index[mask.to_numpy()]
            for h in FWD_HORIZONS:
                col = f[f"fwd_{h}h_atr"]
                kept = _thin(ev_times, h)
                vals = col.reindex(pd.DatetimeIndex(kept)).dropna()
                pooled.setdefault((name, h), []).extend(zip(vals.index.values, vals.values))
                rows.append(_row(name, h, sym, vals, oos_start, min_events))
    for (name, h), pairs in pooled.items():
        if pairs:
            s = pd.Series([v for _, v in pairs], index=pd.DatetimeIndex([t for t, _ in pairs]))
            rows.append(_row(name, h, "ALL", s, oos_start, min_events))
    df = pd.DataFrame([r for r in rows if r is not None])
    if df.empty:
        return df
    df["q_value"] = bh_qvalues(df["p"].to_numpy())
    df["consistent_oos"] = (np.sign(df["is_mean"]) == np.sign(df["oos_mean"])) & (df["oos_t"].abs() >= 1.0)
    df["discovery"] = (df["q_value"] < 0.10) & df["consistent_oos"]
    df["n_tests"] = len(df)
    return df.sort_values(["discovery", "q_value", "p"], ascending=[False, True, True]).reset_index(drop=True)


def _row(name, h, sym, vals: pd.Series, oos_start, min_events):
    if len(vals) < min_events:
        return None
    x = vals.to_numpy()
    t, p = _t_p(x)
    ins, oos = vals[vals.index < oos_start].to_numpy(), vals[vals.index >= oos_start].to_numpy()
    return {"condition": name, "horizon_h": h, "symbol": sym, "n": len(x),
            "mean_fwd_atr": round(float(x.mean()), 4), "hit_rate_up": round(float((x > 0).mean()), 3),
            "t": round(t, 2), "p": p,
            "is_mean": round(float(ins.mean()), 4) if len(ins) else 0.0,
            "oos_mean": round(float(oos.mean()), 4) if len(oos) else 0.0,
            "oos_n": len(oos), "oos_t": round(_t_p(oos)[0], 2) if len(oos) >= 3 else 0.0}
