from datetime import timedelta

import pandas as pd
import pytest

from trading_agent import management as mg
from trading_agent.agent.tools import ToolExecutor
from trading_agent.broker import SimBroker
from trading_agent.config import Config, RiskConfig, WatchdogConfig
from trading_agent.data import synthetic_bars
from trading_agent.indicators import add_indicators
from trading_agent.journal import Journal
from trading_agent.models import Direction
from trading_agent.playbook import build_features, find_signals
from trading_agent.research import edge_table, resample, simulate_signals
from trading_agent.risk import RiskManager
from trading_agent.watchdog import Watchdog


def bars(rows, start="2024-01-01 00:00"):
    idx = pd.date_range(start, periods=len(rows), freq="15min")
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx)
    df["tick_volume"], df["spread"] = 1.0, 0.0
    return df


def desk(df, tmp_path):
    cfg = Config(watchlist=["EURUSD"])
    b = SimBroker({"EURUSD": df}, {"EURUSD": cfg.spec("EURUSD")}, "M15", 10_000, 0, start=df.index[4])
    j = Journal(str(tmp_path / "j.sqlite"))
    run = j.get_or_create_run("sim", "t", "t")
    rm = RiskManager(RiskConfig(), b, journal=j, run_id=run)
    ex = ToolExecutor(b, rm, j, run, ["EURUSD"], hide_dates=False,
                      default_management={"break_even_at_r": 1.0})
    return b, j, run, rm, ex


def test_break_even_and_partial(tmp_path):
    flat = [(1.1000, 1.1002, 1.0998, 1.1000)] * 5
    rally = [(1.1000, 1.1025, 1.0999, 1.1022), (1.1022, 1.1030, 1.1020, 1.1028), (1.1028, 1.1029, 1.1027, 1.1028)]
    b, j, run, rm, ex = desk(bars(flat + rally), tmp_path)
    text, err = ex.execute("open_trade", {"symbol": "EURUSD", "direction": "buy", "stop_loss": 1.0985,
                                          "risk_percent": 1.0, "setup": "t", "reasoning": "t"})
    assert not err, text
    ticket, vol0 = b.positions()[0].ticket, b.positions()[0].volume
    ex.execute("set_trade_management", {"ticket": ticket, "partial_at_r": 1.0, "partial_fraction": 0.5})
    wd = Watchdog(b, j, run, rm, WatchdogConfig(), wake=None)
    b.step(); b.step()            # rally bar plays out; now at the next bar's open (1.1022 = +1.1R)
    out = wd.tick()
    p = b.positions()[0]
    assert any("partial" in a for a in out["actions"])
    assert p.volume == pytest.approx(vol0 - int(vol0 * 0.5 / 0.01 + 1e-9) * 0.01)
    assert p.stop_loss > 1.1000   # moved to break-even (+lock)


def test_alert_triggers_and_wakes(tmp_path):
    rows = [(1.1000, 1.1002, 1.0998, 1.1000)] * 5 + [(1.1000, 1.1060, 1.0999, 1.1055), (1.1055, 1.1060, 1.1050, 1.1058)]
    b, j, run, rm, ex = desk(bars(rows), tmp_path)
    text, err = ex.execute("set_alert", {"symbol": "EURUSD", "condition": "above", "price": 1.1050,
                                         "note": "check breakout"})
    assert not err, text
    woken = []
    wd = Watchdog(b, j, run, rm, WatchdogConfig(max_wakes_per_hour=1), wake=lambda c: woken.append(c) or True)
    assert wd.tick()["alerts"] == []
    b.step(); b.step()            # simulator quotes the bar open: 1.1055 now
    fired = wd.tick()["alerts"]
    assert fired and "check breakout" in woken[0]
    assert j.alerts(run, "triggered")
    _, err = ex.execute("set_alert", {"symbol": "EURUSD", "condition": "below", "price": 1.2, "note": "x"})
    assert err   # would trigger immediately


def test_playbook_has_no_lookahead():
    base = synthetic_bars("2024-01-01", 200, "M15", seed=5)
    h1, h4, d1 = (add_indicators(resample(base, r)) for r in ("1h", "4h", "1D"))
    full = find_signals(h1, h4, d1)
    cut = pd.Timestamp("2024-05-01")
    part = find_signals(*(add_indicators(resample(base[base.index < cut], r)) for r in ("1h", "4h", "1D")))
    # signals before the cut must be identical whether or not future data exists
    a = full[full.index < cut - pd.Timedelta(hours=1)][["setup", "direction", "stop"]]
    b = part[part.index < cut - pd.Timedelta(hours=1)][["setup", "direction", "stop"]]
    pd.testing.assert_frame_equal(a, b, check_exact=False, rtol=1e-6)
    # higher-timeframe values come only from bars closed before the decision
    f = build_features(h1, h4, d1)
    row_time = f.index[500]
    h4_used = h4[h4.index + pd.Timedelta(hours=4) <= row_time + pd.Timedelta(hours=1)].iloc[-1]
    assert f.loc[row_time, "h4_close"] == h4_used["close"]


def test_research_on_noise_finds_no_validated_edge():
    cfg = Config(watchlist=["EURUSD"])
    base = synthetic_bars("2024-01-01", 500, "M15", seed=9)
    h1, h4, d1 = (add_indicators(resample(base, r)) for r in ("1h", "4h", "1D"))
    tr = simulate_signals(find_signals(h1, h4, d1), base, cfg.spec("EURUSD"), 7.0, 72).assign(symbol="EURUSD")
    assert len(tr) > 100
    assert tr["r"].min() >= -1.6           # stops respected (gaps aside)
    table = edge_table(tr, base.index[0] + (base.index[-1] - base.index[0]) * 0.7, 30, 15)
    top = [r for r in table if r["level"] == "setup"]
    assert not any(r["validated"] for r in top)
