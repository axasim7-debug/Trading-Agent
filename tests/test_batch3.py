import asyncio

import pandas as pd
import pytest

from trading_agent.agent.tools import ToolExecutor
from trading_agent.broker import SimBroker
from trading_agent.config import Config, RiskConfig, WatchdogConfig
from trading_agent.data import synthetic_bars
from trading_agent.diagnostics import Sync, calibration_report
from trading_agent.journal import Journal
from trading_agent.models import Direction
from trading_agent.notifier import RecordingNotifier
from trading_agent.risk import RiskManager
from trading_agent.structure import market_structure, swings
from trading_agent.watchdog import Watchdog


def bars(rows, start="2024-01-01 00:00"):
    idx = pd.date_range(start, periods=len(rows), freq="15min")
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx)
    df["tick_volume"], df["spread"] = 1.0, 10.0
    return df


def make_desk(df, tmp_path, start_i=4, **risk_kw):
    cfg = Config(watchlist=["EURUSD"])
    b = SimBroker({"EURUSD": df}, {"EURUSD": cfg.spec("EURUSD")}, "M15", 10_000, 0, start=df.index[start_i])
    j = Journal(str(tmp_path / "j.sqlite"))
    run = j.get_or_create_run("sim", "t", "t")
    rm = RiskManager(RiskConfig(**risk_kw), b, journal=j, run_id=run)
    note = RecordingNotifier()
    ex = ToolExecutor(b, rm, j, run, ["EURUSD"], hide_dates=False, notifier=note,
                      default_management={"break_even_at_r": 1.0})
    return b, j, run, rm, ex, note


def test_pending_limit_fills_and_is_journaled(tmp_path):
    flat = [(1.1000, 1.1003, 1.0997, 1.1000)] * 5
    dip = [(1.1000, 1.1001, 1.0975, 1.0985), (1.0985, 1.0990, 1.0982, 1.0988)]
    b, j, run, rm, ex, note = make_desk(bars(flat + dip), tmp_path)
    text, err = ex.execute("place_pending_order", {
        "symbol": "EURUSD", "direction": "buy", "entry_type": "limit", "entry_price": 1.0980,
        "stop_loss": 1.0960, "take_profit": 1.1030, "risk_percent": 0.5, "setup": "trend-pullback",
        "reasoning": "buy the dip at support", "win_probability": 0.55})
    assert not err, text
    _, err = ex.execute("place_pending_order", {   # a buy limit above price is invalid
        "symbol": "EURUSD", "direction": "buy", "entry_type": "limit", "entry_price": 1.1050,
        "stop_loss": 1.1000, "risk_percent": 0.5, "setup": "x", "reasoning": "x", "win_probability": 0.5})
    assert err
    assert rm.open_risk() > 0                     # pending risk counts
    b.step(); b.step()                            # flat bar, then the dip bar fills the limit
    out = Sync(b, j, run, note).run()
    assert out["filled"]
    p = b.positions()[0]
    assert p.entry_price == pytest.approx(1.0980)
    t = j.trades(run)
    assert t.iloc[0]["entry_type"] == "limit" and t.iloc[0]["win_probability"] == 0.55
    assert any("أمر معلّق" in m for m in note.messages)


def test_drawdown_cuts_risk_and_weekly_limit(tmp_path):
    df = synthetic_bars("2024-01-01", 5, "M15", seed=1)
    b, j, run, rm, ex, _ = make_desk(df, tmp_path, start_i=200, max_weekly_loss_pct=4.0)
    rm.period_state()                 # records peak / week start at 10,000
    b.balance -= 600                  # -6%: first drawdown tier (5% -> x0.5)
    ps = rm.period_state()
    assert ps["risk_multiplier"] == 0.5 and ps["effective_max_risk_per_trade_pct"] == 0.5
    _, ask = b.quote("EURUSD")
    plan = rm.plan_order("EURUSD", Direction.BUY, ask - 0.002, None, 1.0)
    assert any("drawdown" in r for r in plan.reasons)
    assert "weekly loss" in (rm.block_reason() or "") or "daily loss" in (rm.block_reason() or "")


