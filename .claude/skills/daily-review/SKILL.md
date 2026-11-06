---
name: daily-review
description: End-of-day review - grade every decision, compare with the plan, update per-setup statistics, save evidence-based lessons. Use when asked to review the trading day.
disable-model-invocation: true
---

Review today's trading after the New York close, following CLAUDE.md. Do not open trades here
(managing an existing position's stop is allowed if something changed materially).

1. Read: `get_account_status`, `get_open_positions`, `get_plan`, `get_recent_cycles` (limit 30),
   `get_trade_history` (limit 50), `get_notes`.
2. For each trade opened or closed today, grade separately:
   - Process: was it a playbook setup, sized by conviction, with a stop at a real invalidation level?
   - Execution: entry timing, stop placement, management.
   - Outcome: the R result. A good process with a loss is still a good trade.
3. Compare the day with the daily plan: did you follow it? Missed valid setups? Took unplanned trades?
4. `get_performance_diagnostics`: calibration of today's win probabilities, signals skipped vs taken,
   MAE/MFE (were stops too tight / exits too early?).
5. Look at per-setup statistics. Flag any setup with a negative average R over a meaningful sample.
6. Save at most 2 notes with `save_note`, only for lessons supported by several trades (cite the
   evidence: number of trades, average R).
7. `log_cycle` (kind `review`) in Arabic: day P/L in % and R, grades, rule breaks (if any), lessons,
   and what to watch tomorrow. Print the same review.
