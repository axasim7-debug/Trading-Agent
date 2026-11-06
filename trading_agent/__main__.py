"""Command line entry point: python -m trading_agent <command> ..."""
from __future__ import annotations

import argparse
import logging
import sys

from .config import load_config
from .journal import Journal
from .stats import format_report, performance


def _agent_factory(kind: str, cfg, backtest: bool, args):
    from .agent import ClaudeAgent, RandomAgent

    if kind == "claude":
        return lambda ex: ClaudeAgent(cfg.agent, ex, backtest=backtest)
    if kind == "random":
        return lambda ex: RandomAgent(ex, trade_probability=args.random_p,
                                      risk_percent=min(0.5, cfg.risk.max_risk_per_trade_pct), seed=args.seed)
    raise SystemExit(f"Unknown agent {kind}")


def cmd_backtest(args):
    from .broker import SimBroker
    from .data import load_symbols
    from .runner import Session

    cfg = load_config(args.config)
    if args.max_cycles is not None:
        cfg.backtest.max_cycles = args.max_cycles
    bt = cfg.backtest
    data = load_symbols(bt.data_dir, cfg.watchlist, bt.base_timeframe)
    broker = SimBroker(data, {s: cfg.spec(s) for s in data}, bt.base_timeframe, bt.initial_balance,
                       cfg.commission_per_lot, start=bt.start, end=bt.end)
    journal = Journal(cfg.db_path)
    session = Session(cfg, broker, journal, "backtest", args.agent, args.label,
                      _agent_factory(args.agent, cfg, True, args))
    session.run_backtest()
    print(session.report_text())


def cmd_live(args):  # pragma: no cover - needs MT5
    from .broker.mt5 import MT5Broker
    from .runner import Session

    cfg = load_config(args.config)
    broker = MT5Broker(cfg)
    journal = Journal(cfg.db_path)
    session = Session(cfg, broker, journal, "live", args.agent, args.label,
                      _agent_factory(args.agent, cfg, False, args))
    session.run_live(poll_seconds=args.poll)


def cmd_report(args):
    cfg = load_config(args.config)
    journal = Journal(cfg.db_path)
    runs = journal.runs()
    if runs.empty:
        print("No runs recorded yet.")
        return
    ids = args.runs or runs["id"].tolist()
    for rid in ids:
        meta = runs[runs["id"] == rid].iloc[0]
        trades = journal.trades(rid)
        eq = journal.equity_curve(rid)
        init = eq["balance"].iloc[0] if not eq.empty else None
        print(format_report(f"run {rid} - {meta['mode']} / {meta['agent']} {meta['label'] or ''}",
                            performance(trades, eq, init), journal.usage_totals(rid)))
        print()


def cmd_download(args):  # pragma: no cover - needs MT5
    from .data import download_from_mt5

    cfg = load_config(args.config)
    for p in download_from_mt5(cfg, args.timeframe, args.bars):
        print("wrote", p)


def cmd_sample_data(args):
    from pathlib import Path

    from .data import data_path, synthetic_bars

    cfg = load_config(args.config)
    Path(cfg.backtest.data_dir).mkdir(parents=True, exist_ok=True)
    start_prices = {"EURUSD": 1.10, "GBPUSD": 1.27, "AUDUSD": 0.66, "USDJPY": 150.0,
                    "USDCHF": 0.88, "USDCAD": 1.36, "XAUUSD": 2000.0}
    for i, s in enumerate(cfg.watchlist):
        spec = cfg.spec(s)
        df = synthetic_bars("2024-01-01", args.days, cfg.backtest.base_timeframe,
                            price=start_prices.get(s, 1.0), spread_points=spec.default_spread_points, seed=i)
        p = data_path(cfg.backtest.data_dir, s, cfg.backtest.base_timeframe)
        df.reset_index(names="time").to_csv(p, index=False)
        print("wrote", p, "(synthetic random walk - for pipeline testing only)")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="trading_agent", description="AI trading agent research harness")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("backtest", help="Run an agent on historical data")
    p.add_argument("--agent", choices=["claude", "random"], default="claude")
    p.add_argument("--label", default="")
    p.add_argument("--max-cycles", type=int, default=None)
    p.add_argument("--seed", type=int, default=None)
    p.add_argument("--random-p", type=float, default=0.1, help="trade probability per cycle for the random agent")
    p.set_defaults(func=cmd_backtest)

    p = sub.add_parser("live", help="Run the agent on the MT5 demo account")
    p.add_argument("--agent", choices=["claude", "random"], default="claude")
    p.add_argument("--label", default="")
    p.add_argument("--poll", type=int, default=15)
    p.add_argument("--seed", type=int, default=None)
    p.add_argument("--random-p", type=float, default=0.1)
    p.set_defaults(func=cmd_live)

    p = sub.add_parser("report", help="Print statistics for recorded runs")
    p.add_argument("runs", nargs="*", type=int)
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("download", help="Download history from MT5 into the data folder")
    p.add_argument("--timeframe", default="M15")
    p.add_argument("--bars", type=int, default=100_000)
    p.set_defaults(func=cmd_download)

    p = sub.add_parser("sample-data", help="Write synthetic random-walk data to try the pipeline")
    p.add_argument("--days", type=int, default=120)
    p.set_defaults(func=cmd_sample_data)

    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(message)s")
    args.func(args)


if __name__ == "__main__":
    sys.exit(main())
