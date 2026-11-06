"""Broker interface. The agent, risk manager and runner only talk to this API,
so the same agent runs unchanged on the backtest simulator and on MetaTrader 5."""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

import pandas as pd

from ..models import Account, ClosedTrade, Direction, OrderResult, PendingOrder, Position, SymbolSpec


class Broker(ABC):
    account_currency: str = "USD"

    @abstractmethod
    def now(self) -> datetime: ...

    @abstractmethod
    def account(self) -> Account: ...

    @abstractmethod
    def symbol_spec(self, symbol: str) -> SymbolSpec: ...

    @abstractmethod
    def get_bars(self, symbol: str, timeframe: str, count: int) -> pd.DataFrame:
        """Return up to `count` COMPLETED bars (oldest first), indexed by bar open time.
        Columns: open, high, low, close, tick_volume, spread (points)."""

    @abstractmethod
    def quote(self, symbol: str) -> tuple[float, float]:
        """Current (bid, ask)."""

    @abstractmethod
    def positions(self) -> list[Position]: ...

    @abstractmethod
    def open_position(self, symbol: str, direction: Direction, volume: float,
                      stop_loss: float, take_profit: float | None, comment: str = "") -> OrderResult: ...

    @abstractmethod
    def modify_position(self, ticket: int, stop_loss: float | None, take_profit: float | None) -> OrderResult: ...

    @abstractmethod
    def close_position(self, ticket: int) -> OrderResult: ...

    # ---- pending orders (limit / stop entries) ----------------------------------------------
    def place_pending(self, symbol: str, direction: Direction, order_type: str, price: float, volume: float,
                      stop_loss: float, take_profit: float | None, expires=None, comment: str = "") -> OrderResult:
        return OrderResult(False, message="pending orders not supported by this broker")

    def pending_orders(self) -> list[PendingOrder]:
        return []

    def cancel_pending(self, ticket: int) -> OrderResult:
        return OrderResult(False, message="pending orders not supported by this broker")

    def close_partial(self, ticket: int, volume: float) -> OrderResult:
        return OrderResult(False, message="partial close not supported by this broker")

    @abstractmethod
    def closed_trade(self, ticket: int) -> ClosedTrade | None:
        """Details of a position that is no longer open, or None if still open/unknown."""

    def utc_offset(self):
        """Broker clock minus UTC. Simulated data is treated as UTC."""
        from datetime import timedelta
        return timedelta(0)

    def shutdown(self) -> None:  # pragma: no cover - optional hook
        pass

    # ---- helpers shared by all brokers -------------------------------------------------
    def quote_to_account(self, symbol: str, amount: float) -> float:
        """Convert an amount in the symbol's quote currency into the account currency."""
        q = self.symbol_spec(symbol).quote
        acc = self.account_currency
        if q == acc:
            return amount
        for pair, invert in ((q + acc, False), (acc + q, True)):
            try:
                bid, ask = self.quote(pair)
            except KeyError:
                continue
            mid = (bid + ask) / 2
            return amount / mid if invert else amount * mid
        raise KeyError(
            f"Cannot convert {q} to {acc}: add {q}{acc} or {acc}{q} data/symbol so P/L can be valued."
        )

    def position(self, ticket: int) -> Position | None:
        return next((p for p in self.positions() if p.ticket == ticket), None)
