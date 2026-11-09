# Feature: Tradovate/NinjaTrader Deterministic Execution Bot

## STATUS AS OF 2026-11-09 (read this first)
The architecture pivoted twice since this doc was first written. The
original plan (direct Tradovate REST/WS API) is a **confirmed dead end**:
Tradovate prop-firm/evaluation accounts cannot get API Access at all (see
Pivot History below). Current plan: **NinjaTrader 8 ATI (Order Instruction
Files) + a read-only NinjaScript AddOn for account telemetry**, running on
a Windows VPS (TradingVPS, Basic tier: 2 cores / 8GB RAM / $35mo) where NT8
is already installed and connected to the Lucid Tradovate account. User
connects to that VPS via Microsoft Remote Desktop ("Windows App" on Mac);
Claude has no remote-execution tool into it yet (no SSH confirmed/enabled)
— deployment there is still an open logistics question, see Open
Questions. User is migrating this project to the VPS via `git`.

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

## Open questions (blocking next implementation step)
- How will Claude actually touch the VPS to help set this up? User wants to migrate via `git` from Mac to VPS, and asked Claude to "already be on the VPS" — but there is no remote-execution tool available in this session (no SSH client configured, RDP is a human-only GUI). Needs: either (a) OpenSSH Server enabled on the Windows VPS with host/port/credentials given explicitly, or (b) Claude prepares code + exact commands here, user runs them on the VPS via RDP and pastes results back. Not yet decided.

## Acceptance criteria
- [x] Unit tests for risk_guard cover: under all limits (pass), daily loss breached, EOD max-loss breached, consistency rule breached, position size over 3 MNQ, ambiguous/missing data (fail closed). Evidence: `python3 -m pytest tests/tradovate_bot/test_risk_guard.py -v` → 24 passed. Still valid — unaffected by the transport pivot.
- [x] Executor rejects an order when any risk_guard check fails, and logs why. Evidence: `tests/tradovate_bot/test_executor.py::test_process_order_rejects_when_risk_guard_fails` and `::test_process_order_rejects_oversized_mnq_position` pass. Still valid; executor will be rewired to the OIF writer but this contract (reject + log) doesn't change.
- [ ] ~~Tradovate client authenticates against demo env...~~ SUPERSEDED — direct API confirmed impossible for prop accounts. `client.py` as written will not be used against the real account.
- [ ] ~~WebSocket listener reconnects...~~ SUPERSEDED — no WebSocket in the ATI/OIF path; replaced by tailing NT8's `outgoing` folder for confirmation files.
- [x] README documents env vars required and how to switch demo → live. Still accurate for the parts that remain (no hardcoded creds), but the demo/live Tradovate env var is moot under ATI — README needs a rewrite pass when the OIF module lands.
- [x] NEW: OIF writer module places/cancels/closes a position via NT8 ATI and the resulting order/position confirmation files are correctly parsed. Evidence: `python -m pytest tests/tradovate_bot/test_oif_writer.py tests/tradovate_bot/test_executor.py -v` → 24 passed. Caveat: `tail_outgoing`'s "parsing" is an intentionally honest generic semicolon-split (filename-convention account/instrument/kind only) — the outgoing files' internal field semantics are still unconfirmed against a real NT8 instance; these confirmations feed fill-logging only, never risk_guard.
- [x] NEW: Read-only NinjaScript AddOn exports account realized P&L (today + total) and EOD cash balance to a JSON file NT8-side, and `executor`/`risk_guard` can read it. `ninjatrader_addon/AccountTelemetryAddOn.cs` + `tradovate_bot/account_state.py` (10/10 tests passing, see below). Caveat: the AddOn itself is **not independently verified against a live/demo NT8 instance** — no NT8 runtime was available to compile/run it this session. Must be sanity-checked on the VPS (including a real day-rollover) before being trusted for the funded account.
- [ ] NEW: Whole stack (NT8 + AddOn + Python executor) runs unattended on the Windows VPS (TradingVPS Basic) without manual restarts. Not started — blocked on the remote-access open question above.

## Tasks
1. [x] Tradovate REST/WS client — `tradovate_bot/client.py`. Built, tested against mocks, but SUPERSEDED (dead end for prop accounts). Kept in repo as reference/for a possible future live non-prop Tradovate account; not deleted.
2. [x] risk_guard module + unit tests (test-first). `tradovate_bot/risk_guard.py` + `tests/tradovate_bot/test_risk_guard.py`, tests written before implementation, 24/24 passing. Still the current, correct decision engine.
3. [x] executor main loop. `tradovate_bot/executor.py` rewired to the OIF writer + AddOn JSON reader (`oif_writer`/`account_state` modules); no longer imports `client.py`/websockets at all. `tests/tradovate_bot/test_executor.py` rewritten to match (14/14 passing).
4. [x] Process/runbook docs — `tradovate_bot/README.md` rewritten for the ATI/OIF path (env vars, OIF grammar, AddOn JSON contract, unverified-items list). `tradovate_bot/RUNBOOK_DEMO.md` given a superseded banner pointing at the README instead of a full rewrite (it's entirely about the dead direct-API/Mac path).
5. [x] NEW: OIF writer module (replaces client.py's order-placement role). `tradovate_bot/oif_writer.py` + `tests/tradovate_bot/test_oif_writer.py`, 10/10 passing.
6. [x] NEW: NinjaScript AddOn for read-only account telemetry. `ninjatrader_addon/AccountTelemetryAddOn.cs` — unverified against live NT8 (see acceptance criteria caveat above).
7. [ ] NEW: VPS deployment runbook (git clone/pull on Windows VPS, Python install, NT8 ATI settings to enable, AddOn install into NT8, how to run executor unattended on Windows — NSSM or Task Scheduler, not systemd).
8. [ ] NEW: Resolve remote-access method (SSH vs. manual-RDP-with-pasted-output) so implementation/verification on the VPS can actually happen.

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

Still true regardless of transport: `risk_guard.py` is pure, deterministic,
fail-closed, test-first, and already covers every Lucid risk-rule case —
24/24 of its own tests passing, unaffected by any pivot. The OIF writer and
AddOn telemetry reader are now implemented and unit-tested (fail-closed
contracts verified), and `executor.py` no longer depends on `client.py` or
any network/websocket library at all.

Not yet done: tasks 7 and 8 — running the whole stack unattended on the
VPS, and resolving how Claude actually touches that VPS. Also not yet
done: verifying the NinjaScript AddOn's actual runtime behavior (day-
rollover correctness in particular) against a real NT8 instance, and
verifying NT8 ATI's real outgoing-file field layout.
