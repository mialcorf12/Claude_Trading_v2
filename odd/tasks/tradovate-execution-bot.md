# Feature: Tradovate/NinjaTrader Deterministic Execution Bot

## STATUS AS OF 2026-11-09 (read this first)
The architecture pivoted twice since this doc was first written. The
original plan (direct Tradovate REST/WS API) is a **confirmed dead end**:
Tradovate prop-firm/evaluation accounts cannot get API Access at all (see
Pivot History below). Current plan: **NinjaTrader 8 ATI (Order Instruction
Files) + a read-only NinjaScript AddOn for account telemetry**, running on
a Windows VPS (TradingVPS, Basic tier: 2 cores / 8GB RAM / $35mo) where NT8
is already installed and connected to the Lucid Tradovate account. User
connects to that VPS via Microsoft Remote Desktop ("Windows App" on Mac).
Claude has no remote-execution tool into it and, per the user's 2026-11-09
decision (see Open Questions), none will be set up — deployment/operation
on the VPS is a closed decision: the user runs every step manually via RDP,
following `tradovate_bot/RUNBOOK_VPS.md`. User is migrating this project to
the VPS via `git`.

`risk_guard.py` and the executor's decision logic are unaffected by any of
this churn — they're pure/deterministic and already tested green. What
changes each pivot is only the **transport** (how an approved order
actually reaches the broker) and how account state is read.

## Objective
A standalone, always-on executor that places/closes MNQ orders for
Alberto's Lucid Trading funded account using fixed, non-LLM rules, so it
can run without him watching a screen 24/7. Claude's existing
`day-trader-skill` pack (scanner/analyst/risk/broker) stays the signal
layer; this bot is the execution layer. No live LLM call ever sits in the
order-placement path — only hardcoded arithmetic.

## Why
Lucid Trading explicitly allows EAs/API bots on evaluation and funded
accounts (verified 2026-11-09 via web search — no special approval needed).
An LLM in the execution loop is unacceptably risky (latency, non-determinism,
hallucination) for a funded account with hard-dollar kill switches. Decision:
separate signal (Claude) from execution (deterministic bot).

