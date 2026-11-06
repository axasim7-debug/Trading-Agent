"""Live scanning of lab-approved strategies (used by the `scan_setups` tool)."""
from __future__ import annotations

from .features import lab_features, strategy_view
from .registry import Registry
from .strategy import load_strategies


def approved_signals(broker, symbols: list[str], registry_path: str = "lab/registry.json",
                     strategies_dir: str = "strategies", history_bars: int = 6000) -> list[dict]:
    reg = Registry(registry_path)
    approved = reg.approved()
    if not approved:
        return []
    strategies = load_strategies(strategies_dir)
    out = []
    for sym in symbols:
        base = broker.get_bars(sym, "M15", history_bars)
        if base.empty:
            continue
        f = strategy_view(lab_features(base))
        last_t = f.index[-1]
        for name, entry in approved.items():
            st = strategies.get(name)
            if st is None:
                continue
            sig = st.signals(f, **entry["final_params"])
            if sig is None or len(sig) == 0 or last_t not in sig.index:
                continue
            row = sig.loc[[last_t]].iloc[-1]
            rep = entry.get("last_report", {})
            out.append({
                "symbol": sym, "setup": name, "direction": "buy" if row["direction"] > 0 else "sell",
                "bar_time": last_t, "ref_close": float(f.loc[last_t, "close"]),
                "suggested_stop": float(row["stop"]), "suggested_target": float(row["target"]),
                "atr": float(f.loc[last_t, "atr14"]), "session": f.loc[last_t, "session"],
                "regime": f.loc[last_t, "regime"], "source": "lab-approved",
                "edge": {"walk_forward_oos": rep.get("walk_forward_oos"),
                         "holdout": (entry.get("holdout") or {}).get("holdout"),
                         "by_symbol": (rep.get("by_symbol") or {}).get(sym), "validated": True},
            })
    return out
