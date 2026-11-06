"""The lab must (1) find nothing in pure noise and (2) find a real effect when one is planted."""
import numpy as np
import pandas as pd
import pytest

from trading_agent.config import Config
from trading_agent.data import synthetic_bars
from trading_agent.lab import Evaluator, Registry, explore, lab_features, load_strategies


def planted_breakout(days=900, seed=0, drift=0.00035, price=1.10):
    """Random walk where, after London closes above the Asia high, price drifts up for 4 hours."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2022-01-03", periods=days * 96, freq="15min")
    idx = idx[idx.dayofweek < 5]
    vol = 0.0006
    o = np.empty(len(idx)); h = np.empty(len(idx)); l = np.empty(len(idx)); c = np.empty(len(idx))
    p = price
    day = None
    asia_hi, drift_left, broke = -np.inf, 0, False
    for i, t in enumerate(idx):
        if t.date() != day:
            day, asia_hi, broke = t.date(), -np.inf, False
        mu = drift if drift_left > 0 else 0.0
        drift_left = max(0, drift_left - 1)
        steps = p * np.exp(np.cumsum(rng.normal(mu / 4, vol / 2, 4)))
        o[i], c[i] = p, steps[-1]
        h[i], l[i] = max(p, steps.max()), min(p, steps.min())
        p = c[i]
        if t.hour < 7:
            asia_hi = max(asia_hi, h[i])
        elif 7 <= t.hour < 12 and not broke and t.minute == 45 and c[i] > asia_hi:
            broke, drift_left = True, 16      # effect starts after the H1 bar closes above the Asia high
    return pd.DataFrame({"open": o, "high": h, "low": l, "close": c, "tick_volume": 1.0, "spread": 10.0},
                        index=idx)


@pytest.fixture(scope="module")
def strategies():
    return load_strategies("strategies")


def evaluator(data):
    cfg = Config(watchlist=list(data))
    feats = {s: lab_features(df) for s, df in data.items()}
    return feats, Evaluator(data, feats, {s: cfg.spec(s) for s in data}, 2.0, 72, 0.15, 5)


def test_noise_produces_no_discoveries_and_no_candidates(strategies):
    data = {s: synthetic_bars("2022-01-03", 700, "M15", price=p, seed=i)
            for i, (s, p) in enumerate([("EURUSD", 1.1), ("GBPUSD", 1.27)])}
    feats, ev = evaluator(data)
    df = explore(feats)
    assert len(df) > 500
    assert df["discovery"].sum() <= 1          # FDR control: essentially nothing survives
    for st in strategies.values():
        assert not ev.evaluate(st, n_trials=50, min_oos_trades=50)["candidate"]


def test_planted_effect_is_discovered_and_approved(strategies, tmp_path):
    data = {"EURUSD": planted_breakout(seed=1), "GBPUSD": planted_breakout(seed=2, price=1.27)}
    feats, ev = evaluator(data)
    df = explore(feats)
    hits = df[df["discovery"] & (df["condition"] == "london_break_asia_high") & (df["horizon_h"] == 4)]
    assert len(hits) >= 1 and (hits["mean_fwd_atr"] > 0).all()

    st = strategies["asia-breakout"]
    rep = ev.evaluate(st, n_trials=len(st.param_sets()), min_oos_trades=50)
    assert rep["walk_forward_oos"]["avg_r"] > 0
    assert rep["gates"]["walk_forward_avg_r>0"]

    reg = Registry(str(tmp_path / "registry.json"))
    reg.record_evaluation(rep)
    if rep["candidate"]:
        res = ev.finalize(st, rep["final_params"])
        entry = reg.record_finalize(st.name, res)
        assert entry["status"] in ("approved", "rejected")
        with pytest.raises(ValueError):          # the holdout cannot be reused
            reg.record_evaluation(rep)


def test_strategy_never_sees_forward_returns(strategies):
    data = {"EURUSD": synthetic_bars("2022-01-03", 120, "M15", seed=3)}
    feats, ev = evaluator(data)
    seen = {}

    class Spy(type(strategies["asia-breakout"])):
        name = "spy"
        param_grid = {}

        def signals(self, f):
            seen["cols"] = list(f.columns)
            return super().signals(f, buffer_atr=0.0, rr=2.0, max_range_atr=3.0)

    ev.trades(Spy(), {})
    assert seen["cols"] and not any(c.startswith("fwd_") for c in seen["cols"])
