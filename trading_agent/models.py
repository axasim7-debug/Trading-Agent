"""Core data types shared by brokers, the risk manager and the agents."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from enum import Enum


class Direction(str, Enum):
    BUY = "buy"
    SELL = "sell"

    @property
    def sign(self) -> int:
        return 1 if self is Direction.BUY else -1


TIMEFRAMES: dict[str, timedelta] = {
    "M1": timedelta(minutes=1),
    "M5": timedelta(minutes=5),
    "M15": timedelta(minutes=15),
    "M30": timedelta(minutes=30),
    "H1": timedelta(hours=1),
    "H4": timedelta(hours=4),
    "D1": timedelta(days=1),
}


@dataclass
class SymbolSpec:
    name: str
    point: float = 0.00001          # smallest price increment
    pip: float = 0.0001             # what traders call a "pip"
    contract_size: float = 100_000  # units per 1.0 lot
    volume_min: float = 0.01
    volume_max: float = 50.0
    volume_step: float = 0.01
    default_spread_points: float = 15

    @property
    def base(self) -> str:
        return self.name[:3].upper()

    @property
    def quote(self) -> str:
        return self.name[3:6].upper()


@dataclass
class Account:
    balance: float
    equity: float
    margin_free: float
    currency: str = "USD"
    is_demo: bool = True


@dataclass
class Position:
    ticket: int
    symbol: str
    direction: Direction
    volume: float
    entry_price: float
    stop_loss: float | None
    take_profit: float | None
    open_time: datetime
    profit: float = 0.0  # floating P/L in account currency

    def to_dict(self) -> dict:
        d = asdict(self)
        d["direction"] = self.direction.value
        d["open_time"] = self.open_time.isoformat()
        return d


@dataclass
class ClosedTrade:
    ticket: int
    symbol: str
    direction: Direction
    volume: float
    entry_price: float
    exit_price: float
    open_time: datetime
    close_time: datetime
    profit: float              # net, account currency (after commission)
    close_reason: str = ""     # "sl", "tp", "manual", "end_of_test", ...


@dataclass
class OrderResult:
    ok: bool
    ticket: int | None = None
    price: float | None = None
    volume: float | None = None
    message: str = ""
    extra: dict = field(default_factory=dict)
