---
name: daily-prep
description: Pre-London daily preparation - calendar, macro news, bias and key levels per market, scenarios; saves the daily plan. Use when asked to prepare the trading day.
disable-model-invocation: true
---

Prepare today's trading plan before the London open, following CLAUDE.md. Do not open trades here.

1. Read context in parallel: `get_session_info`, `get_account_status`, `get_open_positions`,
   `get_plan` (weekly plan), `get_recent_cycles`, `get_notes`.
2. Calendar: `get_economic_calendar` for the next 24 hours (Medium+), all watchlist currencies.
   List the High-impact events with server times; these define no-trade windows.
3. News and macro: web search for overnight developments relevant to USD, EUR, GBP, JPY and gold
   (central-bank speakers/decisions, inflation/jobs surprises, risk sentiment, yields, geopolitics).
   Use reputable sources, note publication times, and keep only what can move today's markets.
4. Per watchlist market: D1 + H4 candles. Record bias (long / short / neutral) with the reason,
   key levels (prior day high/low, weekly levels, major swing points), D1 ATR, and the playbook
   setups that could trigger today with their exact conditions.
5. Rank markets by quality of opportunity. Note markets to ignore today and why.
6. Save with `save_plan` (scope `daily`), in Arabic, structured as: events and no-trade windows,
   macro backdrop, per-market bias/levels/scenarios, top focus list, risk notes for open positions.
7. `log_cycle` (kind `daily_prep`) with a short summary, and print the plan.
