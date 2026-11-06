"""The playbook as code: deterministic, causal setup scanners.

The SAME functions are used for historical research (edge statistics) and for live scanning, so
what the agent sees live is exactly what was measured. Each signal is evaluated on a completed H1
bar; the decision time is that bar's close; higher timeframes only contribute bars that had already
closed at that moment (no look-ahead).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .market import bars_with_indicators

SETUPS = ("trend-pullback", "breakout-retest", "range-fade")
SESSIONS_UTC = [("asia", 0, 7), ("london", 7, 12), ("overlap", 12, 16), ("newyork", 16, 21), ("late", 21, 24)]

_HTF_COLS = ["close", "ema50", "ema200", "adx14", "atr14"]


def _align(h1: pd.DataFrame, htf: pd.DataFrame, td: pd.Timedelta, prefix: str) -> pd.DataFrame:
    left = h1.assign(_avail=h1.index + pd.Timedelta(hours=1)).reset_index(names="time")
    right = htf[_HTF_COLS].copy()
    right["ema50_slope"] = right["ema50"].diff(6)
    right = right.add_prefix(prefix)
    right["_avail"] = htf.index + td
    merged = pd.merge_asof(left.sort_values("_avail"), right.sort_values("_avail"), on="_avail",
                           direction="backward")
    return merged.set_index("time").drop(columns="_avail")


def build_features(h1: pd.DataFrame, h4: pd.DataFrame, d1: pd.DataFrame) -> pd.DataFrame:
    """h1/h4/d1 must already contain indicators (see indicators.add_indicators)."""
    f = _align(h1, h4, pd.Timedelta(hours=4), "h4_")
    f = _align(f, d1, pd.Timedelta(days=1), "d1_")
    decision = f.index + pd.Timedelta(hours=1)
    hours = decision.hour
    f["session"] = "late"
    for name, a, b in SESSIONS_UTC:
        f.loc[(hours >= a) & (hours < b), "session"] = name
    adx = f["h4_adx14"]
    f["regime"] = np.where(adx >= 25, "trending", np.where(adx < 20, "ranging", "neutral"))
    return f


def _signals_frame(f: pd.DataFrame) -> pd.DataFrame:
    """Vectorised signal detection. Returns one row per (bar, setup, direction) that fired."""
    c, o, h, l, atr = f["close"], f["open"], f["high"], f["low"], f["atr14"]
    out = []

    # 1) trend-pullback: higher-timeframe trend, pullback into EMA20 on H1, continuation candle
    up = (f["d1_ema50"] > f["d1_ema200"]) & (f["h4_close"] > f["h4_ema50"]) & (f["h4_ema50"] > f["h4_ema200"])
    dn = (f["d1_ema50"] < f["d1_ema200"]) & (f["h4_close"] < f["h4_ema50"]) & (f["h4_ema50"] < f["h4_ema200"])
    touched_dn = l.rolling(4).min() <= f["ema20"]
    touched_up = h.rolling(4).max() >= f["ema20"]
    long_tp = up & touched_dn & (c > f["ema20"]) & (c > o) & (f["ema20"] > f["ema50"]) & f["rsi14"].between(40, 65)
    short_tp = dn & touched_up & (c < f["ema20"]) & (c < o) & (f["ema20"] < f["ema50"]) & f["rsi14"].between(35, 60)
    out.append(_mk(f, long_tp, "trend-pullback", 1, l.rolling(5).min() - 0.2 * atr, None))
    out.append(_mk(f, short_tp, "trend-pullback", -1, h.rolling(5).max() + 0.2 * atr, None))

    # 2) breakout-retest: 24-bar range broken in the last 4 bars, now retested and held
    prior_high = h.rolling(24).max().shift(4)
    prior_low = l.rolling(24).min().shift(4)
    broke_up = c.rolling(4).max() > prior_high + 0.1 * atr
    broke_dn = c.rolling(4).min() < prior_low - 0.1 * atr
    long_br = broke_up & (l <= prior_high + 0.3 * atr) & (c > prior_high) & (c > o) & (f["adx14"] > 20)
    short_br = broke_dn & (h >= prior_low - 0.3 * atr) & (c < prior_low) & (c < o) & (f["adx14"] > 20)
    out.append(_mk(f, long_br, "breakout-retest", 1, prior_high - 0.8 * atr, None))
    out.append(_mk(f, short_br, "breakout-retest", -1, prior_low + 0.8 * atr, None))

    # 3) range-fade: ranging H4, rejection at the H1 Bollinger band, target the middle band
    ranging = (f["h4_adx14"] < 20) & ((f["h4_ema50_slope"].abs() / f["h4_atr14"]) < 0.5)
    short_rf = ranging & (h >= f["bb_up"]) & (c < f["bb_up"]) & (c < o) & (f["rsi14"] > 60)
    long_rf = ranging & (l <= f["bb_low"]) & (c > f["bb_low"]) & (c > o) & (f["rsi14"] < 40)
    out.append(_mk(f, short_rf, "range-fade", -1, h + 0.5 * atr, f["bb_mid"]))
    out.append(_mk(f, long_rf, "range-fade", 1, l - 0.5 * atr, f["bb_mid"]))

    sig = pd.concat([x for x in out if not x.empty]) if any(not x.empty for x in out) else pd.DataFrame()
    if sig.empty:
        return sig
    risk = (sig["ref_price"] - sig["stop"]) * sig["direction"]
    sig = sig[(risk > 0.5 * sig["atr"]) & (risk < 3.0 * sig["atr"])]
    tgt_dist = (sig["target"] - sig["ref_price"]) * sig["direction"]
    sig = sig[tgt_dist >= 1.0 * (sig["ref_price"] - sig["stop"]) * sig["direction"]]
    return sig.sort_index()


def _mk(f, mask, setup, direction, stop, target) -> pd.DataFrame:
    mask = mask.fillna(False)
    if not mask.any():
        return pd.DataFrame()
    rows = f.loc[mask]
    ref = rows["close"]
    st = stop.loc[mask]
    tg = target.loc[mask] if target is not None else ref + direction * 2.0 * (ref - st).abs()
    return pd.DataFrame({
        "setup": setup, "direction": direction, "ref_price": ref, "stop": st, "target": tg,
        "atr": rows["atr14"], "session": rows["session"], "regime": rows["regime"],
        "rsi": rows["rsi14"], "adx": rows["adx14"],
    }, index=rows.index)


def find_signals(h1, h4, d1) -> pd.DataFrame:
    """All historical signals; index = H1 bar open time (decision at +1h)."""
    return _signals_frame(build_features(h1, h4, d1))


def scan_now(broker, symbols: list[str]) -> list[dict]:
    """Signals that fired on the most recent completed H1 bar."""
    found = []
    for sym in symbols:
        h1 = bars_with_indicators(broker, sym, "H1", 400)
        h4 = bars_with_indicators(broker, sym, "H4", 300)
        d1 = bars_with_indicators(broker, sym, "D1", 260)
        if h1.empty or h4.empty or d1.empty:
            continue
        sig = find_signals(h1, h4, d1)
        if sig.empty:
            continue
        last = sig[sig.index == h1.index[-1]]
        for t, r in last.iterrows():
            found.append({"symbol": sym, "setup": r.setup, "direction": "buy" if r.direction > 0 else "sell",
                          "bar_time": t, "ref_close": r.ref_price, "suggested_stop": r.stop,
                          "suggested_target": r.target, "atr": r.atr, "session": r.session,
                          "regime": r.regime})
    return found
