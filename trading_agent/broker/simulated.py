"""Bar-by-bar backtest broker.

Rules (deliberately conservative):
* Data prices are BID. Buys fill at ask = bid + spread, sells at bid.
* Orders fill at the OPEN of the bar that starts at `now` (the agent decides on closed bars only).
* Stops/targets are checked against each bar's high/low. If a bar touches both, the stop wins.
* Gaps through a stop/target fill at the bar open (slippage against you on stops).
* Commission is charged per lot at close. Swaps are ignored.
* get_bars() only returns bars that have fully closed before `now` - no look-ahead.
"""
from __future__ import annotations

import itertools
from datetime import datetime

import pandas as pd

from ..indicators import add_indicators
from ..models import (TIMEFRAMES, Account, ClosedTrade, Direction, OrderResult, PendingOrder,
                      Position, SymbolSpec)
from .base import Broker

_AGG = {"open": "first", "high": "max", "low": "min", "close": "last",
        "tick_volume": "sum", "spread": "max"}


class SimBroker(Broker):
    precomputes_indicators = True

    def __init__(self, data: dict[str, pd.DataFrame], specs: dict[str, SymbolSpec],
                 base_timeframe: str = "M15", initial_balance: float = 10_000.0,
                 commission_per_lot: float = 7.0, start: str | datetime | None = None,
                 end: str | datetime | None = None):
        if not data:
            raise ValueError("SimBroker needs data for at least one symbol")
        self.base_tf = base_timeframe
        self.base_td = TIMEFRAMES[base_timeframe]
        self.specs = specs
        self.commission_per_lot = commission_per_lot
        self.balance = initial_balance
        self.data: dict[str, pd.DataFrame] = {}
        for sym, df in data.items():
            df = df.sort_index()
            if "tick_volume" not in df:
                df["tick_volume"] = 0
            if "spread" not in df:
                df["spread"] = 0
            self.data[sym] = df[list(_AGG)]
        self._frames: dict[tuple, pd.DataFrame] = {}

        timeline = pd.DatetimeIndex(sorted(set().union(*[df.index for df in self.data.values()])))
        if start is not None:
            timeline = timeline[timeline >= pd.Timestamp(start)]
        if end is not None:
            timeline = timeline[timeline < pd.Timestamp(end)]
        if len(timeline) == 0:
            raise ValueError("No bars inside the requested start/end window")
        self.timeline = timeline
        self._i = 0
        self._tickets = itertools.count(1)
        self._open: dict[int, Position] = {}
        self._closed: dict[int, ClosedTrade] = {}
        self._partials: dict[int, float] = {}   # realised P/L of partial closes, per ticket
        self._pending: dict[int, PendingOrder] = {}

    # ---- clock ----------------------------------------------------------------------------
    def now(self) -> datetime:
        return self.timeline[self._i].to_pydatetime()

    def step(self) -> bool:
        """Play the bar that starts at `now` (fill stops/targets), then advance the clock.
        Returns False when the data is exhausted."""
        t = self.timeline[self._i]
        for sym, df in self.data.items():
            if t in df.index:
                bar = df.loc[t]
                self._process_pending(sym, bar)   # fills first; a stop hit in the same bar still counts
                self._process_bar(sym, bar)
        if self._i + 1 >= len(self.timeline):
            return False
        self._i += 1
        return True

    # ---- market data ----------------------------------------------------------------------
    def symbol_spec(self, symbol: str) -> SymbolSpec:
        return self.specs[symbol]

    def _ind_frame(self, symbol: str, timeframe: str) -> pd.DataFrame:
        """Indicators computed once over the full history. Safe because every indicator is causal
        and get_bars() only ever returns rows that closed before `now`."""
        key = (symbol, timeframe, "ind")
        if key not in self._frames:
            self._frames[key] = add_indicators(self._frame(symbol, timeframe))
        return self._frames[key]

    def _frame(self, symbol: str, timeframe: str) -> pd.DataFrame:
        key = (symbol, timeframe)
        if key not in self._frames:
            td = TIMEFRAMES[timeframe]
            if td < self.base_td:
                raise ValueError(f"Timeframe {timeframe} is smaller than the data timeframe {self.base_tf}")
            df = self.data[symbol]
            if td != self.base_td:
                df = df.resample(td, label="left", closed="left").agg(_AGG).dropna(subset=["open"])
            self._frames[key] = df
        return self._frames[key]

    def get_bars(self, symbol: str, timeframe: str, count: int, with_indicators: bool = False) -> pd.DataFrame:
        if symbol not in self.data:
            raise KeyError(symbol)
        df = self._ind_frame(symbol, timeframe) if with_indicators else self._frame(symbol, timeframe)
        cutoff = pd.Timestamp(self.now()) - TIMEFRAMES[timeframe]  # bar must have closed by now
        end = df.index.searchsorted(cutoff, side="right")
        return df.iloc[max(0, end - count):end].copy()

    def _spread_price(self, symbol: str, bar: pd.Series | None = None) -> float:
        spec = self.specs[symbol]
        pts = float(bar["spread"]) if bar is not None and bar["spread"] > 0 else spec.default_spread_points
        return pts * spec.point

    def quote(self, symbol: str) -> tuple[float, float]:
        if symbol not in self.data:
            raise KeyError(symbol)
        df = self.data[symbol]
        t = pd.Timestamp(self.now())
        if t in df.index:
            bar = df.loc[t]
            bid = float(bar["open"])
        else:  # market closed for this symbol right now: last known close
            pos = df.index.searchsorted(t, side="left") - 1
            if pos < 0:
                raise KeyError(f"No data for {symbol} yet")
            bar = df.iloc[pos]
            bid = float(bar["close"])
        return bid, bid + self._spread_price(symbol, bar)

    def has_bar_now(self, symbol: str) -> bool:
        return pd.Timestamp(self.now()) in self.data[symbol].index

    # ---- account --------------------------------------------------------------------------
    def _floating(self, p: Position) -> float:
        bid, ask = self.quote(p.symbol)
        exit_price = bid if p.direction is Direction.BUY else ask
        diff = (exit_price - p.entry_price) * p.direction.sign
        return self.quote_to_account(p.symbol, diff * p.volume * self.specs[p.symbol].contract_size)

    def account(self) -> Account:
        floating = sum(self._floating(p) for p in self._open.values())
        eq = self.balance + floating
        return Account(balance=self.balance, equity=eq, margin_free=eq, is_demo=True)

    def positions(self) -> list[Position]:
        out = []
        for p in self._open.values():
            p.profit = self._floating(p)
            out.append(p)
        return out

    # ---- orders ---------------------------------------------------------------------------
    def open_position(self, symbol, direction, volume, stop_loss, take_profit, comment=""):
        if not self.has_bar_now(symbol):
            return OrderResult(False, message=f"Market closed for {symbol} at this time")
        bid, ask = self.quote(symbol)
        price = ask if direction is Direction.BUY else bid
        ticket = next(self._tickets)
        self._open[ticket] = Position(ticket, symbol, direction, volume, price, stop_loss,
                                      take_profit, self.now())
        return OrderResult(True, ticket=ticket, price=price, volume=volume)

    def modify_position(self, ticket, stop_loss, take_profit):
        p = self._open.get(ticket)
        if p is None:
            return OrderResult(False, message=f"No open position {ticket}")
        if stop_loss is not None:
            p.stop_loss = stop_loss
        if take_profit is not None:
            p.take_profit = take_profit
        return OrderResult(True, ticket=ticket)

    def close_position(self, ticket, reason: str = "manual"):
        p = self._open.get(ticket)
        if p is None:
            return OrderResult(False, message=f"No open position {ticket}")
        bid, ask = self.quote(p.symbol)
        price = bid if p.direction is Direction.BUY else ask
        self._close(p, price, reason)
        return OrderResult(True, ticket=ticket, price=price)

    def place_pending(self, symbol, direction, order_type, price, volume, stop_loss, take_profit,
                      expires=None, comment=""):
        if symbol not in self.data:
            return OrderResult(False, message=f"Unknown symbol {symbol}")
        ticket = next(self._tickets)
        self._pending[ticket] = PendingOrder(ticket, symbol, direction, order_type, price, volume,
                                             stop_loss, take_profit, self.now(), expires)
        return OrderResult(True, ticket=ticket, price=price, volume=volume)

    def pending_orders(self):
        return list(self._pending.values())

    def cancel_pending(self, ticket):
        if self._pending.pop(ticket, None) is None:
            return OrderResult(False, message=f"No pending order {ticket}")
        return OrderResult(True, ticket=ticket)

    def _process_pending(self, symbol: str, bar: pd.Series) -> None:
        s = self._spread_price(symbol, bar)
        o, h, l = float(bar["open"]), float(bar["high"]), float(bar["low"])
        for po in [x for x in self._pending.values() if x.symbol == symbol]:
            if po.expires is not None and self.now() >= po.expires:
                del self._pending[po.ticket]
                continue
            fill = None
            if po.direction is Direction.BUY:          # buys fill at ask = bid + spread
                if po.order_type == "limit" and l + s <= po.price:
                    fill = min(po.price, o + s)
                elif po.order_type == "stop" and h + s >= po.price:
                    fill = max(po.price, o + s)
            else:                                       # sells fill at bid
                if po.order_type == "limit" and h >= po.price:
                    fill = max(po.price, o)
                elif po.order_type == "stop" and l <= po.price:
                    fill = min(po.price, o)
            if fill is not None:
                del self._pending[po.ticket]
                # MT5-style: the position keeps the order's ticket
                self._open[po.ticket] = Position(po.ticket, symbol, po.direction, po.volume, fill,
                                                 po.stop_loss, po.take_profit, self.now())

    def close_partial(self, ticket, volume):
        p = self._open.get(ticket)
        if p is None:
            return OrderResult(False, message=f"No open position {ticket}")
        spec = self.specs[p.symbol]
        volume = round(int(volume / spec.volume_step + 1e-9) * spec.volume_step, 8)
        if volume <= 0 or volume >= p.volume:
            return OrderResult(False, message="partial volume must be > 0 and smaller than the position")
        bid, ask = self.quote(p.symbol)
        price = bid if p.direction is Direction.BUY else ask
        gross = self.quote_to_account(
            p.symbol, (price - p.entry_price) * p.direction.sign * volume * spec.contract_size)
        net = gross - self.commission_per_lot * volume
        self.balance += net
        self._partials[ticket] = self._partials.get(ticket, 0.0) + net
        p.volume = round(p.volume - volume, 8)
        return OrderResult(True, ticket=ticket, price=price, volume=volume)

    def close_all(self, reason: str = "end_of_test") -> None:
        self._pending.clear()
        for t in list(self._open):
            self.close_position(t, reason)

    def closed_trade(self, ticket):
        return self._closed.get(ticket)

    def _close(self, p: Position, price: float, reason: str) -> None:
        spec = self.specs[p.symbol]
        gross = self.quote_to_account(
            p.symbol, (price - p.entry_price) * p.direction.sign * p.volume * spec.contract_size)
        net = gross - self.commission_per_lot * p.volume
        self.balance += net
        del self._open[p.ticket]
        total = net + self._partials.pop(p.ticket, 0.0)
        self._closed[p.ticket] = ClosedTrade(p.ticket, p.symbol, p.direction, p.volume, p.entry_price,
                                             price, p.open_time, self.now(), total, reason)

    def _process_bar(self, symbol: str, bar: pd.Series) -> None:
        s = self._spread_price(symbol, bar)
        o, h, l = float(bar["open"]), float(bar["high"]), float(bar["low"])
        for p in [p for p in self._open.values() if p.symbol == symbol]:
            sl, tp = p.stop_loss, p.take_profit
            if p.direction is Direction.BUY:          # exits at bid
                if sl is not None and o <= sl:
                    self._close(p, o, "sl")
                elif tp is not None and o >= tp:
                    self._close(p, o, "tp")
                elif sl is not None and l <= sl:
                    self._close(p, sl, "sl")
                elif tp is not None and h >= tp:
                    self._close(p, tp, "tp")
            else:                                      # exits at ask = bid + spread
                if sl is not None and o + s >= sl:
                    self._close(p, o + s, "sl")
                elif tp is not None and o + s <= tp:
                    self._close(p, o + s, "tp")
                elif sl is not None and h + s >= sl:
                    self._close(p, sl, "sl")
                elif tp is not None and l + s <= tp:
                    self._close(p, tp, "tp")
