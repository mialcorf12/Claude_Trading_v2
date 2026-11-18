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
That's why it has drifted before (see the 2026-11-10 incident in
`odd/tasks/tradovate-execution-no-necessary.md` where a branch checkout
under this session reverted three fixes that had already shipped). This
section is the fix for that: it's the one place
that should be kept current, so the next cron creation copies from here
instead of from memory.

**Execution notes (added 2026-11-16, from a per-step timing audit this
session -- these change HOW the steps below are run, not what they
decide; no guard, threshold, or decision rule changes)**:
- Step 10's NSSM service check: use `sc queryex TradovateExecutor` (gives
  STATE + PID) and, for that PID's start time, `wmic process where
  "ProcessId=<pid>" get CreationDate` -- both via Bash, **not**
  PowerShell's `Get-CimInstance Win32_Service`. Measured same info
  (state, PID, process start time) in ~0.02-0.13s via Bash vs.
  PowerShell's own host cold-start (commonly 1-3s+) stacked on top of a
  ~0.5s query -- the single most expensive step in the cycle, for
  information that almost never changes between cycles.
- Current ET time: use plain `date` with **no** `TZ=` override -- the
  VPS's OS clock is already set to US Eastern (confirmed matching
  Python's `zoneinfo` output exactly). Setting `TZ=America/New_York` on
  this box has no tzdata to resolve it and silently falls back to GMT --
  don't do that, and don't reach for a `python -c` subprocess just to
  get the time.
- Steps 3/5 and 11's `python -c` calls (signal evaluation +
  `select_bracket_points`, then `log_cycle`): run them as **one**
  combined `python -c` script per cycle instead of two separate
  subprocess calls -- same calls, one fewer Python interpreter cold
  start.
- Steps 1 and 10's Bash-only checks (branch, last commit touching
  `tradovate_bot/*.py`, `git status`, the NT8 log tail, the `trader/`
  dir check): batch them into a single Bash call (one shell script,
  several commands) instead of several separate Bash tool invocations,
  to cut per-call round-trip/orchestration overhead.

1. Check current position first. As of task 1
   (`odd/tasks/tradovate-architecture-scaling.md`), this is now also
   enforced automatically in code: `executor.process_order` refuses a new
   entry whenever `position_state.read_position_state()` reports anything
   other than Flat, and `executor.run_once` auto-sends CLOSEPOSITION when
   the market is closed and a position is open. `read_position_state`
   now also carries the last known position forward across a log
   rotation (fixed 2026-11-11: it used to go blind to "unknown" right
   after NT8 rotates to a new `*.en.txt` file -- a restart, or local
   midnight -- until the next real fill wrote something; it now checks
   every file newest-first and falls back instead of giving up). The
   cron prompt's own manual check (tailing the newest `*.en.txt` under
   `NT8_LOG_DIR` for `Account='Sim101'` + `Market position=`, falling
   back to an older file yourself if the newest one is empty) is now a
   backup, not the only enforcement -- keep doing it anyway, since it
   lets the LLM skip the cycle early with a clear status line instead of
   relying on the code path's refusal to notice.
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
   by calling `signals.select_bracket_points(bars)` on the SAME trailing
   9-bar window already fetched in step 2 and used for the signal read
   -- **changed 2026-11-13 per explicit user request** from the earlier
   hardcoded 16.75/16.75. It returns an equal (SL, TP) pair sized off
   recent volatility: 16.75 pts (~$100 for 3 contracts) when the
   trailing 9-bar average high-low range is below 40.0 points, 33.5 pts
   (~$200 for 3 contracts) at or above it, and fails closed to the
   narrow 16.75 pts pair when volatility can't be computed (malformed or
   insufficient bars). SL and TP are still always equal to each other
   (1:1 risk:reward) -- only the magnitude is now dynamic, not the
   ratio. Use the returned pair as-is for the order JSON, tick-rounded,
   oriented for BUY vs SELL.
6. Write the order JSON to `trader/orders.json` (or the one-shot path used
   for a manual/off-cycle run) **as a JSON list** (`[{...}]`) -- or
   `{"orders": [{...}]}` -- **never a bare order object**.
   `executor.load_orders` only recognizes those two shapes; a bare dict
   has no `"orders"` key, so it silently resolves to zero orders. Confirmed
   2026-11-11 ~23:15 ET: `claim_orders_file` still claims-and-deletes a
   wrongly-shaped file (it doesn't parse JSON), so the file vanishes
   exactly like a normally-processed order -- giving every appearance of
   having run while `risk_guard.evaluate_order` was never actually called
   and nothing reached NT8. Always confirm after writing: a new line
   should appear in `tradovate_bot/state/decisions.log.jsonl` within one
   poll interval; if it doesn't, the shape was wrong. Each order object
   in the list needs: `symbol` "MNQ", `side`, `qty` 3,
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
   every order, silently, until this is fixed. **IMPORTANT, read before
   writing this file**: if the NSSM `TradovateExecutor` service (or any
   other always-on poller) might be running, see step 7's note on the
   2026-11-11 incident below first -- writing this file is safe again
   now that `run_once` claims it atomically (fixed 2026-11-11), but only
   if that service is running CURRENT code.
7. Run it through the executor (same invocation used for prior cycles --
   see `odd/tasks/tradovate-execution-no-necessary.md` for the exact env
   vars/script path used that session).
   **2026-11-11 incident + fix**: an independently-running NSSM service
   (`TradovateExecutor`, started long before any cron session touches
   this file) polls `trader/orders.json` on its own every
   `POLL_INTERVAL_SECONDS` (5s default) via the exact same `run_once`.
   Before the fix, neither `load_orders` nor `run_once` ever removed a
   processed order file, so whichever process polled next -- this
   session's own manual call, or that service -- found and resubmitted
   the SAME order again. That resubmitted a single SELL 3 contracts 12
   times in about a minute, building a 36-contract position (12x the
   3-contract cap) with no bracket, before anyone caught it. Fixed:
   `run_once` now calls `claim_orders_file` to atomically rename the
   file to a unique claimed name the instant it's read (`os.rename` is
   atomic -- only one concurrent claimant can win), then deletes it after
   processing. Any process that loses the race just finds the file
   already gone and does nothing, instead of reprocessing it. This makes
   writing `trader/orders.json` safe again regardless of how many
   pollers are running -- **but a separate, equally serious problem was
   also found and is NOT about this file**: that same NSSM service's
   python process had been running since 2026-11-10 08:43:44 ET, which
   predates nearly every safety fix landed that day (the bracket-from-
   confirmed-fill fix at 11:14, the trading-hours gate at 12:59, the
   one-position-at-a-time + auto-flatten guard at 19:11, the signals.py
   chop/chase guard at 21:32) -- Python does not hot-reload a running
   process's imported modules, so none of those fixes were actually
   active in the service the whole time it ran, which is also why none
   of the 12 duplicate fills got a bracket. **Before trusting the
   service again, verify it was restarted after its current deploy of
   `executor.py` landed, or restart it yourself** -- a running PID's
   start time older than the file's last git-commit time for
   `tradovate_bot/*.py` means it's serving stale code no matter what the
   file on disk says.
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
    branch-checkout incident in `odd/tasks/tradovate-execution-no-necessary.md`,
    where a `git checkout main` under this session silently reverted three
    already-shipped fixes). If the
    `TradovateExecutor` NSSM service (or any other always-on poller) is
    Running, also check whether it needs a restart to pick up anything
    changed in `tradovate_bot/*.py` since it last started (see step 7's
    2026-11-11 incident note) -- Python never hot-reloads a long-running
    process's own imports, so a stale service can silently diverge from
    the file this step just verified on disk. **Use the Bash commands
    from the Execution notes above for this check, not PowerShell.**
11. Log the full structured record to `tradovate_bot/state/cycles.log.jsonl`
    via `executor.log_cycle(...)` (a `python -c` Bash call, combined with
    step 3/5's signal check into one process per the Execution notes
    above) on every single cycle, routine skips included, with the full
    narrative (what happened, why, NT8 confirmation) in `signal_reason`/
    `notes`. **Never edit either doc (this one or
    `tradovate-execution-no-necessary.md`) from an automated/cron-
    triggered cycle, for ANY event, notable included** -- changed
    2026-11-12 for routine skips only, then widened 2026-11-16 (after the
    user asked three times in one session to stop seeing an `Update(...)`
    tool notification, the last time explicitly choosing "stop editing
    the doc forever" over "keep editing, just don't narrate it") to cover
    every cron-triggered edit, with no exception for an order placed, a
    position closing, an incident, or a sanity-check failure -- all of
    that narrative now lives only in the JSONL's `signal_reason`/`notes`
    fields. The `Update(file)` tool-call banner itself is rendered by the
    harness for every Edit/Write call and has no per-file suppression --
    the fix is not calling Edit/Write on either doc from a cron cycle at
    all, not trying to hide the banner. Only edit either doc when the
    user explicitly asks, in that message, to update them (e.g.
    reviewing the JSONL together and deciding something belongs in the
    durable narrative, or a manual procedure change like this one).
12. Clean up any one-shot order file after each run -- as of 2026-11-11
    this is now guaranteed by `run_once` itself (claim-then-delete, see
    step 7's note), not something to remember to do by hand. Still true
    for any one-off path outside `run_once`/`trader/orders.json` (e.g. a
    manual `process_order(...)` call that wrote its own scratch file).

## Progress
Moved out (2026-11-16, user request: shrink this doc for faster reads).
The complete chronological Progress log (every cycle, incident, and pivot
since 2026-11-09) now lives in `odd/tasks/tradovate-execution-no-necessary.md`
-- moved verbatim, nothing deleted or rewritten. New notable entries
(per Cron procedure step 11: an order placed, a position closing, a
parameter/procedure change, a real incident, or a sanity-check failure)
still get appended to THAT file's end, not re-inlined here -- keep this
doc the fast-to-read reference, not the growing log again.
