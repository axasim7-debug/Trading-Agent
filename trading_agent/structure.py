"""Market structure engine: what a discretionary trader marks on the chart before trading.

* swing highs/lows (fractals) and the trend structure they form (HH/HL vs LH/LL);
* support/resistance zones built by clustering swing points (touch count, last touch, strength);
* previous day / week high-low-close, today's Asia-session range;
* average daily range (ADR) and how much of it is already used today;
* nearest levels above/below price, in ATR units.
All computed from completed bars only."""
from __future__ import annotations

from datetime import timedelta

import numpy as np
import pandas as pd

from .market import bars_with_indicators


def swings(df: pd.DataFrame, n: int = 3) -> pd.DataFrame:
    """Fractal swing points: a high higher than the n bars on each side (confirmed n bars later)."""
    h, l = df["high"], df["low"]
    is_high = h == h.rolling(2 * n + 1, center=True).max()
    is_low = l == l.rolling(2 * n + 1, center=True).min()
    out = []
    for t in df.index[is_high.fillna(False)]:
        out.append((t, "high", float(h[t])))
    for t in df.index[is_low.fillna(False)]:
        out.append((t, "low", float(l[t])))
    return pd.DataFrame(out, columns=["time", "kind", "price"]).sort_values("time").reset_index(drop=True)


def trend_structure(sw: pd.DataFrame) -> str:
    highs = sw[sw.kind == "high"]["price"].tail(3).tolist()
    lows = sw[sw.kind == "low"]["price"].tail(3).tolist()
    if len(highs) < 2 or len(lows) < 2:
        return "undetermined"
    hh, hl = highs[-1] > highs[-2], lows[-1] > lows[-2]
    if hh and hl:
        return "bullish (higher highs & higher lows)"
    if not hh and not hl:
        return "bearish (lower highs & lower lows)"
    return "mixed / transitioning"


def sr_zones(sw: pd.DataFrame, atr: float, tolerance_atr: float = 0.35, max_zones: int = 12) -> list[dict]:
    """Cluster swing prices that lie within tolerance*ATR of each other into zones."""
    if sw.empty or not atr or np.isnan(atr):
        return []
    pts = sw.sort_values("price").to_dict("records")
    zones, cur = [], [pts[0]]
    for p in pts[1:]:
        if p["price"] - cur[-1]["price"] <= tolerance_atr * atr:
            cur.append(p)
        else:
            zones.append(cur)
            cur = [p]
    zones.append(cur)
    out = []
    for z in zones:
        prices = [p["price"] for p in z]
        last = max(p["time"] for p in z)
        out.append({"low": min(prices), "high": max(prices), "mid": float(np.mean(prices)),
                    "touches": len(z), "last_touch": last,
                    "kinds": sorted({p["kind"] for p in z})})
    out.sort(key=lambda z: (z["touches"], z["last_touch"]), reverse=True)
    return out[:max_zones]


def _digits(x: float) -> float:
    return float(f"{x:.6g}")


def market_structure(broker, symbol: str, timeframe: str = "H4", lookback: int = 250,
                     utc_offset: timedelta = timedelta(0)) -> dict:
    spec = broker.symbol_spec(symbol)
    df = bars_with_indicators(broker, symbol, timeframe, lookback)
    d1 = bars_with_indicators(broker, symbol, "D1", 30)
    h1 = bars_with_indicators(broker, symbol, "H1", 48)
    if df.empty or d1.empty:
        return {"error": f"not enough data for {symbol}"}
    bid, ask = broker.quote(symbol)
    atr = float(df["atr14"].iloc[-1])
    sw = swings(df)
    zones = sr_zones(sw, atr)
    for z in zones:
        z["distance_atr"] = round((z["mid"] - bid) / atr, 2)
        z["side"] = "above" if z["low"] > bid else ("below" if z["high"] < bid else "price inside zone")
        z["last_touch"] = z["last_touch"].strftime("%Y-%m-%d %H:%M")
        for k in ("low", "high", "mid"):
            z[k] = _digits(z[k])
    above = sorted([z for z in zones if z["side"] == "above"], key=lambda z: z["mid"])[:3]
    below = sorted([z for z in zones if z["side"] == "below"], key=lambda z: -z["mid"])[:3]

    prev_day = d1.iloc[-1]
    now = broker.now()
    week_start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    prev_week = d1[(d1.index < week_start) & (d1.index >= week_start - timedelta(days=7))]
    adr = float((d1["high"] - d1["low"]).tail(20).mean())
    today = h1[h1.index >= now.replace(hour=0, minute=0, second=0, microsecond=0)]
    today_range = float(today["high"].max() - today["low"].min()) if not today.empty else 0.0
    # Asia session (00:00-07:00 UTC) of today, in server time
    asia_start = now.replace(hour=0, minute=0, second=0, microsecond=0) + utc_offset
    asia = h1[(h1.index >= asia_start) & (h1.index < asia_start + timedelta(hours=7))]

    last_swings = sw.tail(6).copy()
    last_swings["time"] = last_swings["time"].dt.strftime("%Y-%m-%d %H:%M")
    return {
        "symbol": symbol, "timeframe": timeframe, "bid": bid, "atr": _digits(atr),
        "atr_pips": round(atr / spec.pip, 1),
        "trend_structure": trend_structure(sw),
        "last_swings": last_swings.to_dict("records"),
        "nearest_resistance": above, "nearest_support": below,
        "all_zones_by_strength": zones,
        "previous_day": {"high": prev_day["high"], "low": prev_day["low"], "close": prev_day["close"]},
        "previous_week": ({"high": float(prev_week["high"].max()), "low": float(prev_week["low"].min()),
                           "close": float(prev_week["close"].iloc[-1])} if not prev_week.empty else None),
        "adr_pips": round(adr / spec.pip, 1),
        "today_range_pips": round(today_range / spec.pip, 1),
        "adr_used_pct": round(100 * today_range / adr, 1) if adr else None,
        "asia_range": ({"high": float(asia["high"].max()), "low": float(asia["low"].min())}
                       if not asia.empty else None),
    }


def key_levels(ms: dict) -> list[tuple[float, str]]:
    """Flatten a structure report into (price, label) pairs for charts."""
    lv = []
    for z in ms.get("nearest_resistance", []):
        lv.append((z["mid"], f"R x{z['touches']}"))
    for z in ms.get("nearest_support", []):
        lv.append((z["mid"], f"S x{z['touches']}"))
    if ms.get("previous_day"):
        lv += [(ms["previous_day"]["high"], "PDH"), (ms["previous_day"]["low"], "PDL")]
    if ms.get("previous_week"):
        lv += [(ms["previous_week"]["high"], "PWH"), (ms["previous_week"]["low"], "PWL")]
    return lv
