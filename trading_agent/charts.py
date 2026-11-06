"""Candlestick chart rendering so the agent can look at the chart the way a human trader does.

Draws completed candles, EMA20/50/200, optional Bollinger bands, key structure levels, open
positions (entry / stop / target), pending orders and active alerts. Returns PNG bytes."""
from __future__ import annotations

import io

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import Rectangle  # noqa: E402

from .market import bars_with_indicators  # noqa: E402


def render_chart(broker, symbol: str, timeframe: str = "H1", count: int = 120, levels=None,
                 positions=None, orders=None, alerts=None, bollinger: bool = False) -> bytes:
    df = bars_with_indicators(broker, symbol, timeframe, count)
    if df.empty:
        raise ValueError(f"no bars for {symbol} {timeframe}")
    fig, (ax, axr) = plt.subplots(2, 1, figsize=(13, 7.5), dpi=100, sharex=True,
                                  gridspec_kw={"height_ratios": [4, 1]})
    x = range(len(df))
    w = 0.6
    for i, (_, r) in zip(x, df.iterrows()):
        up = r["close"] >= r["open"]
        color = "#1a9850" if up else "#d73027"
        ax.vlines(i, r["low"], r["high"], color=color, linewidth=0.8)
        lo, hi = sorted((r["open"], r["close"]))
        ax.add_patch(Rectangle((i - w / 2, lo), w, max(hi - lo, 1e-9), color=color))
    for col, c in (("ema20", "#2c7fb8"), ("ema50", "#fdae61"), ("ema200", "#7b3294")):
        if col in df:
            ax.plot(list(x), df[col].values, color=c, linewidth=1.1, label=col.upper())
    if bollinger and "bb_up" in df:
        ax.plot(list(x), df["bb_up"].values, color="grey", linewidth=0.7, linestyle="--")
        ax.plot(list(x), df["bb_low"].values, color="grey", linewidth=0.7, linestyle="--")

    ymin, ymax = df["low"].min(), df["high"].max()
    pad = (ymax - ymin) * 0.08

    lo_lim, hi_lim = ymin - pad, ymax + pad

    def hline(price, label, color, style="-"):
        nonlocal lo_lim, hi_lim
        if ymin - 3 * pad <= price <= ymax + 3 * pad:   # nearby levels widen the view; far ones are skipped
            lo_lim, hi_lim = min(lo_lim, price - pad / 2), max(hi_lim, price + pad / 2)
            ax.axhline(price, color=color, linestyle=style, linewidth=1)
            ax.text(len(df) + 0.5, price, f" {label} {price:.5g}", color=color, va="center", fontsize=8,
                    clip_on=True)

    for price, label in levels or []:
        hline(price, label, "#636363", ":")
    for p in positions or []:
        hline(p.entry_price, f"#{p.ticket} {p.direction.value} entry", "#08519c")
        if p.stop_loss:
            hline(p.stop_loss, f"#{p.ticket} SL", "#cb181d", "--")
        if p.take_profit:
            hline(p.take_profit, f"#{p.ticket} TP", "#238b45", "--")
    for o in orders or []:
        hline(o.price, f"#{o.ticket} {o.direction.value} {o.order_type}", "#6a51a3", "-.")
    for a in alerts or []:
        hline(a["price"], f"alert {a['condition']}", "#e6550d", "-.")

    ax.set_ylim(lo_lim, hi_lim)
    ax.set_xlim(-1, len(df) + 8)
    ax.legend(loc="upper left", fontsize=8)
    ax.set_title(f"{symbol} {timeframe} - last {len(df)} completed bars (oldest left)")
    ax.grid(alpha=0.2)
    if "rsi14" in df:
        axr.plot(list(x), df["rsi14"].values, color="#3182bd", linewidth=1)
        for lvl in (30, 50, 70):
            axr.axhline(lvl, color="grey", linewidth=0.6, linestyle=":")
        axr.set_ylim(0, 100)
        axr.set_ylabel("RSI14")
    step = max(1, len(df) // 10)
    ticks = list(range(0, len(df), step))
    axr.set_xticks(ticks)
    axr.set_xticklabels([df.index[i].strftime("%m-%d %H:%M") for i in ticks], rotation=30, fontsize=7)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    return buf.getvalue()
