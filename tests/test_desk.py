import asyncio
from datetime import datetime, timedelta

import pytest

from trading_agent.broker import SimBroker
from trading_agent.config import Config, RiskConfig
from trading_agent.data import synthetic_bars
from trading_agent.econ_calendar import EconomicCalendar, Event
from trading_agent.journal import Journal
from trading_agent.models import Direction
from trading_agent.risk import RiskManager


def broker():
    data = {"EURUSD": synthetic_bars("2024-01-01", 5, "M15", price=1.1, seed=1),
            "GBPUSD": synthetic_bars("2024-01-01", 5, "M15", price=1.27, seed=2)}
    cfg = Config(watchlist=list(data))
    return SimBroker(data, {s: cfg.spec(s) for s in data}, "M15", 10_000, 0, start="2024-01-03 10:00")


class FakeCalendar(EconomicCalendar):
    def __init__(self, events):
        self._events = events

    def events(self):
        return self._events


def test_currency_exposure_blocks_stacked_usd_shorts():
    b = broker()
    rm = RiskManager(RiskConfig(max_currency_exposure_pct=1.5, max_total_open_risk_pct=5), b)
    _, ask = b.quote("EURUSD")
    p1 = rm.plan_order("EURUSD", Direction.BUY, ask - 0.002, None, 1.0)
    assert p1.ok
    b.open_position("EURUSD", Direction.BUY, p1.volume, ask - 0.002, None)
    _, ask2 = b.quote("GBPUSD")
    p2 = rm.plan_order("GBPUSD", Direction.BUY, ask2 - 0.002, None, 1.0)   # second USD short
    assert not p2.ok and any("USD exposure" in r for r in p2.reasons)
    p3 = rm.plan_order("GBPUSD", Direction.SELL, b.quote("GBPUSD")[0] + 0.002, None, 1.0)  # offsets USD
    assert not any("exposure" in r for r in p3.reasons)


def test_news_blackout():
    b = broker()
    now = b.now()
    ev = Event(now + timedelta(minutes=10), "USD", "High", "Non-Farm Payrolls", "", "")
    rm = RiskManager(RiskConfig(), b, calendar=FakeCalendar([ev]))
    _, ask = b.quote("EURUSD")
    plan = rm.plan_order("EURUSD", Direction.BUY, ask - 0.002, None, 0.5)
    assert not plan.ok and any("news blackout" in r for r in plan.reasons)
    far = Event(now + timedelta(hours=5), "USD", "High", "CPI", "", "")
    rm2 = RiskManager(RiskConfig(), b, calendar=FakeCalendar([far]))
    assert rm2.plan_order("EURUSD", Direction.BUY, ask - 0.002, None, 0.5).ok


def test_daily_state_survives_restart(tmp_path):
    b = broker()
    j = Journal(str(tmp_path / "j.sqlite"))
    run = j.get_or_create_run("live", "claude-code", "acct")
    RiskManager(RiskConfig(), b, journal=j, run_id=run)
    b.balance -= 500                       # equity drops after the day started
    rm = RiskManager(RiskConfig(), b, journal=j, run_id=run)   # new process, same day
    assert rm.day_start_equity == 10_000
    assert "daily loss" in rm.block_reason()
    assert j.get_or_create_run("live", "claude-code", "acct") == run


def test_mcp_server_exposes_tools(tmp_path, monkeypatch):
    pytest.importorskip("mcp.server.mcpserver")
    from trading_agent.agent.tools import ToolExecutor
    from trading_agent.mcp_server import create_server

    b = broker()
    j = Journal(str(tmp_path / "j.sqlite"))
    run = j.get_or_create_run("sim", "claude-code", "t")
    desk = ToolExecutor(b, RiskManager(RiskConfig(), b, journal=j, run_id=run), j, run,
                        ["EURUSD", "GBPUSD"], hide_dates=False)
    srv = create_server(desk)

    async def go():
        names = {t.name for t in await srv.list_tools()}
        assert {"open_trade", "get_economic_calendar", "save_plan", "log_cycle"} <= names
        r = await srv.call_tool("open_trade", {"symbol": "XXXYYY", "direction": "buy", "stop_loss": 1.0,
                                               "risk_percent": 0.5, "setup": "s", "reasoning": "r"})
        assert r.content[0].text.startswith("ERROR")
        r = await srv.call_tool("save_plan", {"scope": "daily", "plan": "خطة"})
        assert "saved" in r.content[0].text
        r = await srv.call_tool("get_plan", {})
        assert "خطة" in r.content[0].text

    asyncio.run(go())
