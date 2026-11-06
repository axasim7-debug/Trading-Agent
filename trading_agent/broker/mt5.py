"""MetaTrader 5 broker adapter.

Requires Windows (or Wine), an installed and logged-in MT5 terminal, and
`pip install MetaTrader5`. By default it refuses to run on anything but a DEMO account.
Only positions carrying this bot's magic number are visible to the agent.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from ..config import DEFAULT_SYMBOLS, Config
from ..models import Account, ClosedTrade, Direction, OrderResult, Position, SymbolSpec
from .base import Broker


def _load_mt5():
    try:
        import MetaTrader5 as mt5  # type: ignore
    except ImportError as e:  # pragma: no cover - platform specific
        raise RuntimeError(
            "The MetaTrader5 package is not installed. It only works on Windows: "
            "`pip install MetaTrader5`, with the MT5 terminal installed and logged in."
        ) from e
    return mt5


class MT5Broker(Broker):  # pragma: no cover - needs a live terminal
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.mt5 = mt5 = _load_mt5()
        kwargs = {}
        if cfg.mt5.path:
            kwargs["path"] = cfg.mt5.path
        if cfg.mt5.login:
            kwargs.update(login=int(cfg.mt5.login), password=cfg.mt5.password or "", server=cfg.mt5.server or "")
        if not mt5.initialize(**kwargs):
            raise RuntimeError(f"MT5 initialize() failed: {mt5.last_error()}")

        info = mt5.account_info()
        if info is None:
            raise RuntimeError(f"Could not read the MT5 account: {mt5.last_error()}")
        self.account_currency = info.currency
        self._is_demo = info.trade_mode == mt5.ACCOUNT_TRADE_MODE_DEMO
        if not self._is_demo and not cfg.mt5.allow_real_account:
            mt5.shutdown()
            raise RuntimeError("Refusing to trade: the logged-in MT5 account is NOT a demo account.")

        self.tf_map = {
            "M1": mt5.TIMEFRAME_M1, "M5": mt5.TIMEFRAME_M5, "M15": mt5.TIMEFRAME_M15,
            "M30": mt5.TIMEFRAME_M30, "H1": mt5.TIMEFRAME_H1, "H4": mt5.TIMEFRAME_H4,
            "D1": mt5.TIMEFRAME_D1,
        }
        self._specs: dict[str, SymbolSpec] = {}
        for sym in cfg.watchlist:
            if not mt5.symbol_select(self._sym(sym), True):
                raise RuntimeError(f"Symbol {self._sym(sym)} not available on this broker "
                                   f"(check mt5.symbol_suffix in config)")

    # ---- symbol names -----------------------------------------------------------------
    def _sym(self, symbol: str) -> str:
        return symbol + self.cfg.mt5.symbol_suffix

    def _unsym(self, broker_symbol: str) -> str:
        suf = self.cfg.mt5.symbol_suffix
        return broker_symbol[: -len(suf)] if suf and broker_symbol.endswith(suf) else broker_symbol

    # ---- clock / account ---------------------------------------------------------------
    def now(self) -> datetime:
        """Broker SERVER time (MT5 reports server time as if it were UTC)."""
        tick = self.mt5.symbol_info_tick(self._sym(self.cfg.watchlist[0]))
        if tick is None:
            return datetime.now(timezone.utc).replace(tzinfo=None)
        return datetime.fromtimestamp(tick.time, tz=timezone.utc).replace(tzinfo=None)

    def account(self) -> Account:
        a = self.mt5.account_info()
        return Account(balance=a.balance, equity=a.equity, margin_free=a.margin_free,
                       currency=a.currency, is_demo=self._is_demo)

    def symbol_spec(self, symbol: str) -> SymbolSpec:
        if symbol not in self._specs:
            info = self.mt5.symbol_info(self._sym(symbol))
            if info is None:
                raise KeyError(symbol)
            pip = DEFAULT_SYMBOLS.get(symbol, {}).get("pip")
            if pip is None:
                pip = info.point * 10 if info.digits in (3, 5) else info.point
            self._specs[symbol] = SymbolSpec(
                name=symbol, point=info.point, pip=pip, contract_size=info.trade_contract_size,
                volume_min=info.volume_min, volume_max=info.volume_max, volume_step=info.volume_step,
                default_spread_points=info.spread)
        return self._specs[symbol]

    # ---- market data -------------------------------------------------------------------
    def get_bars(self, symbol: str, timeframe: str, count: int) -> pd.DataFrame:
        # start_pos=1 skips the bar that is still forming
        rates = self.mt5.copy_rates_from_pos(self._sym(symbol), self.tf_map[timeframe], 1, count)
        if rates is None or len(rates) == 0:
            raise KeyError(f"No bars for {symbol} {timeframe}: {self.mt5.last_error()}")
        df = pd.DataFrame(rates)
        df["time"] = pd.to_datetime(df["time"], unit="s")
        return df.set_index("time")[["open", "high", "low", "close", "tick_volume", "spread"]]

    def quote(self, symbol: str) -> tuple[float, float]:
        if self.mt5.symbol_info(self._sym(symbol)) is None:
            raise KeyError(symbol)
        self.mt5.symbol_select(self._sym(symbol), True)
        tick = self.mt5.symbol_info_tick(self._sym(symbol))
        if tick is None:
            raise KeyError(symbol)
        return tick.bid, tick.ask

    # ---- positions & orders ------------------------------------------------------------
    def positions(self) -> list[Position]:
        out = []
        for p in self.mt5.positions_get() or []:
            if p.magic != self.cfg.mt5.magic:
                continue
            out.append(Position(
                ticket=p.ticket, symbol=self._unsym(p.symbol),
                direction=Direction.BUY if p.type == self.mt5.POSITION_TYPE_BUY else Direction.SELL,
                volume=p.volume, entry_price=p.price_open, stop_loss=p.sl or None,
                take_profit=p.tp or None,
                open_time=datetime.fromtimestamp(p.time, tz=timezone.utc).replace(tzinfo=None),
                profit=p.profit + p.swap))
        return out

    def _filling(self, broker_symbol: str) -> int:
        mode = self.mt5.symbol_info(broker_symbol).filling_mode
        if mode & 1:
            return self.mt5.ORDER_FILLING_FOK
        if mode & 2:
            return self.mt5.ORDER_FILLING_IOC
        return self.mt5.ORDER_FILLING_RETURN

    def _send(self, request: dict) -> OrderResult:
        res = self.mt5.order_send(request)
        if res is None:
            return OrderResult(False, message=f"order_send failed: {self.mt5.last_error()}")
        if res.retcode != self.mt5.TRADE_RETCODE_DONE:
            return OrderResult(False, message=f"retcode {res.retcode}: {res.comment}")
        return OrderResult(True, ticket=res.order, price=res.price, volume=res.volume)

    def open_position(self, symbol, direction, volume, stop_loss, take_profit, comment=""):
        bs = self._sym(symbol)
        bid, ask = self.quote(symbol)
        is_buy = direction is Direction.BUY
        req = {
            "action": self.mt5.TRADE_ACTION_DEAL, "symbol": bs, "volume": float(volume),
            "type": self.mt5.ORDER_TYPE_BUY if is_buy else self.mt5.ORDER_TYPE_SELL,
            "price": ask if is_buy else bid, "sl": float(stop_loss), "tp": float(take_profit or 0.0),
            "deviation": self.cfg.mt5.deviation_points, "magic": self.cfg.mt5.magic,
            "comment": comment[:31], "type_time": self.mt5.ORDER_TIME_GTC,
            "type_filling": self._filling(bs),
        }
        return self._send(req)

    def modify_position(self, ticket, stop_loss, take_profit):
        p = self.position(ticket)
        if p is None:
            return OrderResult(False, message=f"No open position {ticket}")
        req = {
            "action": self.mt5.TRADE_ACTION_SLTP, "position": ticket, "symbol": self._sym(p.symbol),
            "sl": float(stop_loss if stop_loss is not None else (p.stop_loss or 0.0)),
            "tp": float(take_profit if take_profit is not None else (p.take_profit or 0.0)),
            "magic": self.cfg.mt5.magic,
        }
        return self._send(req)

    def close_position(self, ticket):
        p = self.position(ticket)
        if p is None:
            return OrderResult(False, message=f"No open position {ticket}")
        bs = self._sym(p.symbol)
        bid, ask = self.quote(p.symbol)
        is_buy = p.direction is Direction.BUY
        req = {
            "action": self.mt5.TRADE_ACTION_DEAL, "symbol": bs, "volume": p.volume, "position": ticket,
            "type": self.mt5.ORDER_TYPE_SELL if is_buy else self.mt5.ORDER_TYPE_BUY,
            "price": bid if is_buy else ask, "deviation": self.cfg.mt5.deviation_points,
            "magic": self.cfg.mt5.magic, "comment": "agent close",
            "type_time": self.mt5.ORDER_TIME_GTC, "type_filling": self._filling(bs),
        }
        return self._send(req)

    def closed_trade(self, ticket):
        if self.position(ticket) is not None:
            return None
        deals = self.mt5.history_deals_get(position=ticket)
        if not deals:
            return None
        ins = [d for d in deals if d.entry == self.mt5.DEAL_ENTRY_IN]
        outs = [d for d in deals if d.entry in (self.mt5.DEAL_ENTRY_OUT, self.mt5.DEAL_ENTRY_OUT_BY)]
        if not ins or not outs:
            return None
        first, last = ins[0], outs[-1]
        net = sum(d.profit + d.commission + d.swap + getattr(d, "fee", 0.0) for d in deals)
        reason = {self.mt5.DEAL_REASON_SL: "sl", self.mt5.DEAL_REASON_TP: "tp",
                  getattr(self.mt5, "DEAL_REASON_SO", -99): "stop_out"}.get(last.reason, "manual")
        return ClosedTrade(
            ticket=ticket, symbol=self._unsym(first.symbol),
            direction=Direction.BUY if first.type == self.mt5.DEAL_TYPE_BUY else Direction.SELL,
            volume=first.volume, entry_price=first.price, exit_price=last.price,
            open_time=datetime.fromtimestamp(first.time, tz=timezone.utc).replace(tzinfo=None),
            close_time=datetime.fromtimestamp(last.time, tz=timezone.utc).replace(tzinfo=None),
            profit=net, close_reason=reason)

    def shutdown(self):
        self.mt5.shutdown()
