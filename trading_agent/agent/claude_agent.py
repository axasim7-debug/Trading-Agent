"""Claude trading agent: a manual tool-use loop, one fresh conversation per decision cycle."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from ..config import AgentConfig
from .prompts import system_prompt
from .tools import TOOL_SCHEMAS, TRADING_TOOLS, ToolExecutor

log = logging.getLogger(__name__)

FALLBACK_BETA = "server-side-fallback-2026-07-01"


@dataclass
class CycleResult:
    text: str = ""
    stop_reason: str = ""
    rounds: int = 0
    tool_calls: list[dict] = field(default_factory=list)
    usage: dict = field(default_factory=dict)


class ClaudeAgent:
    name = "claude"

    def __init__(self, cfg: AgentConfig, executor: ToolExecutor, backtest: bool, client=None):
        self.cfg = cfg
        self.executor = executor
        self.backtest = backtest
        if client is None:
            import anthropic
            client = anthropic.Anthropic()
        self.client = client
        self.system = [{
            "type": "text",
            "text": system_prompt(cfg.language, backtest),
            "cache_control": {"type": "ephemeral"},
        }]

    def _tools(self, allow_trading: bool) -> list[dict]:
        tools = [t for t in TOOL_SCHEMAS if allow_trading or t["name"] not in TRADING_TOOLS]
        # Web search only live: in a backtest it would leak the future.
        if self.cfg.web_search and not self.backtest:
            tools = tools + [{"type": "web_search_20260209", "name": "web_search", "max_uses": 3}]
        return tools

    def _create(self, messages: list, tools: list):
        kwargs = dict(
            model=self.cfg.model,
            max_tokens=32000,
            system=self.system,
            tools=tools,
            messages=messages,
            thinking={"type": "adaptive"},
            output_config={"effort": self.cfg.effort},
            cache_control={"type": "ephemeral"},  # automatic caching of the growing tail
        )
        if self.cfg.use_fallbacks:
            kwargs.update(betas=[FALLBACK_BETA], fallbacks="default")
        # Streaming avoids HTTP timeouts on long thinking turns.
        with self.client.beta.messages.stream(**kwargs) as stream:
            return stream.get_final_message()

    def run_cycle(self, prompt: str, allow_trading: bool = True) -> CycleResult:
        tools = self._tools(allow_trading)
        messages: list = [{"role": "user", "content": prompt}]
        result = CycleResult()

        for round_no in range(1, self.cfg.max_tool_rounds + 2):
            response = self._create(messages, tools)
            result.rounds = round_no
            _add_usage(result.usage, response.usage)
            result.stop_reason = response.stop_reason or ""

            if response.stop_reason == "refusal":
                log.warning("Model refused this cycle: %s", getattr(response, "stop_details", None))
                break
            # Append the full content (thinking blocks included) - the history stays append-only.
            messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason == "pause_turn":   # server tool (web search) paused; continue
                continue
            if response.stop_reason != "tool_use":
                break

            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                if block.name in TRADING_TOOLS and not allow_trading:
                    text, is_err = "Trading tools are disabled in this cycle.", True
                else:
                    text, is_err = self.executor.execute(block.name, block.input)
                result.tool_calls.append({"name": block.name, "input": block.input,
                                          "error": is_err, "result": text[:500]})
                tool_results.append({"type": "tool_result", "tool_use_id": block.id,
                                     "content": text, "is_error": is_err})
            if round_no >= self.cfg.max_tool_rounds:
                tool_results.append({"type": "text", "text": "Tool budget for this cycle is used up. "
                                     "Do not call more tools; write your final summary now."})
            messages.append({"role": "user", "content": tool_results})

        result.text = "\n".join(b.text for b in messages[-1]["content"]
                                if getattr(b, "type", None) == "text") if messages[-1]["role"] == "assistant" else ""
        return result


def _add_usage(acc: dict, usage) -> None:
    if usage is None:
        return
    for k in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
        v = getattr(usage, k, None)
        if isinstance(v, int):
            acc[k] = acc.get(k, 0) + v
    acc["api_calls"] = acc.get("api_calls", 0) + 1


def summarize_calls(calls: list[dict]) -> str:
    return json.dumps([{k: c[k] for k in ("name", "error")} for c in calls])
