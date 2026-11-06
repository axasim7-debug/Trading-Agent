"""Always-on watchdog (no LLM): runs between agent cycles.

* applies each position's management plan (break-even, partials, trailing, time stop);
* checks the price alerts the agent set, and wakes the agent when one triggers;
* keeps the journal in sync with stops/targets hit;
* optional hard kill: closes everything when the daily loss limit is breached.

    python -m trading_agent watchdog            # on the MT5 machine, next to Claude Code
"""
from __future__ import annotations

import logging
import subprocess
import time
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from . import management as mg

log = logging.getLogger(__name__)


class Watchdog:
    def __init__(self, broker, journal, run_id: int, risk, cfg, wake: Callable[[str], bool] | None = None,
                 notifier=None, max_hold_hours: int = 72):
        from .diagnostics import Sync
        self.notifier = notifier
        self.sync = Sync(broker, journal, run_id, notifier, cfg.default_management, max_hold_hours)
        self._last_shadow_eval = None
        self.broker = broker
        self.journal = journal
        self.run_id = run_id
        self.risk = risk
        self.cfg = cfg                      # WatchdogConfig
        self.wake = wake
        self._wakes: deque[datetime] = deque()

    # ---- one pass --------------------------------------------------------------------------
    def tick(self) -> dict:
        now = self.broker.now()
        out = {"closed": [], "actions": [], "alerts": []}
        synced = self.sync.run()
        out["closed"] = synced["closed"]
        # evaluate the shadow book every 30 minutes (broker time)
        if self._last_shadow_eval is None or now - self._last_shadow_eval >= timedelta(minutes=30):
            self.sync.evaluate_signals()
            self._last_shadow_eval = now

        for p in self.broker.positions():
            key = mg.plan_key(p.ticket)
            raw = self.journal.get_kv(self.run_id, key)
            plan = mg.ManagementPlan.from_dict(raw) if raw else mg.default_plan(p, self.cfg.default_management)
            try:
                acts = mg.apply(self.broker, self.risk, p, plan, now)
            except KeyError:
                continue
            if acts or raw is None or plan.done != (raw or {}).get("done"):
                self.journal.set_kv(self.run_id, key, plan.to_dict())
            for a in acts:
                self.journal.log_event(self.run_id, now, "mgmt", a)
                if self.notifier is not None:
                    self.notifier.send(f"🛠️ {a}")
                if "stop ->" in a:
                    self.journal.update_levels(self.run_id, p.ticket, sl=p.stop_loss)
            out["actions"] += acts

        triggered = self._check_alerts(now)
        out["alerts"] = triggered

        reason = self.risk.block_reason() or ""
        if self.cfg.close_all_on_daily_loss and reason.startswith("daily loss"):
            for p in self.broker.positions():
                if self.broker.close_position(p.ticket).ok:
                    self.journal.log_event(self.run_id, now, "mgmt", f"#{p.ticket} closed: {reason}")

        if triggered and self.wake:
            ctx = "; ".join(f"alert #{a['id']} {a['symbol']} {a['condition']} {a['price']} "
                            f"(now {a['trigger_price']}): {a['note']}" for a in triggered)
            self._maybe_wake(now, ctx)
        return out

    def _check_alerts(self, now: datetime) -> list[dict]:
        fired = []
        for a in self.journal.alerts(self.run_id, "active"):
            if a["expires"] and str(now)[:16] > a["expires"]:
                self.journal.set_alert_status(a["id"], "expired")
                continue
            try:
                bid, _ = self.broker.quote(a["symbol"])
            except KeyError:
                continue
            hit = bid >= a["price"] if a["condition"] == "above" else bid <= a["price"]
            if hit:
                self.journal.set_alert_status(a["id"], "triggered", now, bid)
                a.update(status="triggered", trigger_price=bid)
                self.journal.log_event(self.run_id, now, "alert", f"{a['symbol']} {a['condition']} {a['price']}: {a['note']}")
                if self.notifier is not None:
                    self.notifier.send(f"🔔 تنبيه {a['symbol']} {a['condition']} {a['price']} (الآن {bid}): {a['note']}")
                fired.append(a)
        return fired

    def _maybe_wake(self, now: datetime, context: str) -> None:
        while self._wakes and now - self._wakes[0] > timedelta(hours=1):
            self._wakes.popleft()
        if len(self._wakes) >= self.cfg.max_wakes_per_hour:
            log.info("wake suppressed (rate limit); alerts stay visible to the next cycle")
            return
        if self.wake(context):
            self._wakes.append(now)

    # ---- loop ------------------------------------------------------------------------------
    def run(self) -> None:  # pragma: no cover - live loop
        log.info("Watchdog running (run %d). Create a STOP file to stop.", self.run_id)
        while not Path("STOP").exists():
            try:
                res = self.tick()
                for a in res["actions"]:
                    log.info(a)
            except Exception:
                log.exception("watchdog tick failed")
            time.sleep(self.cfg.poll_seconds)


def command_waker(template: list[str]) -> Callable[[str], bool]:  # pragma: no cover - spawns a process
    """Start the agent routine in the background, e.g. the Claude Code runner script."""
    def wake(context: str) -> bool:
        args = [a.replace("{context}", context.replace('"', "'")) for a in template]
        try:
            subprocess.Popen(args, cwd=Path.cwd())
            log.info("woke agent: %s", context)
            return True
        except OSError as e:
            log.error("could not wake agent: %s", e)
            return False
    return wake

