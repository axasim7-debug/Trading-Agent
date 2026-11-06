"""`python -m trading_agent lab <command>`: explore | evaluate | finalize | list."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pandas as pd

from ..data import load_symbols, mt5_available, update_history
from .evaluate import Evaluator
from .explore import explore
from .features import lab_features
from .registry import Registry
from .strategy import load_strategies

LAB_DIR = Path("lab")


def _load(cfg, update: bool, bars: int):
    if update and mt5_available():  # pragma: no cover - needs MT5
        print("updating history from MT5 ...")
        update_history(cfg, cfg.backtest.base_timeframe, bars)
    data = load_symbols(cfg.backtest.data_dir, cfg.watchlist, cfg.backtest.base_timeframe)
    print("building features ...")
    feats = {s: lab_features(df) for s, df in data.items()}
    return data, feats


def _save_report(name: str, obj) -> Path:
    out = LAB_DIR / "reports" / f"{datetime.now():%Y%m%d-%H%M}-{name}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(obj, indent=1, default=str, ensure_ascii=False), encoding="utf-8")
    return out


def cmd_explore(cfg, args) -> None:
    _, feats = _load(cfg, not args.no_update, args.bars)
    df = explore(feats, cfg.lab.holdout_fraction)
    if df.empty:
        print("not enough data")
        return
    path = LAB_DIR / "reports" / f"{datetime.now():%Y%m%d-%H%M}-explore.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False)
    disc = df[df["discovery"]]
    print(f"{len(df)} tests run (development data only, holdout untouched). "
          f"{len(disc)} discoveries at FDR 10% with consistent out-of-sample sign.")
    cols = ["condition", "horizon_h", "symbol", "n", "mean_fwd_atr", "hit_rate_up", "t", "q_value",
            "is_mean", "oos_mean", "oos_t"]
    with pd.option_context("display.width", 200, "display.max_columns", 20):
        print((disc if len(disc) else df.head(args.top))[cols].head(args.top).to_string(index=False))
    print(f"\nfull table: {path}")
    print("Findings are hypotheses (no costs/stops). Turn promising ones into strategies/ files and evaluate.")


def cmd_evaluate(cfg, args) -> None:
    strategies = load_strategies(args.strategies_dir)
    names = list(strategies) if args.name == "all" else [args.name]
    missing = [n for n in names if n not in strategies]
    if missing:
        raise SystemExit(f"unknown strategy {missing}; available: {list(strategies)}")
    data, feats = _load(cfg, not args.no_update, args.bars)
    reg = Registry(str(LAB_DIR / "registry.json"))
    ev = Evaluator(data, feats, {s: cfg.spec(s) for s in data}, cfg.commission_per_lot,
                   cfg.research.max_hold_hours, cfg.lab.holdout_fraction, cfg.lab.walk_forward_folds)
    for n in names:
        st = strategies[n]
        status = reg.data["strategies"].get(n, {}).get("status")
        if status in ("approved", "rejected"):
            print(f"skip {n}: already {status}")
            continue
        rep = ev.evaluate(st, reg.trials + len(st.param_sets()), cfg.lab.min_oos_trades)
        reg.record_evaluation(rep)
        path = _save_report(f"eval-{n}", rep)
        wf = rep["walk_forward_oos"]
        print(f"\n=== {n} === {'CANDIDATE' if rep['candidate'] else 'not a candidate'}")
        print(f"walk-forward OOS: n={wf['n']} avg_R={wf['avg_r']} t={wf['t']} (hurdle {rep['t_hurdle']}) "
              f"win={wf['win_rate']} PF={wf['profit_factor']}")
        print(f"params={rep['final_params']} robustness={rep['param_robustness_pct_positive']}% "
              f"cost x2 avg_R={rep['cost_stress_2x']['avg_r']} years={rep['by_year_avg_r']}")
        for g, ok in rep["gates"].items():
            print(f"  [{'x' if ok else ' '}] {g}")
        print(f"report: {path}")


def cmd_finalize(cfg, args) -> None:
    strategies = load_strategies(args.strategies_dir)
    if args.name not in strategies:
        raise SystemExit(f"unknown strategy {args.name}")
    reg = Registry(str(LAB_DIR / "registry.json"))
    entry = reg.data["strategies"].get(args.name)
    if not entry or entry.get("status") != "candidate":
        raise SystemExit(f"{args.name} is not a candidate (status: {entry and entry.get('status')})")
    data, feats = _load(cfg, False, 0)
    ev = Evaluator(data, feats, {s: cfg.spec(s) for s in data}, cfg.commission_per_lot,
                   cfg.research.max_hold_hours, cfg.lab.holdout_fraction, cfg.lab.walk_forward_folds)
    res = ev.finalize(strategies[args.name], entry["final_params"], cfg.lab.min_holdout_trades)
    entry = reg.record_finalize(args.name, res)
    _save_report(f"holdout-{args.name}", res)
    print(f"{args.name}: holdout {res['holdout']} -> {entry['status'].upper()}")
    if entry["status"] == "approved":
        print("Approved strategies are scanned live by scan_setups, with their lab statistics as edge card.")


def cmd_list(cfg, args) -> None:
    reg = Registry(str(LAB_DIR / "registry.json"))
    rows = reg.summary()
    print(f"total trials (parameter sets ever evaluated): {reg.trials}")
    print(pd.DataFrame(rows).to_string(index=False) if rows else "no strategies evaluated yet")
    print("available strategy files:", ", ".join(load_strategies(args.strategies_dir)) or "none")


def add_parser(sub) -> None:
    p = sub.add_parser("lab", help="Strategy research lab")
    lsub = p.add_subparsers(dest="lab_cmd", required=True)
    common = {"--no-update": dict(action="store_true", help="do not refresh history from MT5"),
              "--bars": dict(type=int, default=200_000)}

    def add(name, func, help_, extra=()):
        q = lsub.add_parser(name, help=help_)
        for flag, kw in common.items():
            q.add_argument(flag, **kw)
        q.add_argument("--strategies-dir", default="strategies")
        for a, kw in extra:
            q.add_argument(a, **kw)
        q.set_defaults(func=lambda args, f=func: _run(f, args))

    add("explore", cmd_explore, "scan for statistical anomalies (hypothesis generator)",
        [("--top", dict(type=int, default=30))])
    add("evaluate", cmd_evaluate, "walk-forward evaluation with gates", [("name", dict(help="strategy name or 'all'"))])
    add("finalize", cmd_finalize, "one-time holdout test of a candidate", [("name", dict())])
    add("list", cmd_list, "registry summary")


def _run(func, args) -> None:
    from ..config import load_config
    func(load_config(args.config), args)
