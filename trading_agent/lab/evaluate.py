"""Strict evaluation of a strategy, designed to reject curve-fitted results.

Data split (by time):  [ development ............................ | holdout ]
* development: parameter grid + walk-forward (expanding window, K folds): parameters are chosen on the
  past only and scored on the next fold, so the walk-forward result is genuinely out-of-sample;
* holdout (most recent `holdout_fraction`): untouched until `finalize`, which can run ONCE per strategy.

Gates for a "candidate" (all must pass):
* walk-forward OOS: avg R > 0, at least `min_oos_trades`, t-stat above a hurdle that rises with
  the number of strategies/variants ever tried in the registry (multiple-testing penalty);
* robustness: most parameter sets are profitable (not one lucky corner of the grid);
* cost stress: still profitable with doubled spread and commission;
* stability: profitable in most calendar years.
Then `finalize` on the holdout -> "approved" (eligible for live scanning) or "rejected" for good.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from ..research import simulate_signals
from .features import strategy_view


def _stats(r: pd.Series) -> dict:
    n = len(r)
    if n == 0:
        return {"n": 0, "avg_r": None, "t": None, "win_rate": None, "total_r": 0.0, "profit_factor": None}
    gains, losses = r[r > 0].sum(), -r[r < 0].sum()
    t = float(r.mean() / (r.std(ddof=1) / math.sqrt(n))) if n > 1 and r.std(ddof=1) > 0 else 0.0
    return {"n": int(n), "avg_r": round(float(r.mean()), 4), "t": round(t, 2),
            "win_rate": round(float((r > 0).mean()), 3), "total_r": round(float(r.sum()), 2),
            "profit_factor": round(float(gains / losses), 3) if losses > 0 else None}


def hurdle_t(n_trials: int) -> float:
    """Expected max t-stat of N worthless strategies is about sqrt(2 ln N); demand at least that (min 2)."""
    return round(max(2.0, math.sqrt(2 * math.log(max(n_trials, 1)))), 2)


def max_drawdown_r(r: np.ndarray) -> float:
    eq = np.cumsum(r)
    return float(np.max(np.maximum.accumulate(np.concatenate([[0], eq]))[1:] - eq)) if len(r) else 0.0


def monte_carlo_dd(r: np.ndarray, runs: int = 1000, seed: int = 0) -> dict:
    if len(r) < 10:
        return {}
    rng = np.random.default_rng(seed)
    dds = [max_drawdown_r(rng.choice(r, size=len(r), replace=True)) for _ in range(runs)]
    return {"dd_r_median": round(float(np.median(dds)), 2), "dd_r_95pct": round(float(np.percentile(dds, 95)), 2)}


class Evaluator:
    def __init__(self, data: dict[str, pd.DataFrame], features: dict[str, pd.DataFrame], specs: dict,
                 commission_per_lot: float, max_hold_hours: int = 72, holdout_fraction: float = 0.15,
                 folds: int = 5):
        self.data, self.features, self.specs = data, features, specs
        self.commission = commission_per_lot
        self.max_hold = max_hold_hours
        start = min(f.index[0] for f in features.values())
        end = max(f.index[-1] for f in features.values())
        self.start, self.end = start, end
        self.dev_end = start + (end - start) * (1 - holdout_fraction)
        self.folds = folds

    def trades(self, strategy, params: dict, period: str = "dev", cost_mult: float = 1.0) -> pd.DataFrame:
        out = []
        for sym, f in self.features.items():
            view = strategy_view(f)
            sig = strategy.signals(view, **params)
            if sig is None or len(sig) == 0:
                continue
            sig = sig[(sig.index < self.dev_end) if period == "dev" else (sig.index >= self.dev_end)]
            if sig.empty:
                continue
            sig = sig.assign(setup=strategy.name, session="", regime="")
            spec = self.specs[sym]
            base = self.data[sym]
            if cost_mult != 1.0:
                base = base.assign(spread=np.where(base["spread"] > 0, base["spread"], spec.default_spread_points)
                                   * cost_mult)
            tr = simulate_signals(sig, base, spec, self.commission * cost_mult, self.max_hold)
            if not tr.empty:
                out.append(tr.assign(symbol=sym))
        if not out:
            return pd.DataFrame(columns=["time", "symbol", "r"])
        return pd.concat(out, ignore_index=True).sort_values("time").reset_index(drop=True)

    def evaluate(self, strategy, n_trials: int, min_oos_trades: int = 100) -> dict:
        grid = strategy.param_sets()
        per_param = {i: self.trades(strategy, p) for i, p in enumerate(grid)}
        edges = pd.date_range(self.start, self.dev_end, periods=self.folds + 2)
        oos_parts, chosen = [], []
        for k in range(1, self.folds + 1):
            train_end, test_end = edges[k], edges[k + 1]
            best, best_score = None, -np.inf
            for i, tr in per_param.items():
                past = tr[tr["time"] < train_end]["r"]
                score = past.mean() if len(past) >= 20 else -np.inf
                if score > best_score:
                    best, best_score = i, score
            if best is None or best_score == -np.inf:
                continue
            chosen.append(grid[best])
            tr = per_param[best]
            oos_parts.append(tr[(tr["time"] >= train_end) & (tr["time"] < test_end)])
        wf = pd.concat(oos_parts) if oos_parts else pd.DataFrame(columns=["time", "symbol", "r"])
        wf_stats = _stats(wf["r"])

        full_scores = {i: (tr["r"].mean() if len(tr) >= 20 else -np.inf) for i, tr in per_param.items()}
        final_i = max(full_scores, key=full_scores.get)
        final_params = grid[final_i]
        final_tr = per_param[final_i]
        robust = float(np.mean([s > 0 for s in full_scores.values() if s != -np.inf])) if full_scores else 0.0
        stress = _stats(self.trades(strategy, final_params, cost_mult=2.0)["r"])
        years = final_tr.assign(year=pd.to_datetime(final_tr["time"]).dt.year).groupby("year")["r"].mean() \
            if len(final_tr) else pd.Series(dtype=float)
        by_symbol = {s: _stats(g["r"]) for s, g in final_tr.groupby("symbol")} if len(final_tr) else {}

        hurdle = hurdle_t(n_trials)
        gates = {
            "walk_forward_avg_r>0": bool((wf_stats["avg_r"] or 0) > 0),
            f"walk_forward_trades>={min_oos_trades}": wf_stats["n"] >= min_oos_trades,
            f"walk_forward_t>={hurdle}": bool((wf_stats["t"] or 0) >= hurdle),
            "robust_params>=60%": robust >= 0.6,
            "cost_stress_2x_avg_r>0": bool((stress["avg_r"] or 0) > 0),
            "positive_years>=60%": bool(len(years) and (years > 0).mean() >= 0.6),
        }
        return {
            "strategy": strategy.name, "description": strategy.description,
            "development_period": f"{self.start:%Y-%m-%d} -> {self.dev_end:%Y-%m-%d}",
            "holdout_period": f"{self.dev_end:%Y-%m-%d} -> {self.end:%Y-%m-%d} (untouched)",
            "param_sets_tested": len(grid), "trials_in_registry": n_trials, "t_hurdle": hurdle,
            "walk_forward_oos": wf_stats, "walk_forward_param_choices": chosen,
            "final_params": final_params, "final_params_in_sample": _stats(final_tr["r"]),
            "param_robustness_pct_positive": round(100 * robust, 1),
            "cost_stress_2x": stress,
            "by_year_avg_r": {int(k): round(float(v), 3) for k, v in years.items()},
            "by_symbol": by_symbol,
            "monte_carlo_walk_forward": monte_carlo_dd(wf["r"].to_numpy()),
            "max_drawdown_r_walk_forward": round(max_drawdown_r(wf["r"].to_numpy()), 2),
            "gates": gates, "candidate": all(gates.values()),
        }

    def finalize(self, strategy, params: dict, min_trades: int = 30) -> dict:
        tr = self.trades(strategy, params, period="holdout")
        st = _stats(tr["r"])
        passed = bool(st["n"] >= min_trades and (st["avg_r"] or 0) > 0 and (st["t"] or 0) >= 1.0)
        return {"holdout": st, "holdout_period": f"{self.dev_end:%Y-%m-%d} -> {self.end:%Y-%m-%d}",
                "passed": passed,
                "by_symbol": {s: _stats(g["r"]) for s, g in tr.groupby("symbol")} if len(tr) else {}}
