from datetime import datetime
from types import SimpleNamespace

import pandas as pd
import pytest

from trading_agent.agent.claude_agent import ClaudeAgent
from trading_agent.agent.tools import ToolExecutor, decision_due
from trading_agent.broker import SimBroker
from trading_agent.config import AgentConfig, Config, RiskConfig
from trading_agent.data import synthetic_bars
from trading_agent.journal import Journal
from trading_agent.models import Direction
from trading_agent.risk import RiskManager
from trading_agent.runner import Session


def make_bars(rows, start="2024-01-01 00:00", freq="15min", spread=10):
    idx = pd.date_range(start, periods=len(rows), freq=freq)
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx)
    df["tick_volume"] = 1.0
    df["spread"] = float(spread)
    return df


def sim(df, symbol="EURUSD", **kw):
    cfg = Config(watchlist=[symbol])
    return SimBroker({symbol: df}, {symbol: cfg.spec(symbol)}, "M15", 10_000, commission_per_lot=0, **kw)


def test_get_bars_never_returns_unfinished_bars():
    df = synthetic_bars("2024-01-01", 10, "M15", seed=1)
    b = sim(df, start="2024-01-03 10:00")
    now = pd.Timestamp(b.now())
    m15 = b.get_bars("EURUSD", "M15", 5)
    assert m15.index[-1] + pd.Timedelta("15min") <= now
    h4 = b.get_bars("EURUSD", "H4", 5)
    assert h4.index[-1] + pd.Timedelta("4h") <= now
    d1 = b.get_bars("EURUSD", "D1", 5)
    assert d1.index[-1] + pd.Timedelta("1D") <= now
    # the H4 bar built from data must equal the M15 bars it covers (no future data inside)
    last = h4.index[-1]
    part = df.loc[last: last + pd.Timedelta("4h") - pd.Timedelta("1min")]
    assert h4.iloc[-1]["high"] == part["high"].max()


def test_buy_stop_loss_and_take_profit_fills():
    rows = [(1.1000, 1.1005, 1.0995, 1.1000)] * 4 + [(1.1000, 1.1010, 1.0950, 1.0960)]
    b = sim(make_bars(rows), start="2024-01-01 01:00")
    res = b.open_position("EURUSD", Direction.BUY, 1.0, stop_loss=1.0970, take_profit=1.1100)
    assert res.ok and res.price == pytest.approx(1.1000 + 10 * 0.00001)
    b.step()
    ct = b.closed_trade(res.ticket)
    assert ct.close_reason == "sl" and ct.exit_price == 1.0970
    assert ct.profit == pytest.approx((1.0970 - 1.1001) * 100_000)


def test_bar_touching_both_levels_counts_as_stop():
    rows = [(1.1, 1.1, 1.1, 1.1)] * 4 + [(1.1000, 1.1100, 1.0900, 1.1000)]
    b = sim(make_bars(rows), start="2024-01-01 01:00")
    res = b.open_position("EURUSD", Direction.SELL, 1.0, stop_loss=1.1050, take_profit=1.0950)
    b.step()
    assert b.closed_trade(res.ticket).close_reason == "sl"


def risk_setup(**risk_kw):
    df = synthetic_bars("2024-01-01", 5, "M15", seed=2)
    b = sim(df, start="2024-01-03 00:00")
    return b, RiskManager(RiskConfig(**risk_kw), b, commission_per_lot=0)


def test_position_size_matches_risk():
    b, rm = risk_setup()
    bid, ask = b.quote("EURUSD")
    plan = rm.plan_order("EURUSD", Direction.BUY, stop_loss=ask - 0.0020, take_profit=ask + 0.0040, risk_percent=1.0)
    assert plan.ok, plan.reasons
    assert plan.volume == pytest.approx(0.5)  # $100 risk / (20 pips * $10 per pip per lot)
    assert plan.risk_amount <= 100 + 1e-6


def test_risk_rejections():
    b, rm = risk_setup(max_risk_per_trade_pct=1.0, min_reward_risk=1.5)
    bid, ask = b.quote("EURUSD")
    assert not rm.plan_order("EURUSD", Direction.BUY, ask - 0.002, None, 2.0).ok          # too much risk
    assert not rm.plan_order("EURUSD", Direction.BUY, ask + 0.002, None, 1.0).ok          # stop wrong side
    assert not rm.plan_order("EURUSD", Direction.BUY, ask - 0.002, ask + 0.002, 1.0).ok   # RR 1.0 < 1.5
    assert not rm.plan_order("EURUSD", Direction.BUY, ask - 0.00003, None, 1.0).ok        # stop inside spread