## Account parameters (Lucid Trading, 25k Flex Eval — confirmed by user 2026-11-09)
- Profit target: $1250
- Max loss limit: $1000, drawdown type = **End of Day** (trailing floor recalculated once per day at EOD balance, never intraday)
- Daily loss limit: $600 (intraday, resets daily)
- Consistency rule: 40% (no single day's profit may exceed 40% of total profit counted toward the target)
- Instrument: MNQ, max 3 contracts per position
- Platform: Tradovate (REST + WebSocket API)

## Pivot history
1. **Direct Tradovate REST/WS API** (`tradovate_bot/client.py`, built 2026-11-09). Dead end: confirmed via web search that Tradovate prop-firm/evaluation accounts (incl. Lucid) cannot register an API key or OAuth client — only live funded brokerage accounts with $1,000+ balance and the API Access add-on qualify. `client.py` is unusable for the real funded account; its 45 passing tests only prove the (irrelevant) mocked-HTTP contract.
2. **Third-party webhook bridge** (TradersPost recommended after research — confirmed Lucid support + externally-callable webhook, unlike PickMyTrade which doesn't list Lucid at all, or Autoview which had no Lucid confirmation). Rejected by user: pricing scheme didn't work for them.
3. **Current plan: NinjaTrader 8 ATI + read-only AddOn.** NT8 is already installed on the user's VPS, connected natively to the Lucid Tradovate account. NT8's Automated Trading Interface (ATI) lets an external process submit orders via Order Instruction Files (OIF) — plain semicolon-delimited text files dropped in `My Documents\<NT Folder>\incoming\oif*.txt` — without porting any decision logic into NinjaScript/C#. Confirmed OIF syntax:
   - `PLACE;<ACCOUNT>;<INSTRUMENT>;<ACTION>;<QTY>;<ORDER TYPE>;[LIMIT];[STOP];<TIF>;[OCO];[ORDER ID];[STRATEGY];[STRATEGY ID]`
   - `CANCEL;;;;;;;;;;<ORDER ID>;;[STRATEGY ID]`
   - `CLOSEPOSITION;<ACCOUNT>;<INSTRUMENT>;;;;;;;;;;`
   - NT8 writes confirmations back to `...\outgoing\` (order state, position, connection state files) — but **no account balance / realized P&L file exists in ATI's outgoing set** (confirmed via NT8 docs).
   - Gap: `risk_guard` needs real EOD balance and realized P&L for the Lucid kill switches. A self-tracked Python P&L ledger was considered and rejected (could drift from the broker's real numbers, exactly where accuracy matters most for the $1000 EOD drawdown floor). Decided instead: a small **read-only NinjaScript AddOn (C#, ~50 lines)** that only reads `Account.RealizedPnL`/`CashValue`/etc. and writes them to a JSON file for Python to poll. No trading logic in that AddOn — it is telemetry only.

## Scope
- `tradovate_bot/` keeps its module boundaries, but the transport changes:
  - Replace `client.py`'s direct Tradovate REST/WS calls with an OIF-writer module (writes PLACE/CANCEL/CLOSEPOSITION command files to NT8's `incoming` folder; tails `outgoing` for order/position/connection confirmations).
  - New: a minimal read-only NinjaScript AddOn (C#) that exports account state (realized P&L today, total, EOD cash balance) to a JSON file on a short interval, for `risk_guard`/`executor` to read as its account-state input.
- `risk_guard` module: unchanged — pure, deterministic functions for daily-loss kill switch, EOD-trailing max-loss kill switch, consistency-rule check, and MNQ position sizing (hard cap 3 contracts). Already test-first, already green.
- `executor`: reads an approved order (same shape as `trader-broker`'s `trader/orders.json`), runs it through `risk_guard` using account state from the AddOn's JSON export, places it only if every check passes via the OIF writer, logs every decision and fill to local state files.
- Runs as a long-lived process on the Windows VPS (NSSM/Task Scheduler, not systemd — Windows host), not a chat-invoked skill.
- Explicitly OUT of scope: the order's trade logic (entry/exit timing) — that stays with the existing `trader-*` skills. This bot only decides "is this order allowed right now" and "send it."

## Constraints
- No LLM call anywhere in the execution path.
- No hardcoded credentials — env vars only (now largely moot for the OIF path itself, since ATI needs no API key; still applies to any remaining config).
- Kill switches must fail closed: on any ambiguous/missing account-state read, refuse to trade rather than guess.
- The NinjaScript AddOn must stay strictly read-only telemetry — no order logic, no decision-making in C#.
- Deployment target is the Windows VPS (TradingVPS Basic), not Mac. Mac is dev/test only for the pure-Python pieces (`risk_guard.py`).

## Open questions
- RESOLVED (2026-11-09): How will Claude actually touch the VPS to help set this up? User decided: no SSH/remote-execution tool will be set up (option (a), OpenSSH, is explicitly rejected). Claude prepares code and the exact runbook (`tradovate_bot/RUNBOOK_VPS.md`); the user runs every step on the VPS via RDP and reports results back here. This is permanent, not a temporary workaround.

## Acceptance criteria
- [x] Unit tests for risk_guard cover: under all limits (pass), daily loss breached, EOD max-loss breached, consistency rule breached, position size over 3 MNQ, ambiguous/missing data (fail closed). Evidence: `python3 -m pytest tests/tradovate_bot/test_risk_guard.py -v` → 24 passed. Still valid — unaffected by the transport pivot.
- [x] Executor rejects an order when any risk_guard check fails, and logs why. Evidence: `tests/tradovate_bot/test_executor.py::test_process_order_rejects_when_risk_guard_fails` and `::test_process_order_rejects_oversized_mnq_position` pass. Still valid; executor will be rewired to the OIF writer but this contract (reject + log) doesn't change.
- [ ] ~~Tradovate client authenticates against demo env...~~ SUPERSEDED — direct API confirmed impossible for prop accounts. `client.py` as written will not be used against the real account.
- [ ] ~~WebSocket listener reconnects...~~ SUPERSEDED — no WebSocket in the ATI/OIF path; replaced by tailing NT8's `outgoing` folder for confirmation files.
- [x] README documents env vars required and how to switch demo → live. Still accurate for the parts that remain (no hardcoded creds), but the demo/live Tradovate env var is moot under ATI — README needs a rewrite pass when the OIF module lands.
- [x] NEW: OIF writer module places/cancels/closes a position via NT8 ATI and the resulting order/position confirmation files are correctly parsed. Evidence: `python -m pytest tests/tradovate_bot/test_oif_writer.py tests/tradovate_bot/test_executor.py -v` → 24 passed. Caveat: `tail_outgoing`'s "parsing" is an intentionally honest generic semicolon-split (filename-convention account/instrument/kind only) — the outgoing files' internal field semantics are still unconfirmed against a real NT8 instance; these confirmations feed fill-logging only, never risk_guard.
- [x] NEW: Read-only NinjaScript AddOn exports account realized P&L (today + total) and EOD cash balance to a JSON file NT8-side, and `executor`/`risk_guard` can read it. `ninjatrader_addon/AccountTelemetryAddOn.cs` + `tradovate_bot/account_state.py` (10/10 tests passing, see below). Caveat: the AddOn itself is **not independently verified against a live/demo NT8 instance** — no NT8 runtime was available to compile/run it this session. Must be sanity-checked on the VPS (including a real day-rollover) before being trusted for the funded account. Fixed 2026-11-09: the AddOn originally exposed its config as `[NinjaScriptProperty]` fields, which NinjaTrader cannot show/edit on a windowless AddOn (confirmed via NinjaTrader's own docs/support forum) — it compiled clean but was permanently stuck on hardcoded defaults with no way to find it "active" in any menu (there was never going to be one). Reworked to read `NT8_ACCOUNT_ID`/`ACCOUNT_TELEMETRY_JSON_PATH`/`ACCOUNT_TELEMETRY_EXPORT_INTERVAL_SECONDS` from environment variables (same ones the Python side already uses) and to log a clear `AccountTelemetryAddOn: started. ...` line to NT8's NinjaScript Output window as the real "is it running" signal.
- [x] NEW: VPS deployment runbook exists covering git clone/pull, Python install, NT8 ATI settings, AddOn install, env var config, and unattended run via NSSM/Task Scheduler. Evidence: `tradovate_bot/RUNBOOK_VPS.md`. Caveat: written without VPS/NT8 runtime access (per the closed remote-access decision) — exact NT8 ATI menu wording and the AddOn's day-rollover behavior still need the user to verify on the real machine; the runbook says so explicitly.
- [ ] NEW: Whole stack (NT8 + AddOn + Python executor) actually running unattended on the Windows VPS without manual restarts. Not started — the runbook above exists, but no one has executed it on the real VPS yet.

## Tasks
1. [x] Tradovate REST/WS client — `tradovate_bot/client.py`. Built, tested against mocks, but SUPERSEDED (dead end for prop accounts). Kept in repo as reference/for a possible future live non-prop Tradovate account; not deleted.
2. [x] risk_guard module + unit tests (test-first). `tradovate_bot/risk_guard.py` + `tests/tradovate_bot/test_risk_guard.py`, tests written before implementation, 24/24 passing. Still the current, correct decision engine.
3. [x] executor main loop. `tradovate_bot/executor.py` rewired to the OIF writer + AddOn JSON reader (`oif_writer`/`account_state` modules); no longer imports `client.py`/websockets at all. `tests/tradovate_bot/test_executor.py` rewritten to match (14/14 passing).
4. [x] Process/runbook docs — `tradovate_bot/README.md` rewritten for the ATI/OIF path (env vars, OIF grammar, AddOn JSON contract, unverified-items list). `tradovate_bot/RUNBOOK_DEMO.md` given a superseded banner pointing at the README instead of a full rewrite (it's entirely about the dead direct-API/Mac path).
5. [x] NEW: OIF writer module (replaces client.py's order-placement role). `tradovate_bot/oif_writer.py` + `tests/tradovate_bot/test_oif_writer.py`, 10/10 passing.
6. [x] NEW: NinjaScript AddOn for read-only account telemetry. `ninjatrader_addon/AccountTelemetryAddOn.cs` — unverified against live NT8 (see acceptance criteria caveat above).
7. [x] NEW: VPS deployment runbook (git clone/pull on Windows VPS, Python install, NT8 ATI settings to enable, AddOn install into NT8, how to run executor unattended on Windows — NSSM or Task Scheduler, not systemd). `tradovate_bot/RUNBOOK_VPS.md`. Written for the user to execute manually via RDP; not run against the real VPS yet.

## Cron procedure (current, authoritative — read this before the next
## time the overnight trading cron is (re)created, whether via /loop or
## CronCreate directly)
The cron prompt itself has only ever existed as inline prompt text passed
to CronCreate at schedule time, recreated from scratch (and re-copied by
hand from the previous version plus whatever changed) every time it was
updated -- there has never been a standalone prompt file in this repo.
That's why it has drifted before (see the 2026-11-10 incident below where
a branch checkout under this session reverted three fixes that had
already shipped). This section is the fix for that: it's the one place
that should be kept current, so the next cron creation copies from here
instead of from memory.

1. Check current position first. As of task 1
   (`odd/tasks/tradovate-architecture-scaling.md`), this is now also
   enforced automatically in code: `executor.process_order` refuses a new
   entry whenever `position_state.read_position_state()` reports anything
   other than Flat, and `executor.run_once` auto-sends CLOSEPOSITION when
   the market is closed and a position is open. The cron prompt's own
   manual check (tailing the newest `*.en.txt` under `NT8_LOG_DIR` for
   `Account='Sim101'` + `Market position=`) is now a backup, not the only
   enforcement -- keep doing it anyway, since it lets the LLM skip the
   cycle early with a clear status line instead of relying on the code
   path's refusal to notice.
2. If flat and within trading hours: fetch fresh MNQ bars from TradingView
   (`mcp-tv-get-ohlcv` / `mcp-tv-get-symbol-data`, symbol `CME_MINI:MNQ1!`,
   5-minute bars) -- not Yahoo (confirmed stale by ~11-15 min on
   2026-11-10, see Progress). Confirm `update_mode: "streaming"` and that
   the latest bar's timestamp is recent before trusting the data.
3. Judge BUY/SELL from genuine structure in the bars: prefer an entry on a
   confirmed pullback over chasing an already-extended move with zero
   retracement (cycle 18's loss), and read a zone that has already
   reversed direction multiple times recently as chop, not a clean signal
   (cycle 20). As of task 2/4, both rules are now ALSO enforced in code --
   `tradovate_bot/signals.py`'s `evaluate_signal(bars, direction)` runs
   automatically inside `executor.process_order` and will refuse the
   order even if this step's judgment misses it. The cron's own read is
   still what picks the candidate direction (or skips); code is the
   backstop, not a replacement for actually looking at the bars.
4. If the data gives no clear directional edge, skip the trade this cycle
   (log it, reschedule) rather than forcing one.
5. If there's a clear signal, compute stop_loss_points/take_profit_points
   for 3 contracts ($2/contract/point MNQ multiplier, $6/point total; TP
   $100 -> 16.75 pts, SL $200 -> 33.25 pts), tick-rounded, oriented for
   BUY vs SELL.
6. Write the order JSON to `trader/orders.json` (or the one-shot path used
   for a manual/off-cycle run) with: `symbol` "MNQ", `side`, `qty` 3,
   `type` "market", `stop_loss_points`, `take_profit_points`, **and now
   `bars`: the exact same OHLC bar list fetched in step 2, in the shape
   `tradovate_bot/signals.py` expects (a list of dicts with `open`,
   `high`, `low`, `close` finite numbers, most recent last)** -- added
   2026-11-10 when `signals.py` was wired into `process_order`
   (`odd/tasks/tradovate-architecture-scaling.md` task 4). **Required,
   not optional: `process_order` now fails closed and refuses any entry
   order with no/malformed `bars`, exactly like a missing telemetry file
   or an unreadable position log.** Omitting this field is the same class
   of mistake as the `NT8_LOG_DIR` deployment gap -- the bot will reject
   every order, silently, until this is fixed.
7. Run it through the executor (same invocation used for prior cycles --
   see Progress for the exact env vars/script path used that session).
8. Verify the real outcome against NT8's own log and the outgoing/incoming
   folders -- never report success without reading NT8's own confirmation.
9. If risk_guard rejects (daily loss, EOD floor, consistency, trading
   hours, or now the signals guard) log the exact reason and respect it --
   these are hard protections, not something to route around. A signals-
   guard rejection specifically means the trade would have been
   chasing/chop by the same rule this cron is already supposed to be
   applying in step 3 -- if that happens often, it's a sign step 3's own
   judgment and the code guard disagree, worth a look, not a bug to
   route around.
10. Sanity-check the branch and that `check_trading_hours`/`signals`/
    `position_state` are actually present in the live `executor.py`/
    `risk_guard.py` before trusting the code (added after the 2026-11-10
    branch-checkout incident below, where a `git checkout main` under
    this session silently reverted three already-shipped fixes).
11. Append one short line per cycle to this doc's Progress section
    (timestamp, decision, outcome) and the full structured record to
    `tradovate_bot/state/cycles.log.jsonl` via `executor.log_cycle(...)`.
12. Clean up any one-shot order file after each run.

## Progress
- 2026-11-09: Created. First implementation pass (direct Tradovate REST/WS
  client + risk_guard + executor), 45 tests passing against mocks.
- 2026-11-09: Discovered direct API is a dead end for prop accounts.
  Researched bridges, recommended TradersPost — user rejected (pricing).
  Researched PickMyTrade (doesn't list Lucid) — rejected.
- 2026-11-09: Landed on NinjaTrader 8 ATI (OIF) + read-only NinjaScript
  AddOn for account telemetry, running on the user's existing NT8 VPS.
  Confirmed VPS plan (TradingVPS Basic, 2 core/8GB, upgraded from Starter
  for 24/7-unattended headroom) is adequate; VPS tier doesn't affect code
  correctness, only operational margin.
- 2026-11-09: User is migrating this project to the VPS via `git`. Asked
  Claude to already be set up on the VPS before continuing to code —
  blocked on remote-access method (see Open Questions). This status
  update written so it travels with the git migration.
- 2026-11-09: User confirmed the VPS is already provisioned (Basic tier)
  and asked to continue coding the parts that don't require touching the
  VPS. Landed on branch `feature/tradovate-oif-execution` (local only, not
  pushed): `tradovate_bot/oif_writer.py` (OIF PLACE/CANCEL/CLOSEPOSITION
  command builder + atomic incoming-folder writer + generic outgoing-folder
  tailer), `tradovate_bot/account_state.py` (fail-closed AddOn telemetry
  reader), `executor.py` rewired to drop `client.py`/websockets entirely
  and use the two new modules, `ninjatrader_addon/AccountTelemetryAddOn.cs`
  (read-only telemetry AddOn), and a README/RUNBOOK docs pass. Full suite:
  `python -m pytest tests/tradovate_bot/ -v` → 69 passed: 24 risk_guard
  (unchanged) + 11 historical client.py (still green but irrelevant to the
  live path) + 10 new oif_writer + 10 new account_state + 14 rewired
  executor. Explicitly NOT done this session: tasks 7
  (VPS unattended run) and 8 (remote-access method) — both require
  actually touching the VPS, which stayed out of scope per the user's
  instruction. The NinjaScript AddOn is also explicitly unverified against
  a live/demo NT8 instance (no NT8 runtime available here) — flagged in
  both the acceptance criteria and the README's "What is NOT verified yet"
  section.
- 2026-11-09: User decided task 8 is unnecessary and removed it — no
  SSH/remote-execution tool will ever be set up for this VPS; the user runs
  every deployment/operational step manually via RDP. Resolved the Open
  Questions entry accordingly. Executed task 7: wrote
  `tradovate_bot/RUNBOOK_VPS.md` (git clone/pull, Python install, NT8 ATI
  enablement, AddOn install, env var config, NSSM unattended run). This is
  documentation only — nobody has run it against the real VPS yet, so the
  final "whole stack runs unattended" acceptance criterion stays open until
  the user executes it and reports back.
- 2026-11-09: User compiled `AccountTelemetryAddOn.cs` in NT8 with no
  errors but couldn't find it active in any menu. Researched NinjaTrader
  8's actual AddOn lifecycle (NinjaTrader's own docs + support forum,
  cited in the fix commit) instead of guessing: a headless/windowless
  AddOn needs no menu entry and runs automatically at startup — that part
  was never broken. The real defect: `[NinjaScriptProperty]` values are
  only editable from inside an AddOn's own custom window per NinjaTrader
  support, so this windowless AddOn's `AccountName`/`OutputJsonPath`/
  `ExportIntervalSeconds` could never be changed from NT8's UI as the
  README previously (incorrectly) instructed. Fixed: the AddOn now reads
  `NT8_ACCOUNT_ID`/`ACCOUNT_TELEMETRY_JSON_PATH`/
  `ACCOUNT_TELEMETRY_EXPORT_INTERVAL_SECONDS` from environment variables
  (shared with the Python side), logs a `AccountTelemetryAddOn: started.`
  line to NT8's NinjaScript Output window so "is it running" has an actual
  signal to check, and matches account names case-insensitively. README
  and RUNBOOK_VPS.md corrected to match. Still not independently verified
  against a live NT8 instance — the user needs to recompile, set/confirm
  the env vars, restart NT8, and check NinjaScript Output per the updated
  runbook.

Still true regardless of transport: `risk_guard.py` is pure, deterministic,
fail-closed, test-first, and already covers every Lucid risk-rule case —
24/24 of its own tests passing, unaffected by any pivot. The OIF writer and
AddOn telemetry reader are now implemented and unit-tested (fail-closed
contracts verified), and `executor.py` no longer depends on `client.py` or
any network/websocket library at all.

Not yet done: actually running the whole stack unattended on the real VPS
(task 7's runbook exists but is unexecuted). Task 8 is resolved/removed —
no remote-execution tool will be set up; the user operates the VPS
manually via RDP. Also not yet done: verifying the NinjaScript AddOn's
actual runtime behavior (day-rollover correctness in particular) against a
real NT8 instance, and verifying NT8 ATI's real outgoing-file field layout.
- 2026-11-10: Discovered `process_order` only ever sent a single OIF PLACE
  command for the entry -- `stop_loss_stop_price`/`take_profit_limit_price`
  normalized onto the order but were never read anywhere, so no stop-loss
  or take-profit was ever actually attached in NT8. Implemented test-first:
  `build_bracket_exit_commands` builds the stop (STOP) and/or target
  (LIMIT) OIF commands on the side opposite the entry, same qty, sharing
  one OCO group id (`OCO-<uuid8>`) when both legs are present so a fill on
  either cancels the other in NT8; returns `[]` when the order carries
  neither price. `process_order` now writes the entry command plus
  whatever `build_bracket_exit_commands` returns, logging one fill record
  per OIF file. Also fixed a latent bug while here: the entry's own
  `stop_price` (for a STOP-type entry trigger) previously fell back to
  `stop_loss_stop_price`, conflating an entry trigger with a position's
  protective exit price -- removed that fallback. `python -m pytest
  tests/tradovate_bot/ -v` → 73 passed (4 new bracket tests + all 69
  pre-existing, including the single-command-when-no-bracket-prices cases,
  unchanged). Not yet done: this is unverified against a real NT8 instance
  -- in particular, whether NT8 ATI's OCO field actually links two
  separately-submitted PLACE commands by a client-supplied id the way this
  code assumes, versus requiring NT8's own order id from a prior
  confirmation. Needs a real Sim101 test before being trusted for an
  OCO-bracketed order on the funded account.
- 2026-11-10: Live-tested a real BUY 3 MNQ bracket order against Sim101.
  `oif_writer.write_command`'s atomic temp-then-rename approach (both the
  in-folder and parent-folder variants tried first) turned out incompatible
  with NT8's ATI watcher -- fixed to write directly to the final filename,
  no temp file, no rename (see oif_writer.py and test_oif_writer.py). With
  that fixed, the order actually reached NT8 and surfaced two more real
  bugs: (1) `"STOP"` is not a valid OIF order-type literal -- NT8 rejected
  it outright ("holds invalid order type parameter 'STOP'"); fixed to
  `"STOPMARKET"`. (2) the generic root symbol `"MNQ"` is not a valid NT8
  instrument -- market data subscription failed ("Symbol is inaccessible"
  / "UnknownSymbol"), which then self-rejected both submitted orders
  ("There is no market data available to drive the simulation engine.").
  Fixed by adding a hardcoded `NT8_INSTRUMENT = "MNQ 12-26"` constant in
  `executor.py`, used only for the OIF INSTRUMENT field, decoupled from
  `risk_guard.py`'s `ALLOWED_SYMBOL = "MNQ"` (which stays the generic root
  for risk-check purposes). User explicitly chose a hardcoded constant
  (updated by hand each quarterly roll) over an env var or an automatic
  roll-date calculation, for consistency with risk_guard.py's other
  constants. Both orders self-rejected cleanly before any fill; the
  account stayed flat throughout, no cleanup needed. `python -m pytest
  tests/tradovate_bot/ -v` → 74 passed. Not yet re-tested live after this
  fix.
- 2026-11-10: Re-tested live after the `"STOPMARKET"`/`NT8_INSTRUMENT`
  fix: a BUY 3 `MNQ 12-26` bracket order went through end to end on
  Sim101 -- entry filled at 27936.75 (`Market position=Long`), stop-loss
  (`STOPMARKET` 27926.00) and take-profit (`LIMIT` 27976.00) both
  `Accepted`/`Working`, correctly sharing one NT8-side `Oco='OCO-...'`
  group. Confirms the client-supplied OCO id question from the first
  bracket-OCO entry above: NT8 does honor it. **As of this writing there
  is an open Long 3 `MNQ 12-26` position on Sim101** with that bracket
  still working -- check `NT8_OUTGOING_DIR`/NT8's own Sim101 position
  display before assuming flat in a future session.
- 2026-11-10 00:07 ET: Started an hourly overnight trading loop (local
  CronCreate job `cbb6f9d1`, "7 * * * *", session-only -- dies if this
  Claude session closes, auto-expires after 7 days). Each cycle: check
  Sim101's current `Market position` from NT8's own log first (no
  automated one-at-a-time code exists yet -- this manual log check IS the
  safeguard the user asked for), skip if not flat, else fetch fresh
  MNQ=F/NQ=F data, judge BUY/SELL from genuine structure (skip rather than
  force a trade on weak/missing data), size a 3-contract bracket off a
  fresh quote, place it the same way as tonight's manual runs, and verify
  against NT8's log before reporting anything.
  First cycle (run immediately, not waiting for the first cron fire):
  checked position -- the prior Long 3 position (entry above) had already
  closed via its take-profit at 23:58:37 ET: 3x 1-contract fills at
  27976.00 (exactly the TP level), realized ~+$235.50 gross (3 x 39.25 pts
  x $2/pt) -- more than the ~$100 target, because the entry filled at
  27936.75, well below the 27959.25 reference quote the bracket was sized
  from (slippage between quote-time and fill-time can make realized P&L
  diverge meaningfully from the intended $ target when TP/SL are priced
  off a pre-fill quote, not the actual fill). Account back to flat.
  Then fetched fresh data for a new entry and hit a real data gap: right
  at the ET midnight rollover, Yahoo's NQ=F 5m/1d chart returned no
  `timestamp` array at all, and the 2d/5d range showed only sparse bars
  with a ~1h45m hole (real bars to 22:15 ET, then nothing until a single
  23:59 bar) -- not enough structure to judge direction honestly. Skipped
  the trade this cycle rather than force one; next cron fire (~1:07 ET)
  retries once the post-midnight bar feed should have normalized.
  Second cycle (2026-11-10, ~01:21 ET): checked position first -- still
  flat (no entries placed since the prior cycle). Fetched fresh NQ=F/MNQ=F
  5m/1d bars; the midnight data gap was gone, full bar series from 00:05
  to 01:21 ET. Read: a clean rally from ~27968 to a ~28042 high by 00:40
  ET (+74 pts), then a reversal that gave back more than half the move,
  dropping to ~27986.5 by 01:10 ET, then a weak bounce to ~28005 before
  settling at 27999-28001 (last/MNQ=F quote: 28001.0, essentially flat on
  the day at -0.05%). The last ~25 minutes (00:55-01:21 ET) oscillated in
  a ~48-point range (27986.5-28034) with no clean directional follow-
  through in either direction -- a spike-and-fade with no fresh edge, not
  a tradeable structure. Skipped the trade (per step 3: no clear
  directional edge) rather than force a BUY or SELL on a choppy read; no
  order file written. Next cron fire (~2:07 ET) re-evaluates fresh.
  Third cycle (2026-11-10, ~02:21 ET): checked position first -- still
  flat. Fresh NQ=F/MNQ=F bars showed a genuine downtrend over the prior
  ~90 min: high ~28042 at 00:40 ET, lower highs/lower lows down to a
  27937.25 low at 02:10 ET (-105 pts), then a decelerating bounce to
  27971/27970.5 (current) -- read as SELL on the bounce, with-trend.
  Computed bracket off the 27970.5 MNQ=F reference: stop_loss_stop_price
  28003.75 (+33.25 pts), take_profit_limit_price 27953.75 (-16.75 pts),
  qty 3, side sell. **risk_guard rejected the order**: "consistency rule
  breached: today's profit 235.50 exceeds 40% of total 235.50" -- the
  Lucid Flex Eval's 40% single-day-concentration rule (risk_guard.py's
  `CONSISTENCY_PCT`), structurally breached right now because the only
  realized profit on this account ever (the ~$235.50 TP close from
  cycle 1) is 100% of lifetime total, so any same-day profit will fail
  this check until more trading days dilute the ratio below 40%. No OIF
  command was written (write_command never called -- confirmed: incoming
  folder empty, NT8 log unchanged since 23:58:37 ET). Per the hard-limit
  rule, no further entries will be proposed tonight; risk_guard will
  continue to self-enforce this same rejection on every future cron fire
  until the ratio naturally resolves (new trading days add to
  `total_realized_profit`'s denominator) -- no code change or override
  applied, by design.
  Fourth cycle (2026-11-10, ~03:31 ET): checked position -- still flat.
  Checked `account_telemetry.json` directly before building a new signal:
  unchanged since cycle 3 (daily_profit_today=235.50,
  total_realized_profit=235.50, same 100% concentration). Since the
  cycle-3 consistency-rule rejection is structural and depends only on
  this telemetry (not on the new order's direction/price), skipped
  re-fetching Yahoo data and rebuilding a bracket this cycle -- it would
  be rejected the same way regardless of BUY/SELL. No order attempted,
  no OIF written. Standing hard-limit block from cycle 3 still in effect;
  will keep checking telemetry each cycle rather than re-deriving a full
  signal while it stays unchanged.
  Fifth cycle (2026-11-10, ~04:30 ET): checked position -- still flat.
  Telemetry still unchanged (daily_profit_today=235.50,
  total_realized_profit=235.50). Same structural consistency-rule block
  from cycle 3 still in effect; skipped rebuilding a signal for the same
  reason as cycle 4. No order attempted.
  Sixth cycle (2026-11-10, ~05:30 ET): checked position -- still flat.
  Telemetry still unchanged (daily_profit_today=235.50,
  total_realized_profit=235.50). Same consistency-rule block still in
  effect; no order attempted.
  Seventh cycle (2026-11-10, ~06:30 ET): checked position -- still flat.
  Telemetry still unchanged (daily_profit_today=235.50,
  total_realized_profit=235.50). Same consistency-rule block still in
  effect; no order attempted. This block will only clear once a new
  trading day's activity changes the profit/total ratio (EOD rollover
  and/or a new closed trade) -- it is expected to persist for the rest
  of tonight's remaining cycles.
  Confirmed (user asked where this lives): `check_consistency` in
  `tradovate_bot/risk_guard.py:87-99` (CONSISTENCY_PCT=0.40, line 28).
  The EOD/day-boundary reset is NOT in Python -- it's in the NinjaScript
  AddOn, `ninjatrader_addon/AccountTelemetryAddOn.cs:194-212`
  (`ResetDayBoundaryIfNeeded`), which rolls the trading day over at
  17:00 ET (not VPS midnight) and recalculates `eod_balance_last_close`
  / resets `daily_profit_today` at that boundary.
  Eighth cycle (2026-11-10, ~07:30 ET): checked position -- still flat.
  Telemetry still unchanged (daily_profit_today=235.50,
  total_realized_profit=235.50). Same consistency-rule block still in
  effect; no order attempted.
  **Fix (2026-11-10, user-identified issue):** `check_consistency` in
  `risk_guard.py` compared `daily_profit_today` against 40% of
  `total_realized_profit` (profit realized so far), which self-defeats on
  any account's first-ever profitable day -- that day's profit is always
  100% of the near-zero running total, so the rule could never be
  satisfied until several more trading days diluted the ratio (exactly
  what blocked every cycle tonight after cycle 1's TP close). User
  confirmed via AskUserQuestion the correct formula compares against the
  fixed `PROFIT_TARGET` ($1250) instead: `daily_profit_today > 0.40 *
  PROFIT_TARGET` (threshold $500), independent of total_realized_profit.
  Applied test-first (RED: 3 updated/new tests failed against old code --
  boundary-at-$500 test, the overnight-lockout regression test, and the
  direct check_consistency test; GREEN: all 76 tests in tests/tradovate_bot/
  pass after the fix). total_realized_profit is still validated as a
  required finite-number field (fail-closed preserved) even though it no
  longer drives the comparison. Changed: `tradovate_bot/risk_guard.py`
  (`check_consistency` + module docstring),
  `tests/tradovate_bot/test_risk_guard.py`. With this fix, tonight's
  $235.50/$235.50 state would now PASS (235.50 < 500) -- the next cron
  cycle will re-evaluate a fresh signal for real instead of self-blocking.
  Committed as `02e410a`.
  Ninth cycle (2026-11-10, ~08:21 ET): checked position -- still flat.
  Telemetry unchanged (235.50/235.50) but now PASSES the fixed
  consistency rule (235.50 < 500). Fetched fresh NQ=F/MNQ=F data: the
  last ~2 hours (06:30-08:21 ET) show price oscillating in an ~80-point
  range (27877-27961) with no sustained breakout either way -- a dip to
  27877.5 (near the day's 27844.5 low) at 08:10 bounced to 27917.75 then
  fell back to 27892.75 (current), a failed/partial bounce rather than a
  clean continuation. Read as choppy consolidation at session lows, not
  a confident directional edge. Skipped the trade (step 3) rather than
  force one; this was a genuine market-data judgment call, not a
  risk_guard rejection. No order attempted.
  Loop rescheduled (2026-11-10, ~09:15 ET, user request): cadence changed
  from hourly to every 30 min. Cancelled cron `cbb6f9d1`, created
  `c9e1834b` ("7,37 * * * *", same offset convention). Separately, user
  set `CONSISTENCY_PCT = 2.00` directly on disk (intentional,
  uncommitted, left as-is per explicit instruction) -- loosens the
  consistency threshold from $500 to $2500, but still protects against
  extreme single-day concentration since `daily_profit_today` is the
  day's *cumulative* realized P&L, not a per-trade figure.
  Tenth cycle (2026-11-10, ~09:19 ET, first at the new 30-min cadence):
  checked position -- flat. Fetched fresh MNQ=F/NQ=F data: a sharp
  breakdown at 08:30 ET (27920 -> 27706.5 in a single 5-min bar, ~213
  pts), continuing to a low of ~27652.5 by 08:55 ET, then only a weak
  partial bounce to 27739.0 (current) -- confirmed on both NQ=F and
  MNQ=F. Read as SELL on the bounce, with-trend (strong down momentum,
  unconvincing retracement). Bracket off 27739.0: stop_loss_stop_price
  27772.25 (+33.25 pts), take_profit_limit_price 27722.25 (-16.75 pts),
  qty 3. **risk_guard allowed the order** (all checks passed, including
  consistency at the new 2.00 threshold). **Filled live**: NT8 log shows
  Execution Price=27727.75, Quantity=3, Short, Account='Sim101',
  Instrument='MNQ 12-26' at 09:19:43 ET; all 3 OIF commands (entry/stop/
  target) confirmed SUBMITTED via NT8's outgoing fills; incoming folder
  empty afterward. Order file cleaned up. Position now open: Short 3
  MNQ 12-26 @ 27727.75, bracket live (SL 27772.25 / TP 27722.25). Next
  cron fire (~09:37 ET, 30-min cadence) will check this position first.
  Position closed (2026-11-10, 09:20:52 ET, before the next cron fire):
  take-profit filled -- bought back 3 MNQ 12-26 @ 27720.50 (slightly
  better than the 27722.25 TP level), closing the Short. Gain: 7.25 pts
  x $2/pt x 3 = $43.50. Telemetry updated: daily_profit_today and
  total_realized_profit both now $279.00 (235.50 + 43.50), still well
  under the $2500 consistency threshold. Account confirmed Flat; the
  next cycle's position check (CronList shows `c9e1834b` next firing
  ~09:49am, offset by the scheduler's per-job jitter) will correctly see
  Flat and proceed to evaluate a fresh signal.
  Eleventh cycle (2026-11-10, ~09:39 ET): checked position -- flat.
  Fresh data showed a clean recovery from the 08:55 flush low (27652.5)
  to 27797.75 by 09:35 ET (+145 pts, higher lows/higher highs), then a
  modest pullback to 27764.25 (current, ~23% retracement) -- read as BUY
  on the dip, with the new short-term uptrend. Bracket off 27764.25:
  stop_loss_stop_price 27731.00, take_profit_limit_price 27781.00, qty 3.
  risk_guard allowed it. **Filled, but with a notable gap/slippage
  effect**: entry filled at avg 27784.83 (price had already moved ~20.5
  pts above the 27764.25 reference by fill time), which put the TP level
  (27781.00) already below the live market price -- so the resting
  SELL-LIMIT TP leg was immediately marketable and filled within the
  same second as the entry (avg exit 27784.58), closing the trade almost
  instantly for a small loss of ~$1.50 (3 x -0.25 pts x $2/pt) instead of
  riding toward the $100 target. Confirmed via NT8 log (Flat again at
  09:49:43 ET) and telemetry (277.50, down from 279.00). Same root cause
  as the first live trade's slippage finding, mirrored: when the market
  gaps between quote-time and fill-time, a bracket sized off the
  pre-submission quote can land partly or fully in-the-money at entry,
  causing premature exits -- a known, not-yet-addressed design gap
  (brackets are sized off a quote fetched before submission, not off the
  confirmed fill price). Account Flat, order file cleaned up.
  Manual off-cycle trade (2026-11-10, ~09:43 ET, user request, cron
  cadence left unchanged): checked position -- flat. Same uptrend thesis
  continued: fresh higher high 27853 (09:40 ET) on top of the prior
  cycle's structure, pullback to 27807.75 (current) -- still a
  higher-low pullback within the intact uptrend, read as BUY. Bracket
  off 27807.75: SL 27774.50, TP 27824.50, qty 3. risk_guard allowed it.
  **Same slippage pattern as the prior cycle, again**: market kept
  moving fast -- entry filled at avg 27834.08 (+26.3 pts above the
  27807.75 reference by fill time), put the 27824.50 TP already
  in-the-money at entry, instant SELL-LIMIT fill at avg 27833.08,
  closing for a small loss of $6.00 (3 x -1.0 pt x $2/pt). Confirmed via
  NT8 log (Flat again at 09:53:36 ET) and telemetry (271.50, down from
  277.50). This is now the SECOND consecutive occurrence of the same
  quote-to-fill slippage effect in fast-moving conditions -- worth
  treating as a pattern, not a one-off, if this market stays this
  volatile. Account Flat, order file cleaned up.
  Twelfth cycle (2026-11-10, ~10:09 ET): checked position -- flat.
  Fresh data showed a very strong, fast rally continuing: +388 pts from
  the 08:55 low (27652.5) to 28040.75 (current) in ~75 min, only a
  shallow pullback in the last 2 bars. Read as BUY-the-dip continuation
  (same thesis as prior cycles, now more extended). Bracket off
  28040.75: SL 28007.50, TP 28057.50, qty 3. risk_guard allowed it.
  **NEW, more severe failure mode (escalation of the known slippage
  issue)**: market moved DOWN ~63 pts between the 28040.75 reference and
  the actual fill (avg 27977.92) -- the opposite direction from the
  prior two cycles. This put `stop_loss_stop_price` (28007.50) ABOVE the
  live market at submission time. NT8 did not silently execute it like
  the earlier TP-side incidents -- it flat-out REJECTED the stop leg:
  "Sell stop or sell stop limit orders can't be placed above the
  market." Worse: because the stop and target legs share one OCO group,
  rejecting the stop leg invalidated the OCO group, so NT8 also rejected
  the target leg ("The OCO ID ... cannot be reused"). **Net result: the
  Long 3 MNQ 12-26 @ 27977.92 position was left completely naked -- no
  stop, no target -- confirmed via NT8's own log (both legs show
  'Rejected' with native error text) and fills.jsonl (`REJECTED;0;0`
  twice).** Treated this as an active risk emergency rather than waiting
  for the next 30-min cron cycle: flattened the position immediately via
  a direct `CLOSEPOSITION;Sim101;MNQ 12-26` OIF command (bypassing
  risk_guard/process_order deliberately, since this is protective
  flattening of an already-open naked position, not a new speculative
  entry). Confirmed closed via NT8 log: sold 3 @ 27991.25 at 10:21:45 ET,
  Flat. The market happened to move favorably during the ~2 minutes the
  position was unprotected, so this closed for a $80.00 gain
  (13.333 pts x $2/pt x 3) rather than a loss -- but that was luck, not
  protection by design. Telemetry: 351.50 (up from 271.50). This
  confirms the quote-staleness bracket-sizing issue (flagged after
  cycle 11) is a real, recurring, and now demonstrably more dangerous
  defect that can leave a live position fully unprotected, not just
  cause early/suboptimal exits -- this needs a decision and a fix before
  the loop keeps running unattended.
  User asked to validate Option 3 (ATM Strategy via executor.py). Checked
  the repo: the OIF grammar's `[STRATEGY]`/`[STRATEGY ID]` fields exist
  but their actual semantics were never documented or verified here --
  no evidence they invoke an ATM Strategy template. From general
  NinjaTrader knowledge (not verified in this session): ATM Strategies
  are normally invoked via the NinjaScript `AtmStrategyCreate` API or
  interactively, a different transport than the OIF/ATI file-drop
  interface already working tonight. Told the user Option 3 likely
  needs that separate API, not a tweak to the current OIF transport, and
  recommended verifying against NT8's official docs or a live test
  before committing to it. No code changed.
  Thirteenth cycle (2026-11-10, ~10:39 ET): checked position -- flat
  (confirmed closed from the prior incident). Fresh data: clear reversal
  since the 10:00 ET top (28048.5) -- lower highs/lower lows down to
  27870.0 (10:35 ET), bounce to 27890.75 (current). Read as SELL on the
  bounce, continuing the pullback. Bracket off 27890.75: SL 27924.00, TP
  27874.00, qty 3. risk_guard allowed it. Watched very carefully for any
  leg rejection this time (per the near-miss two cycles ago). **No
  rejection**, but the **same quote-staleness pattern recurred for a
  4th time**: market gapped ~116 pts further down between the 27890.75
  reference and the actual fill (entry avg 27774.67, far beyond the
  intended 16.75-pt TP distance) -- the TP leg (BUY LIMIT @ 27874) was
  already marketable at submission and filled within the same second
  (avg 27776.25), closing for a small loss of $9.50 (3 x 1.583 pts x
  $2/pt) rather than forming a real trade. The stop leg (BUY STOPMARKET
  @ 27924) was Accepted correctly this time (valid direction, since
  27924 was above the live market) and the OCO cancelled it cleanly once
  the TP leg filled -- confirming the OCO mechanism itself works fine;
  the earlier naked-position incident was specifically about the stop
  price landing on the wrong side of a fast-moving market, not an OCO
  design flaw in general. Telemetry: 342.00 (down from 351.50). This is
  now 4 of the last ~5 trades distorted by the same root cause (quote
  fetched before submission vs. actual fill price) in a market moving
  unusually fast tonight -- strong evidence this needs Option 2 (size
  the bracket off the confirmed fill price) built soon, not deferred.
  Cron paused (2026-11-10, ~10:55 ET, user request): cancelled
  `c9e1834b`. No job scheduled; account confirmed Flat at pause time.
  **Option 2 implemented (2026-11-10, user request, test-first)**:
  replaced caller-supplied absolute `stop_loss_stop_price`/
  `take_profit_limit_price` with `stop_loss_points`/`take_profit_points`
  (point distances). `process_order` now places the entry alone first,
  waits for NT8's own fill confirmation via the new
  `wait_for_entry_fill` (polls `NT8_OUTGOING_DIR`, default 10s timeout,
  returns `FillResult(status, price)` with status filled/rejected/
  timeout), then computes the bracket from that CONFIRMED price via the
  new `compute_bracket_prices_from_fill` (tick-rounded to 0.25). If NT8
  rejects the entry, no bracket is placed. If fill confirmation times
  out, `process_order` defensively flattens via `CLOSEPOSITION` rather
  than risk leaving a naked position (directly closes the gap that
  caused the near-miss two cycles ago). `build_bracket_exit_commands`
  signature changed to take explicit `stop_price`/`target_price`
  arguments instead of reading absolute prices off the order. Added a
  fail-closed refusal when a bracket is requested but
  `NT8_OUTGOING_DIR` isn't configured (can't confirm a fill price at
  all in that case). RED confirmed first (14 new/changed tests failing
  against the old code), then GREEN: 84/86 tests in
  `tests/tradovate_bot/` pass (the other 2 are the pre-existing,
  expected `CONSISTENCY_PCT=2.00` live-edit mismatches, unrelated,
  left untouched per the user's earlier instruction). Changed:
  `tradovate_bot/executor.py`, `tests/tradovate_bot/test_executor.py`,
  `tradovate_bot/README.md`. Not yet committed, and not yet tested
  against a real live NT8 fill (the next live cycle, whenever the user
  resumes, will be the first real-world test of this new path).
  Default timeout tuned (user request): `FILL_CONFIRMATION_TIMEOUT_SECONDS`
  default changed from 10s to 5s. README updated to match.
  **First live test of the fill-driven bracket (2026-11-10, ~11:09 ET,
  manual, user request)**: checked position -- flat. Fresh data: strong,
  sustained downtrend since the 10:00 ET top (28048.5), -281 pts to a
  27740.25 low (10:55 ET), modest bounce to 27767.25 (current). Read as
  SELL on the bounce, continuing the trend. Order placed with the NEW
  schema: side sell, qty 3, stop_loss_points 33.25, take_profit_points
  16.75 (no absolute prices supplied by the caller at all). risk_guard
  allowed it. **Confirmed working as designed**: entry filled clean at
  27824.00 (single fill, not fragmented); executor waited for that
  confirmation (SUBMITTED -> FILLED within ~0.6s per NT8's own log/
  fills.jsonl) and only then computed the bracket from it: stop
  27857.25 (fill + 33.25, correctly above fill for a short), target
  27807.25 (fill - 16.75, correctly below). Both legs came back
  ACCEPTED/WORKING -- no rejection, no instant-marketable fill this
  time. Verified the position is genuinely still open afterward (Short
  3 @ 27824 in NT8's log, telemetry unchanged at 342.00, no premature
  close) -- this is the first cycle all night where the bracket landed
  correctly on both sides on the first try. Order file cleaned up.
  Position currently open with a live bracket; outcome will show up in
  a future check.
  Manual trade closed (2026-11-10, 11:10:32 ET): take-profit filled
  exactly at the computed level, 27807.25 -- gain $100.50 (16.75 pts x
  $2/pt x 3), matching the intended $100 target precisely for the first
  time all night. Telemetry: 442.50 (up from 342.00). Account Flat.
  Cron resumed at 15-min cadence (job `13016b17`, offset 7/22/37/52,
  session-only, expires in 7 days) with the corrected points-based
  prompt. Committed the Option 2 fix as `ba16113` (risk_guard.py's
  CONSISTENCY_PCT=2.00 left uncommitted, as instructed).
  Fourteenth cycle (2026-11-10, ~11:12 ET, first cron fire at the new
  15-min cadence): checked position -- flat (confirmed closed from the
  manual trade above). Fresh data: after the 10:55 low (~27740), price
  bounced to 27830.25 (11:05) then pulled back to 27801.75 (11:10),
  now 27813.5 -- oscillating in a tight ~30-point range for the last
  ~35 minutes with no clear breakout either way. Read as choppy
  consolidation, no directional edge. Skipped the trade (step 3) rather
  than force one. No order attempted.
  Fifteenth cycle (2026-11-10, ~11:27 ET): checked position -- flat.
  Fresh data: a pullback from the 11:05 high (27830.25) to 27761.5
  (current), but still inside the same ~27740-27830 range flagged last
  cycle -- not yet a new low below the established 27740.25 range floor.
  Read as continued chop testing the lower boundary of an established
  range, not a fresh breakdown; skipped (step 3) rather than sell
  support that already held once. No order attempted.
  Sixteenth cycle (2026-11-10, ~11:42 ET): checked position -- flat.
  Fresh data: the previously choppy 27740-27830 range finally broke --
  a new low of 27709.25 (11:35 ET), below the prior 27740.25 floor,
  confirming a genuine breakdown rather than more chop; bounce to
  27729.25 (11:40) partially retraced to 27711.25 (current). Read as
  SELL on the bounce, now that the range is confirmed broken. Order:
  side sell, qty 3, stop_loss_points 33.25, take_profit_points 16.75.
  risk_guard allowed it. **Clean fill-driven bracket again**: entry
  filled single-shot at 27684.75 (qty 3, no fragmentation); bracket
  computed from that real fill: stop 27718.00 (fill + 33.25), target
  27668.00 (fill - 16.75), both ACCEPTED/WORKING -- no rejection, no
  instant-marketable leg. Incoming folder empty afterward. Order file
  cleaned up. Position open: Short 3 MNQ 12-26 @ 27684.75 with a live,
  correctly-placed bracket; outcome pending a future check.
  Position closed (2026-11-10, 11:54:43 ET): take-profit filled exactly
  at 27668.00. Account Flat.
  Seventeenth cycle (2026-11-10, ~11:57 ET): checked position -- flat.
  Fresh data: the downtrend continued past the prior cycle's level --
  a fresh low of 27632.5 (11:55 ET) after the 11:35-11:45 leg down,
  tiny bounce to 27639.0 (current). Read as SELL on the bounce,
  continuing the same established downtrend. Order: side sell, qty 3,
  stop_loss_points 33.25, take_profit_points 16.75. risk_guard allowed
  it. Entry filled (fragmented, 2+1) at avg 27626.42; bracket computed
  from that fill: target 27609.75 (fill - 16.75), both legs
  WORKING/ACCEPTED, no rejection. Incoming folder empty afterward.
  Order file cleaned up. Position open: Short 3 MNQ 12-26 @ ~27626.42
  with a live bracket; outcome pending a future check.
  User instruction: going forward, when the post-cron stop-hook shows
  the Gentle AI review consent envelope for the recurring task-doc +
  uncommitted risk_guard.py candidate, auto-select "decline" without
  asking each time (user's standing decision for this specific
  recurring situation) -- does not disable review generally.
  Position closed via STOP-LOSS (2026-11-10, 12:09:59 ET): exit at
  27660.00 vs entry ~27626.42 -- a real loss this time, ~$200.50 (3 x
  33.25 pts x $2/pt), within the designed max risk per trade. The
  bracket worked exactly as intended; not every trade wins. Telemetry:
  344.00 (down from 544.50). Account Flat.
  Eighteenth cycle (2026-11-10, ~12:12 ET): checked position -- flat.
  Fresh data: a clear reversal off the 11:55 low (27631.75) -- four
  consecutive higher closes (27657.25, 27658.75, 27678.5, 27692.0),
  +60 pts with no pullback yet. Read as BUY with the fresh momentum
  (chasing the move slightly, since there was no pullback to buy, but
  four consecutive up-closes is genuine structure, not noise). Order:
  side buy, qty 3, stop_loss_points 33.25, take_profit_points 16.75.
  risk_guard allowed it. Entry filled (fragmented, 2+1) at avg
  27660.83; bracket computed from that fill: target 27677.50 (fill +
  16.75), both legs ACCEPTED/WORKING, no rejection. Incoming folder
  empty afterward. Order file cleaned up. Position open: Long 3 MNQ
  12-26 @ ~27660.83 with a live bracket; outcome pending a future
  check.
  Position closed via STOP-LOSS (2026-11-10, 12:23:40 ET): exit at
  27627.25 vs entry ~27660.83 -- a second consecutive stop-out,
  ~$201.50 loss. Telemetry: 142.50 (down from 344.00). Account Flat.
  User asked for a root-cause analysis of these two consecutive
  losses. Found via a live cross-check: Yahoo's free chart feed was
  silently stale by ~11-15 minutes at the time (confirmed: its
  "current" quote was frozen at 12:17:41 ET while real time was
  12:28:20 ET) -- cycle 18's BUY decision was made on data that looked
  current but was already outdated by the time the order actually
  filled 11 minutes later, buying right at the top of a bounce that
  had already reversed. Secondary factor: genuine chop/whipsaw in the
  27600-27700 zone compounded the stale-data problem. User asked about
  a second data source; confirmed the TradingView MCP server already
  connected in this session (`mcp-tv-get-ohlcv`/`mcp-tv-get-symbol-data`,
  symbol `CME_MINI:MNQ1!`) returns genuinely live data
  (`update_mode: "streaming"`, bars within ~1 min of real time vs.
  Yahoo's stale feed) -- switched the cron's data source to TradingView
  going forward, plus two decision-quality rules: prefer entries on a
  pullback over chasing an extended move, and treat a price zone that
  has already reversed multiple times recently as a weaker signal.
  Cancelled cron `13016b17`, created `c0e24c44` (same 15-min cadence,
  "7,22,37,52 * * * *") with the updated TradingView-based prompt.
  Nineteenth cycle (2026-11-10, ~12:43 ET, first cycle using
  TradingView): checked position -- flat. Fresh TradingView data
  (freshness check passed: last bar 12:40 ET vs now 12:43 ET,
  update_mode streaming) showed a sharp, genuine breakdown -- 27640
  (12:30) to 27548.25 (12:35), -91.75 pts in one bar, decisively below
  the recent chop floor (27622-27631.75). But current price (27549.75)
  is essentially still at that low with no bounce yet. Per the new
  pullback rule, skipped (step 3) rather than sell with zero
  retracement to enter on. No order attempted.
  **Market-hours rule implemented (2026-11-10, user request, test-first)**:
  user asked to validate whether a mandatory flat-by-16:00-ET /
  reopen-at-18:00-ET rule existed anywhere (it did not -- verified by
  reading risk_guard.py and executor.py in full, no time-of-day check
  existed at all, only the unrelated 17:00 ET EOD accounting rollover
  in the NinjaScript AddOn). Added `check_trading_hours(now)` to
  `risk_guard.py`: closed daily 16:00-18:00 ET Mon-Thu, closed Fri
  16:00 ET - Sun 18:00 ET (weekend), open otherwise. Wired into
  `evaluate_order` as a new required third parameter `now` (no default
  -- a caller can never silently skip this check by omitting it,
  consistent with the module's fail-closed contract). RED confirmed
  first (new/changed tests failed against the old 2-arg signature),
  then GREEN: 96/98 tests in `tests/tradovate_bot/` pass (the other 2
  are the pre-existing `CONSISTENCY_PCT=2.00` live-edit mismatches,
  unrelated). `executor.process_order` now passes `time_utils.now_et()`
  as `now`. Actually flattening a position that's already open when
  the close window starts still has no Python-side position-awareness
  (that's a known, not-yet-built capability -- today's "position check"
  safeguard is entirely manual via the cron prompt reading NT8's log),
  so the cron prompt's step 1a now instructs a manual CLOSEPOSITION in
  that specific case; new entries outside hours are blocked in code
  regardless. Cancelled cron `c0e24c44`, created `195cc97c` (same
  15-min cadence) with the updated prompt. Changed:
  `tradovate_bot/risk_guard.py`, `tests/tradovate_bot/test_risk_guard.py`,
  `tradovate_bot/executor.py`, `tests/tradovate_bot/test_executor.py`,
  `tradovate_bot/README.md`. Not yet committed.
  Committed as `db35d82` (CONSISTENCY_PCT line split out via a
  temporary revert-stage-restore so the uncommitted 2.00 value stayed
  uncommitted, per the standing instruction).
  Twentieth cycle (2026-11-10, ~13:08 ET): checked position -- flat.
  Still well within trading hours (13:08 ET, nowhere near 16:00).
  Fresh TradingView data (freshness check passed: last bar 13:05 ET,
  update_mode streaming): the 27540-27690 zone has been chopping for
  over an hour -- breakdown to 27548.25 (12:35), bounce to 27637.25
  (12:55, itself a LOWER high than 12:15's 27689.25), now pulling back
  to 27603.5-27617.75 (current). Per the new repeated-reversal caution
  rule, read this as continued chop rather than a confident signal in
  either direction. Skipped (step 3). No order attempted.
  Twenty-first cycle (2026-11-10, ~13:23 ET): checked position -- flat,
  within trading hours. Fresh TradingView data (freshness check
  passed: last bar 13:20 ET, streaming): a slow, low-volatility grind
  down from the 12:55 bounce high (27637.25) to ~27596.5 (current),
  -41 pts over 25 min with essentially no bounce -- drift, not genuine
  momentum, still inside the same hour-long choppy range. Skipped
  (step 3). No order attempted.
  Twenty-second cycle (2026-11-10, ~13:38 ET): checked position --
  flat, within trading hours. Fresh TradingView data (freshness check
  passed: last bar 13:35 ET, streaming): a clean 4-consecutive-bar
  rally from the 13:20 low (27594.5) to 27676.75 (current), +82 pts,
  but no pullback yet, and still inside the same multi-hour choppy
  range (27517-27732) that has reversed repeatedly. Per the
  prefer-a-pullback and repeated-reversal-zone rules, waited rather
  than chase the 4th straight green bar. Skipped (step 3). No order
  attempted.
  Twenty-third cycle (2026-11-10, ~13:53 ET): checked position --
  flat, within trading hours. Fresh TradingView data (freshness check
  passed: last bar 13:50 ET, streaming): the pullback we were waiting
  for materialized (13:30-13:35: 27656 -> 27643.25) and resolved
  upward to a fresh local high (27678.25, 13:50), now a shallow dip to
  27669 (current). Read as BUY on the confirmed pullback, continuing
  the uptrend off the 13:20 low (27594.5). Order: side buy, qty 3,
  stop_loss_points 33.25, take_profit_points 16.75. risk_guard allowed
  it (including the new trading-hours check). Entry filled clean,
  single-shot at 27688.50; bracket computed from that fill: target
  27705.25 (fill + 16.75), both legs ACCEPTED/WORKING, no rejection.
  Incoming folder empty afterward. Order file cleaned up. Position
  open: Long 3 MNQ 12-26 @ 27688.50 with a live bracket; outcome
  pending a future check.
  Position closed via take-profit (2026-11-10, 13:54:41 ET): exit at
  exactly 27705.25, gain $100.50. Telemetry: 243.00. Account Flat.
  Twenty-fourth cycle (2026-11-10, ~14:08 ET): checked position --
  flat, within trading hours. Fresh TradingView data (freshness check
  passed: last bar 14:05 ET, streaming): a genuine breakout above the
  ~2-hour choppy range (27517-27699) to a fresh high of 27740.75
  (14:05 ET), now a shallow pullback to 27716 (current). Read as BUY
  on the pullback, continuing the breakout -- the first clean break of
  new ground all cycle, not just another leg of the same chop. Order:
  side buy, qty 3, stop_loss_points 33.25, take_profit_points 16.75.
  risk_guard allowed it. Entry filled clean, single-shot at 27721.25;
  bracket computed from that fill: target 27738.00 (fill + 16.75),
  both legs ACCEPTED/WORKING, no rejection. Incoming folder empty
  afterward. Order file cleaned up. Position open: Long 3 MNQ 12-26 @
  27721.25 with a live bracket; outcome pending a future check.
  Position closed via take-profit (2026-11-10, 14:09:32 ET): exit at
  exactly 27738.00, gain $100.50. Telemetry: 343.50. Account Flat.
  Twenty-fifth cycle (2026-11-10, ~14:23 ET): checked position --
  flat, within trading hours. Fresh TradingView data (freshness check
  passed: last bar 14:20 ET, streaming): same healthy pattern as the
  last two cycles -- a real pullback (14:05-14:10: 27746.75 ->
  27697.25) resolved upward to 27719 (14:20), now a shallow dip to
  27707 (current). Read as BUY on the confirmed pullback, continuing
  the uptrend. Order: side buy, qty 3, stop_loss_points 33.25,
  take_profit_points 16.75. risk_guard allowed it.
  **New bug found: the entry filled (confirmed in NT8's log at
  27715.17), but `process_order` still defensively flattened on a
  fill-confirmation timeout, closing almost immediately at 27715.75
  (net +$3.50, essentially flat -- the safety net worked, but a real
  trade opportunity was lost).** Root cause confirmed in
  `oif_writer.tail_outgoing` (line 145, `if filename in updated_seen:
  continue`): NT8 rewrites the SAME outgoing filename in place as an
  order's status progresses (confirmed: `Sim101_2d501818...txt`
  appeared twice tonight, once as `SUBMITTED`, later as `FILLED`, same
  filename). `wait_for_entry_fill` reads that file once while it's
  still `SUBMITTED`, marks the filename "seen", and never re-reads it
  once NT8 rewrites it to `FILLED` -- so it waits out the full 5s
  timeout and defensively flattens, even though the fill already
  happened. This is intermittent: it only manifests when a poll
  catches the file mid-transition (SUBMITTED first) rather than
  already-FILLED on the first read, which is why earlier cycles
  tonight didn't hit it. Not yet fixed -- flagged for the user to
  decide on a fix (likely: `wait_for_entry_fill` should re-read a
  filename it has already seen rather than permanently excluding it,
  scoped to that wait loop only, without changing `tail_outgoing`'s
  shared semantics used elsewhere for continuous fill-logging).
  Telemetry: 347.00 (up from 343.50, the small net gain from the
  premature close). Account Flat.
  **Progress-log gap, cycles 27-37 (2026-11-10, ~14:38-17:38 ET) --
  reconstructed summary**: these cycles' detailed notes were lost when
  something outside this session checked out `main` and pulled at
  ~17:27 ET (see incident below) before they were committed -- no
  code or trading activity was lost, only this doc's prose for that
  window, now summarized from the session's own chat history.
  Cycle 26 (14:38): chop near highs, skipped. Cycle 27 (14:53): clean
  breakout to 27775, BUY filled 27771.00, TP hit 27787.75 later for
  +$100.50 (telemetry to 442.50-ish range). Cycle 28 (15:08): position
  closed via STOP at 27737.75 (~-$199.50, designed risk); fresh cycle
  chopped near 27700-27790, skipped. Cycle 29 (15:23): clean breakout
  to 27814.25, BUY filled 27806.00, TP hit 27822.75 (+$100.50).
  Cycle 30 (15:38): pullback continuation, BUY filled 27784.00, TP hit
  27800.75 (+$100.50; telemetry reached 348.50). Cycle 31 (15:53):
  strong fresh breakout (27881) with zero pullback and only ~7 min to
  the 16:00 close -- skipped per the chase/close-proximity rules.
  Cycle 32 (16:08): first cycle past the 16:00 ET close; flat, no
  position to flatten, trading-hours gate correctly produced no
  activity. Cycles 33-37 (16:23-17:23): flat, unchanged, still outside
  trading hours each time -- no orders attempted, confirming the gate
  held the whole closed window.
  **Incident (2026-11-10, ~17:27 ET): working directory branch was
  switched out from under this session.** Something outside this
  session (likely the user via RDP) ran `git checkout main` then
  `git pull --tags origin main` on this same working directory at
  17:26:58-17:27:17 ET, landing on a PR merge (`fe45539`) that only
  included feature-branch history up to an earlier commit
  (`8b6cfc0`) -- missing the three fixes made later this session:
  `ba16113` (fill-driven bracket), `db35d82` (trading-hours gate), and
  `97775cd` (outgoing-file re-read fix). Discovered when this cycle's
  routine position check noticed the task doc's tail looked stale;
  confirmed via `git log`/`git reflog` and a grep for
  `check_trading_hours` in the live `risk_guard.py` (zero matches).
  No live trade was placed with the reverted code -- account was Flat
  and outside trading hours at the time, so the gap was caught before
  it mattered. Paused the cron (`195cc97c`) immediately as a
  precaution. feature/tradovate-oif-execution still had all 3 commits
  intact (3 ahead of what got merged). Per user's explicit choice,
  merged feature/tradovate-oif-execution into main (stashing and
  reapplying the uncommitted CONSISTENCY_PCT=2.00 change around the
  merge to avoid a conflict) -- clean merge, no conflicts
  (`c11b218`). Verified all 3 fixes present post-merge and
  tests green (97/99, same 2 known CONSISTENCY_PCT mismatches).
  Rescheduled the cron as `074cf349` (same 15-min cadence), with an
  added step 8 instructing future cycles to sanity-check the branch
  and the trading-hours gate's presence before trusting the code,
  given this already happened once. `main` pushed to `origin/main`
  (user request) -- `fe45539..c11b218`, now in sync.
  Branch `v2.1` created from `main` (user request) and pushed to
  `origin` with upstream tracking; this session now works on `v2.1`
  going forward.
  Cycle (2026-11-10, ~17:52 ET, first cycle on branch v2.1): sanity
  check per step 8 passed (branch=v2.1, check_trading_hours present).
  Checked position -- flat, unchanged. Still outside trading hours
  (~8 min until the 18:00 ET reopen). No order attempted.
  Cycle (2026-11-10, ~18:07 ET): sanity check passed. Checked
  position -- flat. Market reopened at 18:00 ET after the expected
  16:55-18:00 ET data/liquidity gap (consistent with the trading-hours
  rule). Fresh TradingView data (freshness check passed: last bar
  18:05 ET, streaming), but only 2 bars (10 min) exist since reopen,
  with a gap-down from the pre-close level (27902 -> 27859.5) and no
  real structure yet. Skipped (step 3) -- insufficient fresh
  post-reopen data to judge momentum confidently. No order attempted.
  Cycle (2026-11-10, ~18:22 ET): sanity check passed. Checked
  position -- flat, within trading hours. Fresh TradingView data
  (freshness check passed: last bar 18:20 ET, streaming): a clean,
  steady rally since the 18:00 ET reopen (27859.5 -> 27946, +86.5 pts
  over 20 min) with no pullback yet. Per the prefer-a-pullback rule,
  waited rather than chase. Skipped (step 3). No order attempted.
  Cycle (2026-11-10, ~18:43 ET): flat, BUY on confirmed pullback,
  filled 27971.75, bracket OK (target 27988.50). Noticed an EOD
  day-boundary rollover in telemetry right after (total_realized_profit
  reset to 0) -- flagged for later verification, see cycles.log.jsonl
  for full detail (per the new structured-log convention, this doc's
  lines stay short from here on).
  Position closed via STOP-LOSS (18:52:06 ET): the 18:43 Long closed at
  27938.25, loss ~$201.
  Cycle (2026-11-10, ~18:58 ET): flat. Fresh data: pullback from
  27980.5 high, only one small uptick after the decline -- not a
  confirmed pullback-then-continuation. Skipped (step 3, weak/chop
  signal). No order attempted. See cycles.log.jsonl.
  Added the "Cron procedure" section above (2026-11-10, user request:
  "update that cron prompt/procedure now", after `signals.py` was wired
  into `process_order` -- see
  `odd/tasks/tradovate-architecture-scaling.md` task 4). The order JSON
  now requires a `bars` field; without it every order is refused. Also
  documents that position-state and signal-quality are now enforced in
  code (tasks 1/2/4), not only by the cron's own manual judgment.
  Cron resumed (2026-11-10, ~21:39 ET, user request "resume el cron"):
  recreated as job `85944661`, same 15-min cadence, prompt now points at
  this doc's Cron procedure section instead of re-embedding the steps.
  First cycle (run immediately): flat, within hours. Fresh TradingView
  data (last bar 21:35 ET, streaming): chop -- 28135 high -> 28063 low ->
  28118 bounce -> pulling back to 28094.75. Cross-checked with
  `signals.evaluate_signal`: both buy and sell flagged (4 reversals in
  the trailing 9-bar window), confirming the manual read. Skipped. No
  order attempted. See cycles.log.jsonl.
  Cycle (2026-11-10, ~21:57 ET, first scheduled cron fire): sanity check
  passed (branch v2.1, check_trading_hours/signals/position_state all
  present). Flat, within hours. Fresh TradingView data (last bar 21:55
  ET, streaming): still chop, same range -- 28063 low -> 28112 bounce ->
  28095 pullback -> 28117.75 breakout -> now pulling back to 28106.
  `signals.evaluate_signal` flags both buy/sell (3 reversals in trailing
  window), confirming the read. Skipped. No order attempted. See
  cycles.log.jsonl.
  Cycle (2026-11-10, ~22:11 ET): sanity check passed. Flat, within
  hours. Fresh TradingView data (last bar 22:10 ET, streaming): a real
  high-volume breakout (vol ~12.9k vs ~5k avg), 28115 -> 28163.25, with
  a genuine pullback earlier in the lookback before it -- read as BUY.
  **First real disagreement between manual read and the code backstop**
  (Cron procedure step 9): attempted the order through the live
  `executor.process_order`; `signals.evaluate_signal` refused it as chop
  (3 reversals in the trailing 9-bar window, which still spans the
  preceding choppy range). Respected the refusal -- confirmed via
  `decisions.log.jsonl` and an empty NT8 incoming folder that nothing
  was sent. Also noted: telemetry `daily_realized_pnl` is -549, within
  $51 of the $600 daily loss limit (not breached, just close). See
  cycles.log.jsonl.
  Cron recreated (2026-11-10, ~22:3x ET, user request "verifica la posicion
  actual contra el log real de NT8 antes de recrear el cron"): verified
  against the real NT8 log (`log.20261110.00000.en.txt`) before recreating
  -- last position-state line is Flat at 18:52:06 ET (full close of the
  18:43 Long entry), no fills since; the only later event is a transient
  "Symbol is inaccessible" data-feed error at 21:46:53 ET, already recovered,
  not a position change. Recreated as job `2c28893e`, cadence changed to
  `7,22,37,52 * * * *` (was a 15-min cadence on job `85944661`), prompt still
  points at this doc's Cron procedure section instead of re-embedding the
  steps.
  Telemetry bug investigated and partially fixed (2026-11-10, ~22:3x ET,
  user report: "daily_realized_pnl is -549 pero NT8 muestra -200.50"):
  confirmed live via `tradovate_bot/state/account_telemetry.json` --
  `total_realized_profit` (-200.5) matched NT8 exactly, but
  `daily_realized_pnl` (-549) didn't, because the AddOn's own
  `realizedPnlAtDayStart` baseline (+348.5) had desynced from NT8's
  `RealizedProfitLoss` counter. Initially misdiagnosed the 17:00-ET
  day-boundary in `AccountTelemetryAddOn.cs` as the cause and changed it to
  18:00 -- **wrong**, caught before compiling: `odd/tasks/
  eastern-time-migration.md` (T4) already records that 17:00 ET was an
  explicit user decision, not a guess, so it was reverted back to 17:00.
  The actual fix kept: a divergence guard in `ExportOnce` that compares the
  RealizedProfitLoss-based daily delta against the CashValue-based one and
  withholds the export (instead of writing a bad number) if they disagree
  by more than $5 -- lets `account_state.py`'s existing staleness fail-close
  take over instead of risk_guard trusting a desynced counter. Deployed the
  corrected source to the live NT8 copy (`...\\NinjaTrader 8\\bin\\Custom\\
  AddOns\\AccountTelemetryAddOn.cs`, confirmed matching via diff) but **not
  yet recompiled** -- user still needs to open NinjaScript Editor and press
  F5 per `RUNBOOK_VPS.md`'s documented step; the running DLL is unchanged
  until then. Root cause of the original desync (why the two NT8 counters
  disagreed in the first place) not identified -- no AddOn startup/reset
  log lines found in the `.en.txt` NT8 log to pin an exact restart time.
  Cycle (2026-11-10, ~22:40 ET, cron job `2c28893e`): sanity check passed
  (branch v2.1, check_trading_hours/signals/position_state all present and
  wired). Flat since 18:52:06 ET (confirmed against NT8 log), within
  hours. Fresh TradingView data (last bar 22:40 ET, streaming): the 22:05
  high-volume breakout (28115 -> 28163.25, already read as BUY in the
  prior ~22:11 cycle and refused by signals as chop) extended to 28186.25
  then faded back to ~28157-28163 -- a 30-point range over the last ~35
  min. `signals.evaluate_signal` confirmed by direct call: chase guard
  clear (a real retracement, 28176.25 -> 28157.5, exists in the trailing
  8-bar window) but chop guard fired for BOTH directions (3 swing
  highs/lows in the trailing 9-bar window, CHOP_SWING_THRESHOLD=2) --
  manual read agreed before running the code, so skipped without
  attempting an order through the executor. Also noted: NT8 logged a
  transient MNQ "Symbol is inaccessible" data/tick subscription error at
  21:46:53 ET with no later recovery line visible in the log -- doesn't
  block this skip (TradingView bars are the actual data source) but worth
  confirming before the next cycle that does have a signal, since it could
  affect order routing through NT8 ATI. See cycles.log.jsonl.
