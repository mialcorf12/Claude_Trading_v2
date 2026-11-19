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
3. [x] Config object replacing module-level globals in `risk_guard.py` and
   `executor.py`, test-first, keeping back-compatible env-var loading in
   `main()`.
4. [x] Wire `signals.py` into `executor.process_order`'s decision flow
   (user request, 2026-11-10: "conecta signals.py al flujo de decisión").
   Every entry order must carry its own `bars` (the OHLC list the caller
   already fetched that cycle); `process_order` runs
   `signals.evaluate_signal(order["bars"], order["side"])` and refuses the
   entry when the verdict isn't clear -- same fail-closed pattern as the
   position-state/telemetry checks, not an optional extra. An order with
   no/malformed `bars` is refused, never silently allowed through
   (`signals.py`'s own guards already fail closed on missing/insufficient
   bars, so no extra special-casing is needed in `executor.py`).

## Acceptance criteria
- [x] New modules have unit tests, test-first (RED confirmed before
  implementation, then GREEN).
- [x] `executor.py` no longer depends on the cron prompt's manual NT8-log
  grep for one-position-at-a-time, nor on its manual pre-close flatten
  step — both exist in code now (the prompt steps become redundant
  backups, not the only enforcement).
- [x] Full suite green: `python -m pytest tests/tradovate_bot/ -v`.
- [x] `tradovate_bot/README.md` updated to document the new modules.
- [x] `signals.py` has a real caller in `executor.process_order`, test-first,
  full suite green, no regression in any pre-existing test's *intent*
  (tests that needed `bars` added to keep exercising what they always
  tested are not a regression; a test whose assertion changed meaning
  would be).

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
- 2026-11-10: Task 3 done. Added `tradovate_bot/config.py`: a frozen
  `BotConfig` dataclass (account_id, instrument, allowed_symbol,
  max_contracts, incoming_dir, outgoing_dir, log_dir,
  telemetry_json_path, telemetry_max_staleness_seconds,
  fill_timeout_seconds, fill_poll_interval_seconds) plus
  `BotConfig.from_env()`, which reproduces executor.py's exact current
  env-var reads/defaults (`instrument`/`allowed_symbol`/`max_contracts`
  aren't env-backed -- they default from `NT8_INSTRUMENT`'s literal value
  and `risk_guard.ALLOWED_SYMBOL`/`MAX_CONTRACTS`). Gave
  `risk_guard.check_position_size` optional `allowed_symbol=None,
  max_contracts=None` kwargs (fall back to the module constants) and
  `risk_guard.evaluate_order` an optional `config=None` param that reads
  `config.allowed_symbol`/`config.max_contracts` via `getattr` (duck-typed
  -- `risk_guard.py` never imports `config.py`, so there's no import
  cycle even though `config.py` imports `risk_guard` for its defaults).
  Gave every `executor.py` function that reads an identity/transport
  global an optional `config=None` param (`build_place_command`,
  `build_bracket_exit_commands`, `fetch_account_state`,
  `current_position`, `wait_for_entry_fill`, `_snapshot_outgoing_files`,
  `process_order`, `run_once`, `poll_outgoing_once`, `main`): when
  `config` is given its fields override the corresponding module global
  for that call only (an explicitly passed `account=` to
  `build_place_command`/`build_bracket_exit_commands` still wins over
  `config.account_id` -- the more specific, already-existing override);
  when omitted/`None`, every module global is read exactly as before.
  `current_position` is only ever called with the `config` kwarg when a
  `config` was actually supplied (never `config=None` explicitly) because
  every existing test monkeypatches it with a plain zero-argument stub --
  passing a keyword arg to that stub would have broken all of them.
  Test-first: wrote `tests/tradovate_bot/test_config.py` (3 tests) and the
  config-related additions to `test_risk_guard.py` (6 tests) and
  `test_executor.py` (5 tests) before any implementation; confirmed RED
  by temporarily moving the not-yet-written `config.py` aside and running
  the new/changed tests (`ImportError`/`TypeError: unexpected keyword
  argument`), then restored `config.py` and implemented the rest to
  GREEN. Full suite: `python -m pytest tests/tradovate_bot/ -v` -> 152
  passed, 2 failed -- the same pre-existing, intentional uncommitted
  `CONSISTENCY_PCT=2.00` override in `risk_guard.py` (not touched by this
  task) as every prior task this session; no previously-passing test
  needed any change, confirming the additive constraint held.
