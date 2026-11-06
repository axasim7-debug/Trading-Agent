"""Mean reversion after RSI extremes, only when the higher timeframe is not trending.

Hypothesis: in non-trending regimes, H1 RSI extremes are exhaustion and price reverts toward the
20-period mean (Bollinger middle band)."""
from trading_agent.lab import Strategy


class RsiExtremeReversion(Strategy):
    name = "rsi-extreme-reversion"
    description = "Fade H1 RSI extremes when H4 ADX is low; target the middle band, stop beyond the extreme"
    param_grid = {"rsi_lo": [20, 25], "rsi_hi": [75, 80], "max_h4_adx": [20, 25], "stop_atr": [0.5, 1.0]}

    def signals(self, f, rsi_lo, rsi_hi, max_h4_adx, stop_atr):
        calm = f["h4_adx14"] < max_h4_adx
        longs = calm & (f["rsi14"] < rsi_lo) & (f["close"] > f["open"])     # first up bar after the extreme
        shorts = calm & (f["rsi14"] > rsi_hi) & (f["close"] < f["open"])
        long_stop = f["low"].rolling(3).min() - stop_atr * f["atr14"]
        short_stop = f["high"].rolling(3).max() + stop_atr * f["atr14"]
        return self.make(f, longs, shorts, long_stop, short_stop,
                         long_target=f["bb_mid"], short_target=f["bb_mid"])


STRATEGY = RsiExtremeReversion()
