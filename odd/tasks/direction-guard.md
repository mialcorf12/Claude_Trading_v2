# Feature: Direction guard in signals.py

## Objective
Make `signals.evaluate_signal` check that the candidate direction is supported
by the structure of the window, instead of returning `clear` for both buy and
sell on the same bars.

## Why
2026-11-18 21:58 ET BUY 3 MNQ (lost $200.50): the window was a failed rally
(103 pt rally 27407 -> 27510 fully retraced to a new low 27398.75, lower
highs after), yet `evaluate_signal` returned clear for BOTH buy and sell
because the chase/chop guards never look at direction. The Opus post-mortem
listed this as recommendation #3 (design gap, not a bug).

## Scope
- New `check_direction_guard(bars, direction)` in `tradovate_bot/signals.py`,
  folded into `evaluate_signal` as a third guard (reasons order: chase, chop,
  direction).
- Tests: `tests/tradovate_bot/test_signals.py` (+ executor integration).
- Docs: signals.py module/function docstrings, CYCLE_PROMPT.md wording.
- NOT in scope: chase-by-size (#4), structural stop (#5), fill-anchored
  bracket (#2), minor fixes (#7).

## Design (window = trailing 9 bars, min 5)
BUY: swing high = last bar with the highest high; origin = lowest low BEFORE
it; leg = high - origin. Refuse ONLY when the leg is meaningful
(>= 1.0 x average bar range) and the lowest low AFTER the swing high is at or
below the origin (rally fully retraced = failed rally, not a pullback). SELL
mirrors it with highs/lows swapped. Fail closed on bad direction, malformed
bars or too few bars. Not flagged: a move still extending (the chase guard owns
that), no prior leg, or a leg too small to be a trend.

## Scope decision (2026-11-19, from a replay)
A first version also refused "no prior leg" and "leg too small". Replay of the
2026-11-18 session (candles/MNQ_5m.json, 191 ten-bar windows, entry at the next
bar open, 1:1 bracket from select_bracket_points, 12-bar horizon, stop first on
a same-bar touch) over the windows the old guards let through:

| group | trades | net |
|---|---|---|
| A fully retraced (removed) | 30 | -150.8 pts |
| B no prior leg (removed) | 30 | +234.5 pts |
| C leg too small (removed) | 4 | +67.0 pts |
| kept | 56 | -385.2 pts |

B and C removed winners, so only A is enforced. Caveats: in-sample, one
session, overlapping windows (not independent), approximate fills, no costs.
Low confidence. Also note the KEPT group is net negative: the guards alone do
not create an edge.

## Tasks
- [x] T1 Tests first (RED): 13 failing tests observed.
- [x] T2 Implement guard + wire into evaluate_signal (GREEN).
- [x] T3 Adjust the two tests whose contract changes (chop-only -> "a chop
      reason is present"; too-few-bars 2 -> 3 reasons) and add an executor
      integration test. Suite: 214 passed, 2 known risk_guard failures.
- [x] T4 Docs + CYCLE_PROMPT wording.
- [x] T5 Deploy: TradovateExecutor restarted 2026-11-19 00:10:21 ET (PID 7540) while Flat; stderr shows only the startup banner.

## Acceptance criteria
- The 21:15-21:55 window of the lost trade: BUY flagged (fully retraced),
  SELL not flagged by this guard.
- Cycle 23 pullback-continuation window still passes for BUY.
- A SELL on a monotonic rise is NOT flagged (no prior down-leg; see scope
  decision).
- Suite green except the 2 known risk_guard consistency failures.

## Route declaration
Inline (sonnet): one function + wiring, design settled above.

## Progress
(2026-11-19) T1-T4 done, narrowed to the retrace rule after the replay. Deployed (service PID 7540). Not committed yet.