- 2026-11-10: Code review before wiring `signals.py` (user request) found
  that `NT8_LOG_DIR` (new in task 1) was never added to
  `tradovate_bot/RUNBOOK_VPS.md`'s env-var step and was never set on the
  real VPS -- without it, `current_position()` always returns "unknown"
  and `process_order` refuses every new order (fail-closed as designed,
  but silently stops all trading until set). Fixed: added
  `NT8_LOG_DIR = C:\Users\Administrator\Documents\NinjaTrader 8\log` to
  `RUNBOOK_VPS.md`'s Step 5 env-var block with an explicit warning note.
  Everything else reviewed clean: `config=None` paths in `executor.py`/
  `risk_guard.py` read module globals exactly as before, full suite still
  152 passed / 2 known-failed, `git status` shows only the standing
  `CONSISTENCY_PCT=2.00` + task-doc diffs.
- 2026-11-10: Manual validation against the real NT8 log (user request,
  after confirming `NT8_LOG_DIR` is now set at Machine scope via
  `[Environment]::GetEnvironmentVariable(..., "Machine")`). Ran
  `position_state.read_position_state()` with the real log dir and found a
  genuine parsing bug: NT8 writes two line shapes that both match the
  existing regexes -- execution-leg lines (contain `Execution=`, whose own
  `Quantity=`/`Market position=` describe one fill, not the account's net
  position) and net-position lines (contain `Average price=`, the
  authoritative net state). `parse_position_state` took the latest
  matching line regardless of shape; it returned the correct `Flat, qty=0`
  tonight only because NT8 happened to write the net line immediately
  after each leg in every observed sequence -- not a guaranteed ordering.
  Fixed: skip any line containing `Execution=` before matching, so only
  net-position lines are ever read. Test-first: added
  `test_parse_position_state_ignores_trailing_execution_leg_line` and
  `test_parse_position_state_execution_leg_lines_alone_return_unknown`
  using real lines from tonight's live log
  (`log.20261110.00000.en.txt`), confirmed RED (both failed before the
  fix), then GREEN. Full suite: `python -m pytest tests/tradovate_bot/ -v`
  -> 154 passed, 2 failed -- the same pre-existing `CONSISTENCY_PCT=2.00`
  failures, no regressions. Re-verified against the real log after the
  fix: still correctly `Flat, qty=0`.
  Caveat: `signals.py` is still unwired, by design -- this validation
  covered only `position_state.py`/`current_position()` against live
  data, not the signal-guard thresholds, which remain fixture-only.
