"""Strategy interface for the research lab, and loading of strategy files from `strategies/`.

A strategy file defines `STRATEGY = MyStrategy()` where MyStrategy subclasses Strategy:

    class AsiaBreakout(Strategy):
        name = "asia-breakout"
        description = "London breaks the Asia range; trade the break with stop at the other side"
        param_grid = {"buffer_atr": [0.0, 0.1, 0.2], "rr": [1.5, 2.0]}

        def signals(self, f, buffer_atr, rr):
            ...
            return self.make(f, long_mask, short_mask, long_stop, short_stop, rr=rr)

`f` is the lab feature frame WITHOUT forward-return columns (no look-ahead possible).
`signals` returns a DataFrame indexed by H1 bar time with columns direction (+1/-1), stop, target.
Rows are evaluated at the bar's close; entries happen at the next bar's open.
"""
from __future__ import annotations

import importlib.util
import itertools
from pathlib import Path

import pandas as pd


class Strategy:
    name: str = "unnamed"
    description: str = ""
    param_grid: dict[str, list] = {}

    def signals(self, f: pd.DataFrame, **params) -> pd.DataFrame:  # pragma: no cover - abstract
        raise NotImplementedError

    def param_sets(self) -> list[dict]:
        if not self.param_grid:
            return [{}]
        keys = list(self.param_grid)
        return [dict(zip(keys, combo)) for combo in itertools.product(*(self.param_grid[k] for k in keys))]

    # helper for the common case: boolean masks + stop series, target from a reward:risk multiple
    @staticmethod
    def make(f: pd.DataFrame, long_mask, short_mask, long_stop, short_stop, rr: float | None = None,
             long_target=None, short_target=None) -> pd.DataFrame:
        parts = []
        for mask, d, stop, tgt in ((long_mask, 1, long_stop, long_target), (short_mask, -1, short_stop, short_target)):
            if mask is None:
                continue
            mask = mask.fillna(False)
            if not mask.any():
                continue
            ref = f.loc[mask, "close"]
            st = stop[mask] if isinstance(stop, pd.Series) else pd.Series(stop, index=ref.index)
            if tgt is not None:
                tg = tgt[mask] if isinstance(tgt, pd.Series) else pd.Series(tgt, index=ref.index)
            else:
                tg = ref + d * rr * (ref - st).abs()
            parts.append(pd.DataFrame({"direction": d, "stop": st, "target": tg}, index=ref.index))
        if not parts:
            return pd.DataFrame(columns=["direction", "stop", "target"])
        out = pd.concat(parts).sort_index()
        risk = (f.loc[out.index, "close"] - out["stop"]) * out["direction"]
        return out[(risk > 0) & ((out["target"] - f.loc[out.index, "close"]) * out["direction"] > 0)]


def load_strategies(folder: str = "strategies") -> dict[str, Strategy]:
    out: dict[str, Strategy] = {}
    for path in sorted(Path(folder).glob("*.py")):
        if path.name.startswith("_"):
            continue
        spec = importlib.util.spec_from_file_location(f"strategies.{path.stem}", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        st = getattr(mod, "STRATEGY", None)
        if isinstance(st, Strategy):
            out[st.name] = st
    return out
