"""London breakout of the Asia range (classic session-breakout idea).

Hypothesis: the overnight (Asia) range concentrates liquidity; when London breaks it decisively,
price tends to continue in the break direction during the London/NY session."""
from trading_agent.lab import Strategy


class AsiaBreakout(Strategy):
    name = "asia-breakout"
    description = "London (07-11 UTC) closes beyond the Asia range; stop at the range middle"
    param_grid = {"buffer_atr": [0.0, 0.15, 0.3], "rr": [1.5, 2.0, 3.0], "max_range_atr": [2.0, 3.0]}

    def signals(self, f, buffer_atr, rr, max_range_atr):
        london = f["hour"].between(7, 11)
        rng = f["asia_high"] - f["asia_low"]
        ok = london & (rng < max_range_atr * f["atr14"]) & (rng > 0)
        up = ok & (f["close"] > f["asia_high"] + buffer_atr * f["atr14"]) & \
            (f["close"].shift() <= f["asia_high"] + buffer_atr * f["atr14"])
        dn = ok & (f["close"] < f["asia_low"] - buffer_atr * f["atr14"]) & \
            (f["close"].shift() >= f["asia_low"] - buffer_atr * f["atr14"])
        mid = (f["asia_high"] + f["asia_low"]) / 2
        return self.make(f, up, dn, mid, mid, rr=rr)


STRATEGY = AsiaBreakout()