- 2026-11-10: Task 4 done (user request: "conecta signals.py al flujo de
  decisión"). `normalize_order` now passes an optional `bars` field through
  unchanged (added to the existing pass-through key list alongside
  `stop_price`/`limit_price`/`stop_loss_points`/`take_profit_points`).
  `process_order` runs `signals.evaluate_signal(order.get("bars"),
  order.get("side"))` right after the existing `risk_guard.evaluate_order`
  gate and before the one-position-at-a-time check; a non-clear verdict
  returns a `risk_guard.Decision(False, ...)` refusal naming every fired
  guard's reason (joined with "; "), logged via the existing
  `log_decision`/`extra={"signal_reasons": ...}` pattern -- same
  fail-closed shape as the position-state check right after it. No extra
  "missing bars -> skip the guard" special-casing was added:
  `signals._valid_bars`/the guard functions already fail closed
  (flagged=True) on `bars=None`, malformed bars, or too few bars, so an
  order with no `bars` key is refused by `evaluate_signal`'s own verdict,
  not by any new conditional in `executor.py`. Added `from tradovate_bot
  import signals` to the top-level import. Updated `process_order`'s
  docstring to document the new gate (between the risk_guard paragraph and
  the one-position-at-a-time paragraph, same style). Updated the one stale
  sentence in `signals.py`'s own module docstring ("There is no caller
  inside the bot yet...") to say it is now wired into
  `executor.process_order` -- nothing else in that file touched.
  Test-first: added 3 new tests to `tests/tradovate_bot/test_executor.py`
  (`test_process_order_refuses_entry_with_no_bars`,
  `test_process_order_refuses_entry_when_chase_guard_flags_bars` --
  reusing cycle 18's exact chase fixture --, and
  `test_process_order_proceeds_when_bars_pass_signal_guard`), plus two new
  bar-list fixtures (`CLEAN_SIGNAL_BARS`, adapted from
  `test_signals.py`'s `NEITHER_FIRES_BARS`: one down-close pullback with
  monotonically climbing highs/lows, clears both guards for either "buy"
  or "sell"; `CHASE_FLAGGED_BARS`, copied from `test_signals.py`'s
  `CYCLE_18_CHASE_BARS`). Confirmed RED by temporarily swapping in the
  pre-change `executor.py` (via `git show HEAD:...`) and running just the
  two genuinely-RED new tests: both failed (the no-bars case blew up
  trying to JSON-serialize a `Mock` once it fell through to the OIF write
  with no gate to stop it; the chase-bars case failed for the same
  reason) -- the third new test isn't genuinely RED (clean bars pass
  through identically with or without the gate), so it wasn't used for
  the RED check. Restored the implementation and confirmed GREEN. Went
  through every pre-existing `process_order`/`run_once` test in
  `test_executor.py` one by one and added `"bars": CLEAN_SIGNAL_BARS` to
  13 of them whose `raw_order`(s) needed to reach the one-position-at-a-
  time check or anything after it to keep testing what they always
  tested: `test_process_order_places_order_when_all_checks_pass`,
  `test_run_once_processes_every_order` (both orders),
  `test_process_order_waits_for_fill_then_places_bracket_from_real_fill_price`,
  `test_process_order_flattens_defensively_on_fill_confirmation_timeout`,
  `test_process_order_skips_bracket_when_entry_rejected_by_nt8`,
  `test_process_order_refuses_bracket_when_outgoing_dir_not_configured`,
  `test_process_order_rejects_when_a_long_position_is_already_open`,
  `test_process_order_rejects_when_a_short_position_is_already_open`,
  `test_process_order_rejects_when_position_is_unknown`,
  `test_process_order_allows_when_position_is_flat`,
  `test_run_once_processes_orders_normally_when_within_hours`,
  `test_process_order_with_config_uses_overridden_account_and_instrument`,
  `test_process_order_config_none_behaves_exactly_like_omitting_it`. Five
  tests that reject earlier in `process_order` (risk_guard's own daily-
  loss/trading-hours/oversized-position/missing-telemetry checks) needed
  no change and were individually re-checked to still fail for their
  original reason, not a new one:
  `test_process_order_rejects_when_normalization_fails`,
  `test_process_order_rejects_when_risk_guard_fails`,
  `test_process_order_rejects_when_telemetry_is_missing`,
  `test_process_order_rejects_outside_trading_hours`,
  `test_process_order_rejects_oversized_mnq_position`. The three
  `run_once` closed-market/auto-flatten tests and the three
  `build_place_command`-only config tests needed no change either since
  they never call `process_order` at all (auto-flatten short-circuits
  before order processing; `build_place_command` is called directly).
  Full suite: `python -m pytest tests/tradovate_bot/ -v` -> 157 passed, 2
  failed -- the same pre-existing, intentional uncommitted
  `CONSISTENCY_PCT=2.00` override in `risk_guard.py` (not touched by this
  task), not a regression (154 -> 157 is exactly the 3 new tests added,
  with zero prior tests needing a changed assertion).
