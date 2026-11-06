"""TEMPLATE - copy to strategies/<your_name>.py (files starting with "_" are ignored).

Rules that keep research honest:
* State the hypothesis (WHY it should work) before testing. Prefer ideas from `lab explore` findings.
* Keep the parameter grid small (each combination counts as a trial and raises the bar for everyone).
* Use only columns of `f` (completed-bar information). Forward returns are removed for you.
* A genuinely different idea gets a new `name`; re-running the same name after a rejection is blocked.

Useful columns in f: open high low close, ema20/50/200, rsi14, atr14, adx14, macd_hist, bb_up/bb_mid/bb_low,
h4_close/h4_ema50/h4_ema200/h4_adx14/h4_atr14, d1_* (same), pd_high/pd_low/pd_close (previous day),
asia_high/asia_low (after 07:00), session, regime, hour, dow, ret_1h_atr, ret_4h_atr, ret_24h_atr,
dist_ema200_atr, dist_ema50_atr, streak, range_atr.
"""
from trading_agent.lab import Strategy


class MyIdea(Strategy):
    name = "my-idea"
    description = "One sentence: condition -> expected behaviour -> why"
    param_grid = {"x": [1.0, 2.0]}

    def signals(self, f, x):
        longs = (f["ret_1h_atr"] < -x) & (f["regime"] == "ranging")
        shorts = None
        stop = f["low"] - 0.5 * f["atr14"]
        return self.make(f, longs, shorts, stop, None, rr=1.5)


STRATEGY = MyIdea()
