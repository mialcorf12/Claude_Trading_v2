# Post-mortem #5: structural stop -- evaluated, NOT implemented

## Question
Opus recommendation #5 (2026-11-18 post-mortem): replace the fixed 16.75/33.5 pt
stop with one placed behind the swing extreme (plus a buffer) and cut the
quantity to hold the dollar risk. Motivation: the lost BUY's stop sat 7 pts under
the obvious 27398.75 low where stops cluster.

## Method (2026-11-19)
candles/MNQ_5m.json (2026-11-18 06:30 -> 2026-11-19 00:05), 191 ten-bar windows,
the trades the deployed guards allow (chase + late-bounce + chop + direction;
75 trades, both sides), entry at the next bar open, TP = SL (1:1), 12-bar horizon,
stop first on a same-bar touch, MNQ $2/pt/contract. Baseline = the current
bracket (select_bracket_points, qty 3): n=75, 51% win, net +$502, avg +$6.7,
max drawdown $904.

## Replay 1: replace the stop (swing extreme over the last K bars + buffer x avg
range, floor 16.75 pts, cap 50/80 pts, qty = min(3, floor($200 / (2 x SL))))
- K=3 and K=5: worse than the baseline in nearly every setting (net between
  -$79 and -$1050).
- K=9: only matches the baseline (+$501 / +$405 / +$221) by SKIPPING 53-58 of
  the 75 trades (a width cap acts as a filter, not as a better stop), n=17-39.

## Replay 2: keep the fixed stop, skip the trade when that stop would sit INSIDE
the recent swing (not behind its extreme)
| swing window | removed | net of removed |
|---|---|---|
| last 3 bars | 31-41 | +$1005 to +$1206 (winners) |
| last 5 bars | 40-52 | +$502 to +$704 (winners) |
| last 9 bars, no buffer | 55 | -$704 (losers) |
| last 9 bars, 0.25 x avg buffer | 61 | +$100 (neutral) |

The sign flips with an arbitrary window length: that is noise, not a signal.

## Decision
No structural stop and no structural filter. Caveats: one session, 75 trades,
overlapping windows (not independent), approximate fills, no costs. What would
change this: several more sessions of candles (re-run the same two replays; the
scripts were ad hoc and are not saved -- consider saving them as a reusable tool).

## Post-mortem list status
#1 drift gate, #2 fill slippage, #3 direction guard (retrace rule), #4 late-bounce
chase check, #6 cycle prompt, #7 minor fixes: implemented. #5: evaluated, not
implemented (this document).