def test_structure_finds_swings_and_levels():
    df = synthetic_bars("2024-01-01", 200, "M15", seed=4)
    cfg = Config(watchlist=["EURUSD"])
    b = SimBroker({"EURUSD": df}, {"EURUSD": cfg.spec("EURUSD")}, "M15", 10_000, 0, start="2024-05-01")
    ms = market_structure(b, "EURUSD", "H4")
    assert ms["nearest_support"] and ms["nearest_resistance"]
    assert all(z["mid"] < ms["bid"] for z in ms["nearest_support"])
    assert ms["previous_day"]["high"] >= ms["previous_day"]["low"]
    sw = swings(b.get_bars("EURUSD", "H4", 100))
    assert set(sw.kind) == {"high", "low"}


def test_shadow_book_and_calibration(tmp_path):
    rows = [(1.1000, 1.1003, 1.0997, 1.1000)] * 8 + [(1.1000, 1.1060, 1.0999, 1.1055)] * 8
    b, j, run, rm, ex, _ = make_desk(bars(rows), tmp_path, start_i=4)
    sig_time = b.now() - pd.Timedelta(hours=1)
    j.record_signal(run, {"bar_time": sig_time.to_pydatetime() if hasattr(sig_time, "to_pydatetime") else sig_time,
                          "symbol": "EURUSD", "setup": "trend-pullback", "dir": 1, "ref_close": 1.1,
                          "suggested_stop": 1.0980, "suggested_target": 1.1040, "session": "asia",
                          "regime": "trending"}, validated=False)
    for _ in range(8):
        b.step()
    assert Sync(b, j, run).evaluate_signals() == 1
    assert j.signals(run)[0]["outcome_r"] > 1.5          # target hit
    trades = pd.DataFrame({"profit": [10, -5, 8, -4], "win_probability": [0.7, 0.6, 0.65, 0.4],
                           "r_multiple": [1, -1, 1, -1]})
    rep = calibration_report(trades)
    assert rep["n"] == 4 and 0 <= rep["brier_score"] <= 1


def test_watchdog_notifies_management(tmp_path):
    flat = [(1.1000, 1.1002, 1.0998, 1.1000)] * 5
    rally = [(1.1000, 1.1030, 1.0999, 1.1025), (1.1025, 1.1030, 1.1020, 1.1028), (1.1028, 1.1029, 1.1027, 1.1028)]
    b, j, run, rm, ex, note = make_desk(bars(flat + rally), tmp_path)
    ex.execute("open_trade", {"symbol": "EURUSD", "direction": "buy", "stop_loss": 1.0985, "risk_percent": 0.5,
                              "setup": "t", "reasoning": "t", "win_probability": 0.6})
    wd = Watchdog(b, j, run, rm, WatchdogConfig(), notifier=note)
    b.step(); b.step()
    wd.tick()
    assert any("break-even" in m for m in note.messages)


def test_view_chart_returns_image(tmp_path):
    pytest.importorskip("mcp.server.mcpserver")
    from trading_agent.mcp_server import create_server
    df = synthetic_bars("2024-01-01", 60, "M15", seed=2)
    cfg = Config(watchlist=["EURUSD"])
    b = SimBroker({"EURUSD": df}, {"EURUSD": cfg.spec("EURUSD")}, "M15", 10_000, 0, start="2024-02-20")
    j = Journal(str(tmp_path / "j.sqlite"))
    run = j.get_or_create_run("sim", "t", "t")
    ex = ToolExecutor(b, RiskManager(RiskConfig(), b, journal=j, run_id=run), j, run, ["EURUSD"], hide_dates=False)
    srv = create_server(ex)

    async def go():
        r = await srv.call_tool("view_chart", {"symbol": "EURUSD", "timeframe": "H1", "count": 80})
        assert r.content[0].type == "image"
        names = {t.name for t in await srv.list_tools()}
        assert {"place_pending_order", "get_market_structure", "get_intermarket", "get_positioning",
                "get_performance_diagnostics", "view_chart"} <= names
    asyncio.run(go())
