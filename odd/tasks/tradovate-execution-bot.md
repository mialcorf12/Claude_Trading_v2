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
