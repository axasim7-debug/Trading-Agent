---
name: trade-cycle
description: Hourly trading routine - manage open positions, scan the watchlist, execute only playbook setups, log the cycle. Use when asked to run a trading cycle.
disable-model-invocation: true
---

Run one hourly trading cycle on the demo desk, following CLAUDE.md.

1. **Context** (call these first, in parallel): `get_session_info`, `get_account_status`,
   `get_open_positions`, `get_plan`, `get_recent_cycles`, `get_notes`, `list_alerts`.
   - If you were woken by an alert, handle that market first, then dismiss the alert.
   - If the FX market is closed for the weekend: log a one-line cycle and stop.
   - If `new_trades_blocked` is set: only manage open positions.
2. **Catalysts**: `get_economic_calendar` for the next 6 hours (Medium+). If a High-impact release is
   within the next 2 hours on a currency you hold or plan to trade, factor it in explicitly.
   Use web search only when something material may have happened (big unexplained move,
   central-bank day, geopolitical headline), not every cycle.
3. **Manage positions first**: for each open trade, re-check the thesis on H1/H4 candles and decide:
   hold / tighten stop / take profit / close. Act with `modify_trade` or `close_trade` and give the reason.
4. **Scan**: `scan_setups` (coded playbook signals with edge cards) and `get_market_overview`.
   Pick at most 2-3 markets: fired setups, plus markets the daily plan flagged. For each, pull D1/H4
   context and H1 trigger candles (`get_candles`).
5. **Decide** per market: trade / wait (with the exact trigger) / ignore. Only trade a named playbook
   setup with a clear invalidation level. Size from the edge card (see CLAUDE.md "Statistics come first") and your conviction.
   Use `open_trade` with the setup tag and full reasoning (thesis, edge card, catalyst, invalidation,
   target logic), then immediately `set_trade_management` with the plan that fits the setup.
   For "wait" decisions, place a `set_alert` at the trigger level with a precise note.
6. **Log**: one `log_cycle` (kind `cycle`) in Arabic: positions and actions, markets watched with
   trigger levels, upcoming events. Then print the same summary as your final answer.

Be efficient: do not pull candles you will not use, and do not repeat analysis already in the daily plan
unless price has moved materially.
