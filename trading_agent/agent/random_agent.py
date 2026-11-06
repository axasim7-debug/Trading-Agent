"""Random baseline. Same risk manager, same costs, no skill.
If the AI agent can't beat this by a statistically clear margin, it has no edge."""
from __future__ import annotations

import random

from ..market import bars_with_indicators
from .claude_agent import CycleResult
from .tools import ToolExecutor


class RandomAgent:
    name = "random"

    def __init__(self, executor: ToolExecutor, trade_probability: float = 0.1,
                 risk_percent: float = 0.5, atr_stop_mult: float = 1.5, reward_risk: float = 1.5,
                 seed: int | None = None):
        self.executor = executor
        self.p = trade_probability
        self.risk_percent = risk_percent
        self.k = atr_stop_mult
        self.rr = reward_risk
        self.rng = random.Random(seed)

    def run_cycle(self, prompt: str = "", allow_trading: bool = True) -> CycleResult:
        res = CycleResult(text="random baseline", stop_reason="end_turn", rounds=0)
        if not allow_trading or self.rng.random() >= self.p:
            return res
        ex = self.executor
        open_syms = {p.symbol for p in ex.broker.positions()}
        candidates = [s for s in ex.watchlist if s not in open_syms]
        if not candidates:
            return res
        sym = self.rng.choice(candidates)
        direction = self.rng.choice(["buy", "sell"])
        h1 = bars_with_indicators(ex.broker, sym, "H1", 5)
        if h1.empty or h1["atr14"].isna().iloc[-1]:
            return res
        atr = float(h1["atr14"].iloc[-1])
        bid, ask = ex.broker.quote(sym)
        entry = ask if direction == "buy" else bid
        sign = 1 if direction == "buy" else -1
        sl = entry - sign * self.k * atr
        tp = entry + sign * self.k * atr * self.rr
        text, err = ex.execute("open_trade", {
            "symbol": sym, "direction": direction, "stop_loss": sl, "take_profit": tp,
            "risk_percent": self.risk_percent, "setup": "random", "reasoning": "random baseline"})
        res.tool_calls.append({"name": "open_trade", "input": {"symbol": sym}, "error": err, "result": text[:300]})
        return res
