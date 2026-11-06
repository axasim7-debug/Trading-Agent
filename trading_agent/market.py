"""Turns raw bars into compact, numeric text the agent can reason over."""
from __future__ import annotations

from datetime import datetime

import pandas as pd

from .broker.base import Broker
from .indicators import add_indicators

WARMUP = 260  # enough history for EMA200 to settle


def fmt_time(t: datetime, hide_dates: bool) -> str:
    return t.strftime("%a %H:%M") if hide_dates else t.strftime("%Y-%m-%d %H:%M")


def bars_with_indicators(broker: Broker, symbol: str, timeframe: str, count: int) -> pd.DataFrame:
    if getattr(broker, "precomputes_indicators", False):  # simulator: cached, causal
        return broker.get_bars(symbol, timeframe, count, with_indicators=True)
    df = broker.get_bars(symbol, timeframe, count + WARMUP)
    if df.empty:
        return df
    return add_indicators(df).tail(count)


def _digits(point: float) -> int:
    s = f"{point:.10f}".rstrip("0")
    return len(s.split(".")[1]) if "." in s else 0


def trend_label(row: pd.Series) -> str:
    c, e50, e200 = row.get("close"), row.get("ema50"), row.get("ema200")
    if pd.isna(e50) or pd.isna(e200):
        return "n/a"
    if c > e50 > e200:
        return "up"
    if c < e50 < e200:
        return "down"
    return "mixed"


def candles_text(broker: Broker, symbol: str, timeframe: str, count: int,
                 include_indicators: bool, hide_dates: bool) -> str:
    spec = broker.symbol_spec(symbol)
    d = _digits(spec.point)
    df = bars_with_indicators(broker, symbol, timeframe, count)
    if df.empty:
        return f"No completed {timeframe} bars for {symbol} yet."
    cols = ["open", "high", "low", "close"]
    if include_indicators:
        cols += ["ema20", "ema50", "ema200", "rsi14", "atr14", "adx14", "macd_hist", "bb_up", "bb_low"]
    out = df[cols].copy()
    for c in out.columns:
        out[c] = out[c].round(2 if c in ("rsi14", "adx14") else d)
    out.index = [fmt_time(t, hide_dates) for t in out.index]
    out.index.name = "time"
    header = (f"{symbol} {timeframe} - last {len(out)} completed bars (oldest first). "
              f"Prices are BID. pip={spec.pip}. atr14 is in price units.\n")
    return header + out.to_csv()


def market_overview(broker: Broker, symbols: list[str], hide_dates: bool) -> str:
    lines = ["symbol | bid | spread_pips | D1 | H4 | H1 | H1_rsi | H1_adx | H1_atr_pips | D1_atr_pips | "
             "change_24h_pips | last_H1_close_time"]
    for sym in symbols:
        try:
            spec = broker.symbol_spec(sym)
            bid, ask = broker.quote(sym)
            h1 = bars_with_indicators(broker, sym, "H1", 30)
            h4 = bars_with_indicators(broker, sym, "H4", 5)
            d1 = bars_with_indicators(broker, sym, "D1", 5)
            if h1.empty:
                lines.append(f"{sym} | no data")
                continue
            last = h1.iloc[-1]
            chg = (bid - h1["close"].iloc[-25]) / spec.pip if len(h1) >= 25 else float("nan")
            d1_atr = d1["atr14"].iloc[-1] / spec.pip if not d1.empty else float("nan")
            lines.append(" | ".join([
                sym, f"{bid:.{_digits(spec.point)}f}", f"{(ask - bid) / spec.pip:.1f}",
                trend_label(d1.iloc[-1]) if not d1.empty else "n/a",
                trend_label(h4.iloc[-1]) if not h4.empty else "n/a",
                trend_label(last), f"{last['rsi14']:.1f}", f"{last['adx14']:.1f}",
                f"{last['atr14'] / spec.pip:.1f}", f"{d1_atr:.1f}", f"{chg:+.1f}",
                fmt_time(h1.index[-1], hide_dates),
            ]))
        except Exception as e:  # keep the overview useful even if one symbol fails
            lines.append(f"{sym} | error: {e}")
    lines.append("Trend label: up = close>EMA50>EMA200, down = close<EMA50<EMA200, else mixed.")
    return "\n".join(lines)
