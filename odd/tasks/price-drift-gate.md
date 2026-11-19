# Feature: Pre-trade price-drift gate

## Objective
Refuse a market entry when the live price has moved too far from the price the
entry decision was based on, so the executor never trades a setup that no
longer exists.

## Why
2026-11-18 21:58 ET: a BUY 3 MNQ was decided on the 21:55 5m close (27459.5),
but the order filled ~200 s later at 27425.25 (-34.25 pts, 1.02x the 33.5 pt
stop). Stop/target are anchored to the fill, so the bracket moved with it and
the stop hit two minutes later (-$200.50). A deep post-mortem found no code
bug: the executor had no price or staleness check, and the signals guards
only block chase/chop, never verify direction.

## Scope
- New module `tradovate_bot/price_drift.py` (pure function + fail-closed).
- Wire it into `executor.process_order` after the signals guard, before the
  one-position-at-a-time check. Entries only (auto-flatten is untouched).
- Tests: `tests/tradovate_bot/test_price_drift.py` and executor integration.
- Docs: `tradovate_bot/README.md` section; cron prompt note.
- NOT in scope: fill-anchored bracket changes, direction guard, structural
  stops (see Next steps).

## Design
- decision_price = `order["bars"][-1]["close"]`.
- live_price = last bar close of `candles/<SYMBOL>_1m.json` (BOM-prefixed UTF-8,
  bars stamped at bar CLOSE, ET).
- Reject when |live - decision| > 0.25 x `stop_loss_points` (fallback 8.0 pts
  when no stop). Env: PRICE_DRIFT_MAX_FRACTION, PRICE_DRIFT_MAX_POINTS.
- Fail closed: missing/unreadable/malformed file, unparseable bar time, or a
  live bar older than 150 s (PRICE_DRIFT_MAX_AGE_SECONDS), or missing decision
  price -> refuse.
- Known limit: the "live" price is the last COMPLETED 1m close (up to ~60 s
  lag), so this bounds drift but does not see intra-minute moves.

## Tasks
- [x] T1 Tests first (RED): price_drift unit tests + executor integration tests. Observed RED: ImportError for the new module, then 2 failing executor tests.
- [x] T2 Implement `price_drift.py` and wire into `process_order` (GREEN). 14 unit + 3 integration tests.
- [x] T3 Full test suite green (only the 2 known pre-existing risk_guard
      consistency failures allowed). Observed: 190 passed, 2 failed (the known two).
- [x] T4 Docs: README section + this document's progress.
- [ ] T5 Deploy: restart TradovateExecutor service (only while Flat) and verify.
- [ ] T6 Re-schedule the cron (every 10 min at :06,:16,...,:56) with the new prompt.

## Acceptance criteria
- A BUY decided at 27459.5 with the live 1m close at 27438 and stop 33.5 is
  refused with a reason naming the drift.
- Missing / stale / malformed candles file refuses the entry.
- An order within threshold proceeds exactly as before.
- Existing executor tests still pass.

## Route declaration
Inline (sonnet): one new module + a small hook in executor.py, understood
design; the Opus review already did the broad exploration.

## Progress
(2026-11-18) T1-T4 done. Replay of the lost trade against the real 1m data
(bars cut at 21:57, decision close 27459.5, now 21:58:15) is refused: live
27438.00, drift 21.50 pts, limit 8.38. Live candles file passes with drift 0.
Nothing committed yet. T5 (service restart) and T6 (cron) pending.

## Next steps (not requested yet)
Fill-anchored vs decision-anchored bracket check, direction guard in
signals.py, structural stop, re-fetch right before writing orders.json.
