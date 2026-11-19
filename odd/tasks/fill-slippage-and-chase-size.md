# Feature: Post-mortem #2 (fill slippage) and #4 (chase by move size)

## Objective
Finish items 2 and 4 of the Opus post-mortem of the 2026-11-18 loss.

## Why
- #2: stop/target are anchored to the FILL, so slippage between the decision
  and the fill silently moved the whole bracket (27459.5 decided, 27425.25
  filled). The price-drift gate (#1) bounds the drift BEFORE submission; this
  closes the remaining gap AFTER the fill.
- #4: the chase guard counts closes only, so a 54.5 pt two-bar rip (half the
  prior drop) was not a chase.

## Design
#2: after the entry fill is confirmed, slip = |fill - decision price|
(decision price = bars[-1].close). If slip > the drift limit (0.25 x stop, or
8.0 pts without a stop), defensively flatten (CLOSEPOSITION), place NO bracket
and return a refusal; otherwise keep the fill-anchored bracket unchanged.
Fails closed: an unusable decision price flattens. The check lives in
price_drift.py (`evaluate_fill_slippage`) and reuses its limit.

#4: replay FIRST (like the direction guard): variants on the 2026-11-18 candles,
implement only what the evidence supports.

## Replay of the #4 variants (2026-11-19)
Candles/MNQ_5m.json 2026-11-18, 191 ten-bar windows, entry at the next bar open,
1:1 bracket from select_bracket_points, 12-bar horizon, stop first on a same-bar
touch; over the 97 trades the deployed guards (chase + chop + direction) allowed
(net -50.2 pts):

| variant | removed | net of removed |
|---|---|---|
| 2-bar move > 1.0 x avg range | 21 | +150.8 (winners) |
| 2-bar move > 1.2 x avg range | 16 | +234.5 (winners) |
| 2-bar move > 1.5 x avg range | 6 | 0.0 |
| last bar > 0.8 x avg range | 12 | +100.5 (winners) |
| last bar > 1.0 x avg range | 7 | 0.0 |
| bounce recovered > 50% of the counter-leg | 31 | -100.5 |
| bounce recovered > 75% of the counter-leg | 25 | -150.8 (losers) |

The literal "move size" rules proposed by Opus removed momentum winners (the
risk Opus itself flagged), so they are NOT used. The recovered-fraction rule
("late entry into a bounce that is mostly spent") removes losers; 75% is
implemented. After it: 72 trades, 51% win, net +100.5 pts (the code reproduces
the experiment exactly). Caveats: in-sample, one session, overlapping windows,
approximate fills; low confidence, cheap failure mode (a skipped trade).
Note the lost 21:58 BUY is refused by the direction guard (#3), not by this rule
(its recovery was ~55%).

## Tasks
- [x] T1 #2 tests first (RED), `evaluate_fill_slippage` + wired in process_order.
- [x] T2 #4 replay of the size variants (table above).
- [x] T3 #4 implemented the recovered-fraction check inside check_chase_guard
      (5 tests, 3 red first); the literal size variants are NOT implemented.
- [x] T4 Suite 233 passed + 2 known risk_guard failures; TradovateExecutor
      restarted 2026-11-19 00:33:20 ET (PID 7440) while Flat.

## Acceptance criteria
- A fill 34 pts away from the decision price (limit 8.4) is flattened and gets
  no bracket; the log has stage `fill_slippage`.
- A fill within the limit keeps the existing fill-anchored bracket.
- A BUY that arrives after the bounce recovered >75% of the prior down-leg
  (SELL: up-leg) is flagged by check_chase_guard as a late entry; 50% is not.
- Existing executor tests still pass; suite green except the 2 known failures.

## Route declaration
Inline (sonnet): small hook + one pure function; #4 gated by a replay.

## Progress
(2026-11-19) #2 and #4 done and deployed. Not committed yet.
