---
name: weekly-review
description: Weekend review and weekly plan - per-setup statistics, drawdown, rule adherence, macro outlook and the coming week's calendar; saves the weekly plan. Use when asked for the weekly review.
disable-model-invocation: true
---

Run the weekly review and build next week's plan, following CLAUDE.md. No trading.

1. Read: `get_account_status`, `get_trade_history` (limit 100), `get_recent_cycles` (limit 30), `get_notes`.
2. Performance: trades, win rate, average R, total R, max drawdown this week, and per setup tag.
   Separate playbook setups from `experimental-*`. Note sample sizes; do not over-read small samples.
3. Behaviour: rule adherence, unplanned trades, management errors, days you correctly stayed flat.
4. Decisions about the playbook: keep / watch / suspend each setup based on evidence. Promote an
   experimental setup only with 30+ trades and a positive average R.
5. Next week: web search for the macro outlook (central-bank meetings, key data such as CPI, NFP,
   PMIs, rate decisions) and check `get_economic_calendar` (168 hours, High). D1 candles for each market:
   weekly bias and key weekly levels.
6. Save with `save_plan` (scope `weekly`) in Arabic, plus at most 2 `save_note` lessons.
7. `log_cycle` (kind `weekly_review`) and print the review and plan.
