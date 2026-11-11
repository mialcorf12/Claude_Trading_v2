# Feature: Tradovate Bot — Architecture Scaling Cleanup

## Objective
Move three pieces of trading-loop logic that currently live only in the
cron prompt (an LLM re-reading instructions each cycle) into real, tested
Python code in `tradovate_bot/`, so the bot's safety guarantees and signal
rules don't depend on an LLM correctly re-deriving them every cycle, and so
the codebase can scale to more than one account/instrument without
duplicating prompts.

## Why
Recommended by Claude (2026-11-10) when the user asked for scaling cleanup
opportunities; authorized by the user directly ("quiero que inicies a
trabajar los 3 puntos que mencionas de arquitectura para escalar",
2026-11-10). Three concrete gaps found by reading the current code:
1. One-position-at-a-time and the mandatory 16:00-18:00 ET flatten are
   enforced only by the cron prompt instructing an LLM to grep NT8's own
   log file and reason about it each cycle — `executor.py` itself has no
   code path for either. A prompt-wording drift or a skipped step silently
   removes the safeguard.
2. The pullback-over-chase / repeated-reversal-is-chop signal rules
   (established live, 2026-11-10, after two losing trades) exist only as
   prose in a memory file and the cron prompt — re-derived by judgment each
   cycle, not a testable function.
3. `NT8_ACCOUNT_ID`, `NT8_INSTRUMENT`, `ALLOWED_SYMBOL`, `MAX_CONTRACTS`
   etc. are hardcoded module-level globals (env-var-backed) in
   `risk_guard.py`/`executor.py`, so running a second account or instrument
   needs a second process with different env vars, not a parameterized
   call.

## Scope
- New `tradovate_bot/position_state.py`: reads NT8's own per-instance log
  file (`...\NinjaTrader 8\log\*.en.txt`) for the most recent
  `Account='<account>'` + `Market position=` line, returns a position
  (side: Flat/Long/Short, qty). Pure parsing function over given log
  content/path, fail-closed (unreadable file or no matching line -> an
  explicit "unknown" result, never guessed Flat or guessed open).
- Wire `executor.process_order`/`run_once`: refuse a new entry when
  `position_state` reports a non-flat (or unknown) position — this is the
  one-at-a-time safeguard, currently manual-only. When
  `risk_guard.check_trading_hours` reports closed AND a position is open,
  automatically send `CLOSEPOSITION` instead of relying on the cron
  prompt's manual step.
- New `tradovate_bot/signals.py`: pure functions over a list of OHLCV bars
  implementing the two rules established live this session (prefer a
  confirmed pullback over chasing; a zone that reversed multiple times
  recently reads as chop) as a single `evaluate_signal(bars) ->
  SignalDecision(direction, confidence, reason)`. No network/LLM calls;
  unit-testable with synthetic bar fixtures based on this session's real
  scenarios (cycle 18's chase, the 18:58 cycle's weak pullback, etc.).
- Config refactor: introduce an explicit config object (account id,
  instrument, allowed symbol, max contracts, etc.) that `risk_guard.
  evaluate_order`/`executor.*` take as a parameter instead of reading
  module-level globals, so two configs could run side by side in the same
  process/tests without monkeypatching module globals.

## Constraints
- No LLM call anywhere in risk_guard/executor/position_state/signals (same
  constraint as the rest of the bot).
- Fail-closed: an ambiguous position-state read or missing config must
  refuse/flatten defensively, never guess.
- Test-first (RED before implementation) for every new function, same
  convention as the rest of this module.
- Pure-Python/file-parsing changes verified with synthetic fixtures, not
  against the live Sim101 account — a live sanity check happens only when
  the user resumes the trading loop.
- Keep the `CONSISTENCY_PCT=2.00` live-edit convention intact across any
  commit touching `risk_guard.py` (revert to `0.40`, stage, commit, restore
  `2.00` in the working tree).

## Tasks
1. [x] `position_state.py` + one-position-at-a-time + auto-flatten-on-close
   wiring into `executor.py`, test-first.
2. [x] `signals.py`: pullback-vs-chase + chop/repeated-reversal detection,
   test-first.