def test_stop_can_only_be_tightened():
    b, rm = risk_setup()
    bid, ask = b.quote("EURUSD")
    res = b.open_position("EURUSD", Direction.BUY, 0.1, ask - 0.002, None)
    p = b.position(res.ticket)
    assert rm.check_modify(p, ask - 0.003, None)          # widening rejected
    assert not rm.check_modify(p, ask - 0.001, None)      # tightening ok


def test_daily_loss_limit_blocks_new_trades():
    b, rm = risk_setup(max_daily_loss_pct=1.0)
    b.balance -= 150  # simulate a realised loss today
    assert "daily loss" in rm.block_reason()


def test_decision_due():
    assert decision_due(datetime(2024, 1, 1, 8, 0), "H1")
    assert not decision_due(datetime(2024, 1, 1, 8, 15), "H1")
    assert decision_due(datetime(2024, 1, 1, 8, 0), "H4")
    assert not decision_due(datetime(2024, 1, 1, 9, 0), "H4")


# ---- Claude agent loop with a scripted fake client ------------------------------------------
class FakeStream:
    def __init__(self, msg):
        self.msg = msg

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get_final_message(self):
        return self.msg


class FakeClient:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._stream))

    def _stream(self, **kwargs):
        self.calls.append(kwargs)
        return FakeStream(self.script.pop(0))


def msg(stop_reason, *blocks):
    usage = SimpleNamespace(input_tokens=100, output_tokens=20, cache_read_input_tokens=0,
                            cache_creation_input_tokens=0)
    return SimpleNamespace(stop_reason=stop_reason, content=list(blocks), usage=usage)


def tool_use(id_, name, inp):
    return SimpleNamespace(type="tool_use", id=id_, name=name, input=inp)


def text(t):
    return SimpleNamespace(type="text", text=t)


def test_claude_agent_opens_trade_through_risk_manager(tmp_path):
    df = synthetic_bars("2024-01-01", 30, "M15", seed=3)
    cfg = Config(watchlist=["EURUSD"], db_path=str(tmp_path / "j.sqlite"))
    b = SimBroker({"EURUSD": df}, {"EURUSD": cfg.spec("EURUSD")}, "M15", 10_000, 7.0, start="2024-01-20")
    j = Journal(cfg.db_path)
    rm = RiskManager(cfg.risk, b, 7.0)
    ex = ToolExecutor(b, rm, j, j.start_run("backtest", "claude"), ["EURUSD"], hide_dates=True)
    bid, ask = b.quote("EURUSD")
    client = FakeClient([
        msg("tool_use", tool_use("t1", "get_candles", {"symbol": "EURUSD", "timeframe": "H1", "count": 20})),
        msg("tool_use", tool_use("t2", "open_trade", {
            "symbol": "EURUSD", "direction": "buy", "stop_loss": ask - 0.0030, "take_profit": ask + 0.0060,
            "risk_percent": 5.0, "setup": "test", "reasoning": "too big"})),
        msg("tool_use", tool_use("t3", "open_trade", {
            "symbol": "EURUSD", "direction": "buy", "stop_loss": ask - 0.0030, "take_profit": ask + 0.0060,
            "risk_percent": 0.5, "setup": "test", "reasoning": "ok"})),
        msg("end_turn", text("done")),
    ])
    agent = ClaudeAgent(AgentConfig(), ex, backtest=True, client=client)
    res = agent.run_cycle("go")
    assert res.text == "done" and res.rounds == 4
    assert [c["error"] for c in res.tool_calls] == [False, True, False]
    assert "REJECTED" in res.tool_calls[1]["result"]
    assert len(b.positions()) == 1
    # request shape: adaptive thinking, effort, cached system prompt, fallbacks
    first = client.calls[0]
    assert first["model"] == "claude-opus-5-5"
    assert first["thinking"] == {"type": "adaptive"}
    assert first["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert first["fallbacks"] == "default"
    # tool results for a turn are sent back together in one user message
    assert client.calls[1]["messages"][2]["content"][0]["type"] == "tool_result"


def test_backtest_session_end_to_end(tmp_path):
    from trading_agent.agent import RandomAgent

    data = {s: synthetic_bars("2024-01-01", 25, "M15", price=p, seed=i)
            for i, (s, p) in enumerate([("EURUSD", 1.1), ("USDJPY", 150.0)])}
    cfg = Config(watchlist=list(data), db_path=str(tmp_path / "j.sqlite"))
    b = SimBroker(data, {s: cfg.spec(s) for s in data}, "M15", 10_000, 7.0)
    s = Session(cfg, b, Journal(cfg.db_path), "backtest", "random",
                agent_factory=lambda ex: RandomAgent(ex, trade_probability=0.5, seed=0))
    stats = s.run_backtest()
    assert stats["trades"] > 10
    assert not b.positions()
    # every closed trade loses at most ~1R (stops respected, sizing correct)
    trades = s.journal.trades(s.run_id)
    assert trades["r_multiple"].min() > -1.6
