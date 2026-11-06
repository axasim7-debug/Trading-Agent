"""Configuration loading (YAML file + environment variables for secrets)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .models import SymbolSpec

DEFAULT_SYMBOLS: dict[str, dict] = {
    "EURUSD": dict(point=0.00001, pip=0.0001, contract_size=100_000, default_spread_points=12),
    "GBPUSD": dict(point=0.00001, pip=0.0001, contract_size=100_000, default_spread_points=15),
    "AUDUSD": dict(point=0.00001, pip=0.0001, contract_size=100_000, default_spread_points=14),
    "USDJPY": dict(point=0.001, pip=0.01, contract_size=100_000, default_spread_points=14),
    "USDCHF": dict(point=0.00001, pip=0.0001, contract_size=100_000, default_spread_points=16),
    "USDCAD": dict(point=0.00001, pip=0.0001, contract_size=100_000, default_spread_points=18),
    "XAUUSD": dict(point=0.01, pip=0.1, contract_size=100, default_spread_points=30),
}


@dataclass
class RiskConfig:
    max_risk_per_trade_pct: float = 1.0
    max_total_open_risk_pct: float = 3.0
    max_open_positions: int = 3
    max_positions_per_symbol: int = 1
    max_daily_loss_pct: float = 3.0
    daily_profit_target_pct: float = 2.0
    stop_trading_at_target: bool = False
    min_reward_risk: float = 1.0          # only checked when a take-profit is given
    require_take_profit: bool = False
    max_spread_to_stop_ratio: float = 0.2  # spread may be at most 20% of the stop distance
    max_trades_per_day: int = 10
    max_currency_exposure_pct: float = 2.0    # net risk on any single currency (e.g. long EURUSD + long GBPUSD = USD short twice)
    news_blackout_minutes_before: int = 30    # no new trades on a currency this close to a high-impact release
    news_blackout_minutes_after: int = 15
    news_blackout_min_impact: str = "High"


@dataclass
class AgentConfig:
    model: str = "claude-opus-5-5"
    effort: str = "high"
    language: str = "ar"           # language for the agent's written reasoning/reports
    max_tool_rounds: int = 15
    web_search: bool = False       # live mode only - never enabled in backtests (look-ahead)
    daily_review: bool = True
    notes_in_prompt: int = 15      # how many recent lessons/notes to show each cycle
    use_fallbacks: bool = True     # server-side refusal fallback (Opus 5.5 / Fable 5.1 / Sonnet 5.5 on the Claude API)


@dataclass
class BacktestConfig:
    data_dir: str = "data"
    base_timeframe: str = "M15"
    start: str | None = None
    end: str | None = None
    initial_balance: float = 10_000.0
    hide_dates: bool = True           # hide calendar dates from the LLM to reduce memorised-history leakage
    max_cycles: int | None = None


@dataclass
class MT5Config:
    login: int | None = None
    password: str | None = None
    server: str | None = None
    path: str | None = None           # path to terminal64.exe (optional)
    allow_real_account: bool = False
    magic: int = 515151
    deviation_points: int = 20
    symbol_suffix: str = ""           # e.g. ".m" or "pro" depending on the broker


@dataclass
class WatchdogConfig:
    poll_seconds: int = 10
    max_wakes_per_hour: int = 3
    close_all_on_daily_loss: bool = False
    # Command that wakes the agent when an alert triggers ({context} is replaced). None = never wake.
    wake_command: list[str] | None = field(default_factory=lambda: [
        "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "scripts\\run_routine.ps1",
        "-Routine", "trade-cycle", "-Context", "{context}"])
    # Applied to every position unless the agent sets its own plan with set_trade_management.
    default_management: dict = field(default_factory=lambda: {
        "break_even_at_r": 1.0, "partial_at_r": None, "trail_atr_mult": None, "time_stop_hours": None})


@dataclass
class ResearchConfig:
    stats_path: str = "cache/edge_stats.json"
    max_hold_hours: int = 72
    oos_fraction: float = 0.3          # last 30% of the history is out-of-sample
    min_trades: int = 30
    min_oos_trades: int = 15


@dataclass
class Config:
    watchlist: list[str] = field(default_factory=lambda: ["EURUSD", "GBPUSD", "USDJPY", "XAUUSD"])
    decision_timeframe: str = "H1"
    db_path: str = "journal.sqlite"
    commission_per_lot: float = 7.0   # round-turn, account currency (used in backtests and in risk sizing)
    risk: RiskConfig = field(default_factory=RiskConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    backtest: BacktestConfig = field(default_factory=BacktestConfig)
    mt5: MT5Config = field(default_factory=MT5Config)
    watchdog: WatchdogConfig = field(default_factory=WatchdogConfig)
    research: ResearchConfig = field(default_factory=ResearchConfig)
    symbols: dict[str, SymbolSpec] = field(default_factory=dict)

    def spec(self, symbol: str) -> SymbolSpec:
        if symbol not in self.symbols:
            base = DEFAULT_SYMBOLS.get(symbol, {})
            self.symbols[symbol] = SymbolSpec(name=symbol, **base)
        return self.symbols[symbol]


def _fill(dc_cls, data: dict | None):
    data = data or {}
    known = {f for f in dc_cls.__dataclass_fields__}
    unknown = set(data) - known
    if unknown:
        raise ValueError(f"Unknown keys for {dc_cls.__name__}: {sorted(unknown)}")
    return dc_cls(**data)


def load_config(path: str | os.PathLike | None = None) -> Config:
    raw: dict = {}
    if path and Path(path).exists():
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}

    cfg = Config(
        watchlist=raw.get("watchlist", Config().watchlist),
        decision_timeframe=raw.get("decision_timeframe", "H1"),
        db_path=raw.get("db_path", "journal.sqlite"),
        commission_per_lot=float(raw.get("commission_per_lot", 7.0)),
        risk=_fill(RiskConfig, raw.get("risk")),
        agent=_fill(AgentConfig, raw.get("agent")),
        backtest=_fill(BacktestConfig, raw.get("backtest")),
        mt5=_fill(MT5Config, raw.get("mt5")),
        watchdog=_fill(WatchdogConfig, raw.get("watchdog")),
        research=_fill(ResearchConfig, raw.get("research")),
    )
    for name, spec in (raw.get("symbols") or {}).items():
        merged = {**DEFAULT_SYMBOLS.get(name, {}), **(spec or {})}
        cfg.symbols[name] = SymbolSpec(name=name, **merged)
    for name in cfg.watchlist:
        cfg.spec(name)

    # Secrets come from the environment, never from the committed YAML file.
    if os.environ.get("MT5_LOGIN"):
        cfg.mt5.login = int(os.environ["MT5_LOGIN"])
    if os.environ.get("MT5_PASSWORD"):
        cfg.mt5.password = os.environ["MT5_PASSWORD"]
    if os.environ.get("MT5_SERVER"):
        cfg.mt5.server = os.environ["MT5_SERVER"]
    return cfg