3. [ ] Config object replacing module-level globals in `risk_guard.py` and
   `executor.py`, test-first, keeping back-compatible env-var loading in
   `main()`.

## Acceptance criteria
- [x] New modules have unit tests, test-first (RED confirmed before
  implementation, then GREEN).
- [x] `executor.py` no longer depends on the cron prompt's manual NT8-log
  grep for one-position-at-a-time, nor on its manual pre-close flatten
  step — both exist in code now (the prompt steps become redundant
  backups, not the only enforcement).
- [ ] Full suite green: `python -m pytest tests/tradovate_bot/ -v`.
- [ ] `tradovate_bot/README.md` updated to document the new modules.

## Delivery
Work-unit commits on branch `v2.1` (already the active branch) — no new
branch needed. Conventional commit messages, no AI co-author attribution
(user's standing global instruction).

## Progress
- 2026-11-10: Created. Cron (`c974db5a`) stopped by user request before
  starting this work — no live trading cycles run while this refactor is
  in progress.
- 2026-11-10: Task 1 done. Added `tradovate_bot/position_state.py`
  (`PositionState(side, qty)`, `parse_position_state` pure parser over
  NT8's own `*.en.txt` diagnostic-log text, `find_latest_log_file` +
  `read_position_state` fail-closed file wrapper keyed on `NT8_LOG_DIR`).
  Wired into `executor.py`: `current_position()`, a one-position-at-a-time
  check in `process_order` (rejects when position is Long/Short/unknown,
  logged via the existing `Decision`/`log_decision` pattern), and an
  auto-flatten check at the top of `run_once` (sends `CLOSEPOSITION` and
  logs `status="auto_flatten_market_closed"` when hours are closed and
  position is non-Flat, including "unknown" — treated the same as open,
  never assumed safe). Test-first: 21 new tests (13 in
  `test_position_state.py`, 8 in `test_executor.py`) confirmed RED via
  collection failure (`position_state` module and `current_position`
  didn't exist), then made GREEN. Full suite:
  `python -m pytest tests/tradovate_bot/ -v` → 123 passed, 2 failed — the
  2 failures are the pre-existing, intentional uncommitted
  `CONSISTENCY_PCT=2.00` override in `risk_guard.py` (not touched by this
  task), not a regression.
  Caveat: the log-line format and field regexes (`Account='...'`,
  `Market position=...`, `Quantity=...`) are built from the two sample
  lines quoted in this session's chat, not a fresh sample pulled from a
  real NT8 instance this session — unverified against a live `*.en.txt`
  file before trusting this for the funded account.
- 2026-11-10: Task 2 done. Added `tradovate_bot/signals.py`: pure,
  LLM-free guardrails over a list of OHLC bar dicts, codifying the two
  rules established live this session from root-causing two real losing
  trades. `check_chase_guard(bars, direction) -> GuardResult(flagged,
  reason)` flags a candidate "buy"/"sell" as chasing when the trailing
  8-bar window shows zero retracement and at least 4 consecutive closes
  extending in that direction (cycle 18's exact scenario);
  `check_chop_guard(bars) -> GuardResult(flagged, reason)` flags the
  trailing 9-bar window (~45 min of 5-min bars) as chop when it contains
  2+ swing highs/lows (cycle 20's scenario); `evaluate_signal(bars,
  direction) -> SignalVerdict(clear, direction, reasons)` runs both and
  blocks if either fires, with every firing guard's reason returned.
  Both guards fail closed (flagged=True) on malformed bars, an
  unrecognized direction, or fewer than 5 bars — never a silent "clean"
  for insufficient data. Test-first: 15 new tests in
  `tests/tradovate_bot/test_signals.py`, confirmed RED via collection
  failure (`signals` module didn't exist), then GREEN, all 15 passing on
  the first implementation attempt. Full suite:
  `python -m pytest tests/tradovate_bot/ -v` -> 138 passed, 2 failed —
  the same pre-existing, intentional uncommitted `CONSISTENCY_PCT=2.00`
  override in `risk_guard.py` (not touched by this task), not a
  regression. Caveat: not yet wired into the cron prompt or
  `executor.py` — that integration is a separate decision the user will
  make later; `signals.py` has no caller inside the bot yet, by design.
