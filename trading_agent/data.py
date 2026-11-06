"""Historical data: load MT5 exports / generic CSVs, download from MT5, or generate synthetic test data."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from .models import TIMEFRAMES


def load_csv(path: str | Path) -> pd.DataFrame:
    """Accepts the MT5 'Export bars' format (<DATE>\\t<TIME>\\t<OPEN>...) or a CSV with
    columns time, open, high, low, close[, tick_volume, spread]."""
    path = Path(path)
    head = path.read_text(encoding="utf-8", errors="ignore")[:200]
    if "<DATE>" in head:
        df = pd.read_csv(path, sep="\t")
        df.columns = [c.strip("<>").lower() for c in df.columns]
        df["time"] = pd.to_datetime(df["date"] + " " + df.get("time", "00:00:00"), format="mixed")
        df = df.rename(columns={"tickvol": "tick_volume"})
    else:
        df = pd.read_csv(path)
        df.columns = [c.strip().lower() for c in df.columns]
        tcol = next(c for c in ("time", "datetime", "date", "timestamp") if c in df.columns)
        df["time"] = pd.to_datetime(df[tcol])
    df = df.set_index("time").sort_index()
    df.index = df.index.tz_localize(None) if df.index.tz is not None else df.index
    for c in ("tick_volume", "spread"):
        if c not in df:
            df[c] = 0
    df = df[["open", "high", "low", "close", "tick_volume", "spread"]].astype(float)
    return df[~df.index.duplicated(keep="last")]


def data_path(data_dir: str, symbol: str, timeframe: str) -> Path:
    return Path(data_dir) / f"{symbol}_{timeframe}.csv"


def load_symbols(data_dir: str, symbols: list[str], timeframe: str) -> dict[str, pd.DataFrame]:
    out = {}
    for s in symbols:
        p = data_path(data_dir, s, timeframe)
        if not p.exists():
            raise FileNotFoundError(
                f"Missing {p}. On the MT5 machine `python -m trading_agent research` fetches it automatically, "
                f"or export bars from MT5 (View > Symbols > Bars > Export).")
        out[s] = load_csv(p)
    return out


def mt5_available() -> bool:
    try:
        import MetaTrader5  # noqa: F401  type: ignore
        return True
    except ImportError:
        return False


def update_history(cfg, timeframe: str, bars: int) -> list[Path]:  # pragma: no cover - needs MT5
    """Fetch history from MT5 and MERGE it into the local cache (keeps older bars already saved).
    Note: how far back MT5 can go depends on the terminal setting Tools > Options > Charts >
    'Max bars in chart' - set it to Unlimited for multi-year research."""
    from .broker.mt5 import MT5Broker
    broker = MT5Broker(cfg)
    Path(cfg.backtest.data_dir).mkdir(parents=True, exist_ok=True)
    written = []
    try:
        for s in cfg.watchlist:
            new = broker.get_bars(s, timeframe, bars)
            p = data_path(cfg.backtest.data_dir, s, timeframe)
            if p.exists():
                old = load_csv(p)
                new = pd.concat([old, new])
                new = new[~new.index.duplicated(keep="last")].sort_index()
            new.reset_index(names="time").to_csv(p, index=False)
            written.append(p)
    finally:
        broker.shutdown()
    return written


def download_from_mt5(cfg, timeframe: str, bars: int) -> list[Path]:  # pragma: no cover - needs MT5
    from .broker.mt5 import MT5Broker
    broker = MT5Broker(cfg)
    Path(cfg.backtest.data_dir).mkdir(parents=True, exist_ok=True)
    written = []
    try:
        for s in cfg.watchlist:
            df = broker.get_bars(s, timeframe, bars)
            p = data_path(cfg.backtest.data_dir, s, timeframe)
            df.reset_index().to_csv(p, index=False)
            written.append(p)
    finally:
        broker.shutdown()
    return written


def synthetic_bars(start: str, days: int, timeframe: str = "M15", price: float = 1.10,
                   daily_vol: float = 0.006, spread_points: float = 12, seed: int = 0) -> pd.DataFrame:
    """Pure random-walk bars (weekdays only). Useful to verify that no strategy 'wins' on noise."""
    rng = np.random.default_rng(seed)
    td = TIMEFRAMES[timeframe]
    idx = pd.date_range(start, periods=int(days * pd.Timedelta("1D") / td), freq=td)
    idx = idx[idx.dayofweek < 5]
    per_bar = daily_vol / np.sqrt(pd.Timedelta("1D") / td)
    sub = 4  # intra-bar path points for realistic highs/lows
    rets = rng.normal(0, per_bar / np.sqrt(sub), size=(len(idx), sub))
    path = price * np.exp(np.cumsum(rets.ravel())).reshape(len(idx), sub)
    opens = np.concatenate([[price], path[:-1, -1]])
    highs = np.maximum(opens, path.max(axis=1))
    lows = np.minimum(opens, path.min(axis=1))
    return pd.DataFrame({"open": opens, "high": highs, "low": lows, "close": path[:, -1],
                         "tick_volume": 100.0, "spread": float(spread_points)}, index=idx)
