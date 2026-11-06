---
name: daily-prep
description: Pre-London daily preparation - calendar, macro news, bias and key levels per market, scenarios; saves the daily plan. Use when asked to prepare the trading day.
disable-model-invocation: true
---

Prepare today's trading plan before the London open, following CLAUDE.md. No market orders here;
pending orders at precisely planned levels are allowed.

1. Read context in parallel: `get_session_info`, `get_account_status`, `get_open_positions`,
   `get_plan` (weekly plan), `get_recent_cycles`, `get_notes`.
2. Calendar: `get_economic_calendar` for the next 24 hours (Medium+), all watchlist currencies.
   List the High-impact events with server times; these define no-trade windows.
3. Macro and positioning: `get_intermarket` (yields, curve, dollar, VIX, oil, equities) and
   `get_positioning` for each market (COT, retail). Then web search for overnight developments relevant to USD, EUR, GBP, JPY and gold
   (central-bank speakers/decisions, inflation/jobs surprises, risk sentiment, yields, geopolitics).
   Use reputable sources, note publication times, and keep only what can move today's markets.
4. Per watchlist market: `get_market_structure` (D1 and H4) and `view_chart` (H4). Record bias
   (long / short / neutral) with the reason, key levels (S/R zones, prior day/week high-low, Asia
   range), ADR, and the playbook setups that could trigger today with their exact conditions.
   Place `set_alert`s at the trigger levels, or pending orders where the plan is already precise.
5. Rank markets by quality of opportunity. Note markets to ignore today and why.
6. Save with `save_plan` (scope `daily`), in Arabic, structured as: events and no-trade windows,
   macro backdrop, per-market bias/levels/scenarios, top focus list, risk notes for open positions.
7. `log_cycle` (kind `daily_prep`) with a short summary, and print the plan.
