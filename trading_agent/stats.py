"""Performance statistics, with an honest significance test."""
from __future__ import annotations

import math

import pandas as pd


def _norm_sf(z: float) -> float:
    return 0.5 * math.erfc(z / math.sqrt(2))


def performance(trades: pd.DataFrame, equity: pd.DataFrame | None = None,
                initial_balance: float | None = None) -> dict:
    t = trades.dropna(subset=["profit"])
    n = len(t)
    out: dict = {"trades": n}
    if n == 0:
        return out
    r = t["r_multiple"].dropna()
    wins = t[t["profit"] > 0]["profit"].sum()
    losses = -t[t["profit"] < 0]["profit"].sum()
    out.update({
        "win_rate": round((t["profit"] > 0).mean(), 3),
        "net_profit": round(t["profit"].sum(), 2),
        "profit_factor": round(wins / losses, 3) if losses > 0 else None,
        "avg_r": round(r.mean(), 3) if len(r) else None,
        "avg_win_r": round(r[r > 0].mean(), 3) if (r > 0).any() else None,
        "avg_loss_r": round(r[r <= 0].mean(), 3) if (r <= 0).any() else None,
    })
    if len(r) >= 2 and r.std(ddof=1) > 0:
        tstat = r.mean() / (r.std(ddof=1) / math.sqrt(len(r)))
        out["t_stat_avg_r"] = round(tstat, 2)
        out["p_value_edge"] = round(_norm_sf(tstat), 4)  # one-sided: H0 = no positive edge
    if initial_balance:
        out["return_pct"] = round(100 * t["profit"].sum() / initial_balance, 2)
    if equity is not None and not equity.empty:
        eq = equity["equity"]
        dd = (eq / eq.cummax() - 1).min()
        out["max_drawdown_pct"] = round(100 * dd, 2)
    return out


def verdict(stats: dict) -> str:
    n = stats.get("trades", 0)
    if n < 30:
        return f"Only {n} trades: far too few to conclude anything."
    p = stats.get("p_value_edge")
    if p is None:
        return "Not enough variation to test."
    if p < 0.01 and n >= 200:
        return f"Evidence of a positive edge (p={p}, n={n}). Confirm on fresh out-of-sample data."
    if p < 0.05:
        return f"Weak evidence (p={p}, n={n}). Could be luck/multiple testing; collect more trades."
    return f"No statistically meaningful edge (p={p}, n={n})."


def format_report(name: str, stats: dict, usage: dict | None = None) -> str:
    lines = [f"=== {name} ==="]
    lines += [f"  {k:>18}: {v}" for k, v in stats.items()]
    lines.append(f"  {'verdict':>18}: {verdict(stats)}")
    if usage:
        lines.append(f"  {'api usage':>18}: {usage}")
    return "\n".join(lines)
