"""Feature frame used by the research lab (one row per completed H1 bar, decision at its close).

Everything is causal: a row only uses information available at the close of its H1 bar.
Forward returns (`fwd_*`) are the ONLY look-ahead columns and exist purely as research targets;
strategies must never read them (the evaluator removes them before calling a strategy)."""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..indicators import add_indicators
from ..playbook import build_features
from ..research import resample

FWD_HORIZONS = (1, 4, 12, 24)


def _prev_day_levels(h1: pd.DataFrame, d1: pd.DataFrame) -> pd.DataFrame:
    """Previous completed day's high/low/close for each H1 row."""
    lv = d1[["high", "low", "close"]].add_prefix("pd_")
    lv["_avail"] = d1.index + pd.Timedelta(days=1)
    left = pd.DataFrame({"time": h1.index, "_avail": h1.index + pd.Timedelta(hours=1)})
    m = pd.merge_asof(left.sort_values("_avail"), lv.sort_values("_avail"), on="_avail", direction="backward")
    return m.set_index("time").drop(columns="_avail")


def _asia_range(h1: pd.DataFrame) -> pd.DataFrame:
    """Today's Asia session (00:00-07:00, data clock) high/low, known only after 07:00."""
    day = h1.index.normalize()
    in_asia = h1.index.hour < 7
    hi = h1["high"].where(in_asia).groupby(day).transform("max")
    lo = h1["low"].where(in_asia).groupby(day).transform("min")
    known = h1.index.hour >= 7
    return pd.DataFrame({"asia_high": hi.where(known), "asia_low": lo.where(known)}, index=h1.index)


def lab_features(base: pd.DataFrame) -> pd.DataFrame:
    """Build the full feature frame from base (e.g. M15) bars."""
    h1 = add_indicators(resample(base, "1h"))
    h4 = add_indicators(resample(base, "4h"))
    d1 = add_indicators(resample(base, "1D"))
    f = build_features(h1, h4, d1)
    f = f.join(_prev_day_levels(h1, d1)).join(_asia_range(h1))
    atr = f["atr14"]
    f["ret_1h_atr"] = f["close"].diff() / atr
    f["ret_4h_atr"] = f["close"].diff(4) / atr
    f["ret_24h_atr"] = f["close"].diff(24) / atr
    f["dist_ema200_atr"] = (f["close"] - f["ema200"]) / atr
    f["dist_ema50_atr"] = (f["close"] - f["ema50"]) / atr
    up = (f["close"] > f["open"]).astype(int)
    grp = (up != up.shift()).cumsum()
    f["streak"] = up.groupby(grp).cumcount().add(1) * np.where(up == 1, 1, -1)
    f["range_atr"] = (f["high"] - f["low"]) / atr
    decision = f.index + pd.Timedelta(hours=1)
    f["hour"] = decision.hour
    f["dow"] = decision.dayofweek
    for h in FWD_HORIZONS:
        f[f"fwd_{h}h_atr"] = (f["close"].shift(-h) - f["close"]) / atr
    return f


def strategy_view(f: pd.DataFrame) -> pd.DataFrame:
    """Features a strategy is allowed to see (forward-looking research targets removed)."""
    return f[[c for c in f.columns if not c.startswith("fwd_")]]
