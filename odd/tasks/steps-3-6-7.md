# Feature: Post-mortem follow-ups #3, #6, #7

## Objective
Finish items 3, 6 and 7 of the Opus post-mortem list of the 2026-11-18 loss
(odd/tasks/price-drift-gate.md, odd/tasks/direction-guard.md).

## Scope
- #7 (minor): (a) every decisions.log.jsonl record gets a `stage`, and a final
  `order_sent` record is written when an entry is actually handed to NT8, so
  `allow=True` from the risk_guard stage is no longer mistaken for "order
  placed"; (b) stale signals.py docstring (already gone when evaluate_signal's
  docstring was rewritten for the direction guard: verified, nothing left);
  (c) exporter swap uses File.Replace instead of Delete+Move.
- #6 (cycle prompt): re-read the live 1m price right before writing the order,
  require a one-line thesis naming the trend, confirm placement via the
  `order_sent` record.
- #3 (remaining piece): the "higher low only a few ticks above the pullback
  low" (double bottom) rule from the Opus text. Implemented ONLY if a replay of
  the 2026-11-18 session supports it, like the other two extra rules that were
  dropped.

## Tasks
- [x] T1 #7a tests first (RED: 6 failing with KeyError `stage`), then `stage` on
      every decisions.log record + final `order_sent` record (GREEN).
- [x] T2 #7c exporter File.Replace (C#). NOT compiled or tested here: the user
      must recompile LocalBarExporter in NT8 and re-add it to the chart.
- [x] T3 #6 cycle prompt: required thesis line, one 1m re-read before writing,
      placement confirmed via `order_sent`.
- [x] T4 #3 double-bottom rule replayed with four tolerances: it removes only
      0-5 of 191 windows with mixed results (-67, -50, +17, -50 pts), so there
      is no evidence either way. NOT implemented.
- [x] T5 Suite: 220 passed, 2 known risk_guard failures. README documents the
      stages. TradovateExecutor restarted 2026-11-19 00:21:29 ET (PID 9112).

## Acceptance criteria
- A successful entry leaves rows [risk_guard, ..., order_sent]; any refusal
  leaves a last row whose `stage` names the guard that refused it.
- LocalBarExporter never deletes the file before swapping.
- CYCLE_PROMPT asks for the pre-order re-read, the thesis line and the
  `order_sent` confirmation.
- Suite green except the 2 known risk_guard consistency failures.

## Route declaration
Inline (sonnet): small, understood edits; exporter edit is one block.

## Progress
(2026-11-19) All tasks done. #3 (as specified) was already complete with the
retrace rule; the double-bottom rule is left out for lack of evidence. The
exporter change needs an NT8 recompile. Not committed yet.
