# Tradovate/NinjaTrader Deterministic Execution Bot

A standalone, always-on process that places/closes MNQ orders on the Lucid
Trading Tradovate account using fixed, non-LLM rules. It is the execution
layer below Claude's `day-trader-skill` pack (scanner/analyst/risk/broker),
which stays the signal layer. No LLM call exists anywhere in `tradovate_bot/`.

See `odd/tasks/tradovate-execution-bot.md` for the full pivot history and
current status. Short version: direct Tradovate REST/WS API access is
**confirmed impossible** for this prop-firm/evaluation account. The current
and only supported transport is **NinjaTrader 8's Automated Trading
Interface (ATI)**, via Order Instruction Files (OIF), plus a read-only
NinjaScript AddOn for account telemetry.

## Modules

- `oif_writer.py` -- builds and writes NT8 ATI OIF command files
  (PLACE/CANCEL/CLOSEPOSITION) into NT8's `incoming` folder, and tails its
  `outgoing` folder for order/position/connection confirmation files.
- `account_state.py` -- reads the JSON telemetry file exported by the
  read-only NinjaScript AddOn and maps it onto the fields `risk_guard`
  requires. This is the **only** account-state input risk_guard trusts.
- `risk_guard.py` -- pure, deterministic functions (no I/O) implementing
  the Lucid Trading 25k Flex Eval risk rules. Fails closed on any missing
  or malformed input. Unaffected by the transport pivot.
- `executor.py` -- main loop: reads an approved order (same shape as
  `trader-broker`'s `trader/orders.json`), risk-checks it against account
  state from the AddOn's telemetry file, places it only if every check
  passes via the OIF writer, and logs every decision and fill.
- `config.py` -- optional explicit `BotConfig` identity/transport bundle
  (see "Multi-instance config" below).
- `client.py` -- **historical reference only, not used.** The original
  direct Tradovate REST/WS client. Confirmed dead end: Tradovate
  prop-firm/evaluation accounts (incl. Lucid) cannot register API Access.
  Kept in the repo in case a future, different, live non-prop Tradovate
  account ever needs it; `executor.py` no longer imports it.

## NinjaScript AddOn

`ninjatrader_addon/AccountTelemetryAddOn.cs` -- a read-only AddOn that
exports account balance/P&L to a JSON file for `executor.py`/`risk_guard`
to read. It never places, modifies, or cancels an order; it only reads
`Account.Get(...)` and writes a file. Install it into NT8 (NinjaScript
Editor -> paste/import -> compile).

This AddOn has no window, and NinjaTrader does not expose any UI to edit
`[NinjaScriptProperty]` values on a windowless AddOn (NinjaTrader support
confirms those are only settable from inside an AddOn's own custom window).
So it is **not** configured from any NT8 properties screen -- it reads the
same environment variables as the Python executor, listed below
(`NT8_ACCOUNT_ID`, `ACCOUNT_TELEMETRY_JSON_PATH`,
`ACCOUNT_TELEMETRY_EXPORT_INTERVAL_SECONDS`), once at startup.

**How to confirm it's actually running:** a headless AddOn does not need,
and will not get, any menu entry -- NinjaTrader instantiates and activates
it automatically at startup and on every NinjaScript compile, with nothing
to "find" in a menu. Instead, open NT8's NinjaScript Output window
(Control Center -> New -> NinjaScript Output) and look for a line starting
with `AccountTelemetryAddOn: started.` -- it logs the account/path/interval
it resolved. If you instead see `account '...' not found`, the configured
`NT8_ACCOUNT_ID` doesn't match any connected account's name in NT8 (check
NT8's Accounts tab for the exact literal name).

**Not independently verified against a live/demo NT8 instance** (built
without NT8 runtime access). Before trusting it for the funded account:
run it for at least one full session including a day rollover, and compare
its emitted JSON against NT8's own Cash Value / Account Performance display
for that account.

JSON contract it must emit (flat object, exact field names):

```json
{
  "updated_at": "2026-11-09T14:32:05Z",
  "daily_realized_pnl": -50.0,
  "eod_balance_last_close": 25000.0,
  "current_balance": 24950.0,
  "daily_profit_today": 0.0,
  "total_realized_profit": 100.0
}
```

`account_state.read_account_state()` treats the file as untrustworthy (and
returns `{}`, which makes risk_guard reject) if `updated_at` is older than
`ACCOUNT_TELEMETRY_MAX_STALENESS_SECONDS` -- i.e. if the AddOn/NT8 has
stopped updating it, we refuse to trade rather than act on stale data.

## Local market data (NT8 -> MCP)

Market bars for the cron's signal read come ONLY from the local NT8 feed;
TradingView and Yahoo are no longer used.

- `ninjatrader_addon/LocalBarExporter.cs` -- NT8 indicator. Writes the last
  200 closed bars of the 1m and 5m series to `candles/<SYMBOL>_<tf>.json`
  (e.g. `MNQ_1m.json`, `MNQ_5m.json`; `NQ_*` when working on NQ). Setup:
  copy to `Documents\NinjaTrader 8\bin\Custom\Indicators`, compile in the
  NinjaScript Editor (F5), then add it to the **1-minute** chart of MNQ (or
  NQ) and keep that chart open. Files are UTF-8 **with BOM**.
- `candles/` -- output folder for those JSON files (runtime data).
- `tradovate_bot/localMarketDataMCP.py` -- MCP server exposing
  `get_local_candles(symbol="MNQ", timeframe="5m", limit=10)`. Falls back
  between MNQ and NQ if only one file exists.
- `.mcp.json` (repo root) -- registers it as `local-market-data`, scoped to
  this project. After editing the server, run `/mcp` to reconnect.

Timezone: bar `time` is bar-close time in the VPS local zone, which is ET.
Everything stays in ET; no conversion is done.

Freshness is checked by the MCP, not the caller: the response carries
`freshness` = `{now_et, last_bar_time_et, last_bar_age_seconds,
file_age_seconds, stale}`. `stale` is true when the newest bar or the file
is older than the timeframe + 60 s, or when timestamps cannot be parsed
(fail-closed). It is also true while the market is closed or NT8/the chart
is not running -- in all cases do not trade on that data.

## Price-drift gate

`executor.process_order` refuses a new entry when the live price has moved
away from the price the decision was based on (`price_drift.py`). The
decision price is the close of the last bar in `order["bars"]`; the live
price is the close of the newest completed 1-minute bar in
`candles/<SYMBOL>_1m.json`. The entry is refused when
`|live - decision| > 0.25 x stop_loss_points` (8.375 pts for the usual 33.5
pt bracket; 8.0 pts when the order has no stop), or when the candles file is
missing, unreadable, malformed or its newest bar is older than 150 s
(fail-closed, same as `risk_guard` and `signals`).

Why: stop and target are anchored to the actual fill, so a move between
decision and fill silently shifts the whole bracket. On 2026-11-18 a BUY
decided at 27459.5 filled at 27425.25 (-34.25 pts) and was stopped out two
minutes later; replaying that cycle against the real 1m data the gate refuses
it (21.50 pts of drift, limit 8.38). See `odd/tasks/price-drift-gate.md`.

Limits: the "live" price is the last completed 1m close, so it can lag by up
to about a minute; the gate bounds drift but does not see intra-minute moves.
It only runs on entries. Env overrides: `PRICE_DRIFT_MAX_FRACTION`,
`PRICE_DRIFT_MAX_POINTS`, `PRICE_DRIFT_MAX_AGE_SECONDS`. The service must be
restarted to load changes (Python does not hot-reload).

## Fill-slippage check and late-bounce guard

- After a bracketed entry fills, `executor.process_order` compares the CONFIRMED
  fill price with the decision price (`bars[-1].close`) using the same limit as
  the price-drift gate (0.25 x stop, 8.0 pts without a stop). If the fill slipped
  past it, the position is flattened at once (`CLOSEPOSITION`), NO bracket is
  placed, and the decision log gets a `fill_slippage` record; within the limit the
  bracket stays anchored to the fill. Orders without a bracket never wait for a
  fill and are not checked. See `odd/tasks/fill-slippage-and-chase-size.md`.
- `signals.check_chase_guard` also flags a "late entry": a buy after the bounce
  off a swing low has already recovered more than 75% of the prior down-leg (a
  sell: of the prior up-leg). Chosen by replay of 2026-11-18; the literal
  "last bars moved more than k x the average range" rules removed winners and
  are not used.

## Offline replay (`tradovate_bot/replay.py`)

Rules here are accepted or rejected by replaying exported 5-minute candles, not
by intuition (direction guard, late-bounce check, fill-slippage limit, and the
rejected structural stop: `odd/tasks/*.md`). The harness simulates every trade
the guards would allow (entry at the next bar's open, stop = target from
`select_bracket_points`, stop first on a same-bar touch, $ for 3 MNQ contracts,
no costs) and reports each guard's effect on its own:

    python -m tradovate_bot.replay                # guard ablation on the history
    python -m tradovate_bot.replay --archive      # merge candles/MNQ_5m.json into the history
    python -m tradovate_bot.replay --allow-gaps   # also use windows that cross a market closure

Read it as: a guard whose REMOVED trades net negative is earning its keep; one
whose removed trades net positive is removing winners. `candles/MNQ_5m.json`
only keeps the last 200 bars (~16 h), so `--archive` (run on every cycle, see
`CYCLE_PROMPT.md` step 11) grows `candles/archive/MNQ_5m_history.json`. Caveats:
simulated fills, overlapping windows (trades are not independent), no costs and
few sessions -- it can reject a rule, it cannot prove one. The 2026-11-18
session alone (200 bars): no guards -> -$4623 over 316 trades; each guard removes
mostly losers; all guards together -> +$302 over 66 trades.

## Required environment variables (current, ATI path)

| Variable | Purpose |
|---|---|
| `NT8_INCOMING_DIR` | NT8 ATI's `incoming` folder (OIF commands are written here) |
| `NT8_OUTGOING_DIR` | NT8 ATI's `outgoing` folder (confirmation files are tailed from here). Optional for a bare entry with no bracket, but **required** for any order that requests `stop_loss_points`/`take_profit_points` -- `process_order` refuses rather than place an entry it can't confirm the fill price for (see "Bracket sizing" below) |
| `NT8_ACCOUNT_ID` | The NT8 account *name* string (e.g. `Sim101`), not a Tradovate numeric id |
| `ACCOUNT_TELEMETRY_JSON_PATH` | Path to the AddOn's telemetry JSON file |
| `ACCOUNT_TELEMETRY_MAX_STALENESS_SECONDS` | Max age of telemetry before it's treated as stale (default `30`) |
| `ACCOUNT_TELEMETRY_EXPORT_INTERVAL_SECONDS` | Read by the NinjaScript AddOn only (not `executor.py`): how often it re-exports the telemetry JSON (default `5`) |
| `TRADOVATE_ORDERS_PATH` | Path to the approved-orders file (default `trader/orders.json`) |
| `TRADOVATE_POLL_INTERVAL_SECONDS` | Main loop poll interval (default `5`) |

`executor.main()` refuses to start (exits non-zero) rather than run against
an unconfigured/ambiguous transport if `NT8_INCOMING_DIR`, `NT8_ACCOUNT_ID`,
or `ACCOUNT_TELEMETRY_JSON_PATH` is unset.

## Trading hours

`risk_guard.evaluate_order` now requires a third argument, `now` (a
timezone-aware datetime, e.g. `tradovate_bot.time_utils.now_et()`), and
hard-rejects any order outside the trading window: closed daily
16:00-18:00 ET Monday-Thursday, and closed from Friday 16:00 ET through
Sunday 18:00 ET (weekend) -- confirmed by the user, 2026-11-10. `now` is
required, not defaulted, so a caller can never silently skip this check.
This blocks new entries outside hours in code; actually flattening a
position that's already open when the close window starts is still a
manual step in the cron prompt (no automated position-awareness exists
yet in Python -- see the "What is NOT verified yet" section).

## Bracket sizing (fill-driven, not quote-driven)

An order's protective bracket is requested via `stop_loss_points`/
`take_profit_points` -- distances in price points, never absolute prices.
`process_order` places the entry alone first, waits for NT8's own fill
confirmation (`wait_for_entry_fill`, polling `NT8_OUTGOING_DIR`, default
5s timeout), and only then computes the stop/target as absolute,
tick-rounded prices from that CONFIRMED fill price
(`compute_bracket_prices_from_fill`). It never derives a bracket from a
quote fetched before submission.

This replaced an earlier design (absolute `stop_loss_stop_price`/
`take_profit_limit_price` supplied by the caller) after it caused repeated
live incidents on 2026-11-10: in a fast-moving market, the gap between a
pre-submission quote and the actual fill repeatedly put one bracket leg
on the wrong side of the live market -- causing instant/premature exits
three times, and once causing NT8 to reject the stop leg outright
("Sell stop or sell stop limit orders can't be placed above the
market."), which cascaded into the OCO-linked target leg also being
rejected and left a live position fully unprotected for about two
minutes. See `odd/tasks/tradovate-execution-bot.md` for the full incident
log.

If NT8 rejects the entry itself, no bracket is placed (nothing to
protect). If fill confirmation doesn't arrive within the timeout,
`process_order` defensively flattens the position via `CLOSEPOSITION`
rather than risk leaving it open with no stop and no target.

`executor.py` also has a hardcoded `NT8_INSTRUMENT` constant (currently
`"MNQ 12-26"`) for the OIF command's INSTRUMENT field -- NT8 rejects the
generic `"MNQ"` root outright (confirmed live: no market data available
for it). This must be updated by hand every quarterly MNQ roll (Mar/Jun/
Sep/Dec) to match NT8's own display name for the new front-month contract.
It is intentionally not an env var or auto-computed from the roll
calendar, by explicit choice, to stay consistent with `risk_guard.py`'s
other hardcoded constants.

### Historical/moot env vars (client.py only, not used by executor.py)

`TRADOVATE_USERNAME`, `TRADOVATE_PASSWORD`, `TRADOVATE_API_KEY`,
`TRADOVATE_API_SECRET`, `TRADOVATE_ACCOUNT_ID`, `TRADOVATE_DEVICE_ID`,
`TRADOVATE_ENV` -- these only matter if something still imports `client.py`
directly (e.g. exploratory/interactive use against a different account).
`executor.py` does not read or need them.

## OIF grammar (confirmed, from NT8 ATI docs)

Every command is exactly 13 semicolon-separated fields, unused fields left
empty rather than omitted:

```
PLACE;<ACCOUNT>;<INSTRUMENT>;<ACTION>;<QTY>;<ORDER TYPE>;[LIMIT];[STOP];<TIF>;[OCO];[ORDER ID];[STRATEGY];[STRATEGY ID]
CANCEL;;;;;;;;;;<ORDER ID>;;[STRATEGY ID]
CLOSEPOSITION;<ACCOUNT>;<INSTRUMENT>;;;;;;;;;;
```

`oif_writer.build_place_command`/`build_cancel_command`/
`build_closeposition_command` build these strings; `write_command` writes
them atomically (temp file + rename) into `NT8_INCOMING_DIR` so NT8 never
observes a partially written command file.

**Not confirmed:** the exact internal field layout of the confirmation
files NT8 writes back to `outgoing` (only that no account-balance/P&L file
exists there, which is why the AddOn exists). `oif_writer.tail_outgoing`
deliberately does not claim to decode those fields' meaning -- it returns
the raw semicolon-split fields plus a best-effort account/instrument/kind
guess from the filename. These records are used for fill-logging only, not
for any risk decision. Verify the real field semantics against actual
output once ATI is enabled on the VPS, before trusting any specific field
from it for anything beyond logging.

## Multi-instance config (`BotConfig`)

Every identity/transport value above (`NT8_INCOMING_DIR`,
`NT8_OUTGOING_DIR`, `NT8_ACCOUNT_ID`, `NT8_INSTRUMENT`, `NT8_LOG_DIR`,
`ACCOUNT_TELEMETRY_JSON_PATH`, `ACCOUNT_TELEMETRY_MAX_STALENESS_SECONDS`,
the fill-wait timeout/poll interval, plus `risk_guard`'s `ALLOWED_SYMBOL`/
`MAX_CONTRACTS`) can now also be passed explicitly, per call, as a
`tradovate_bot.config.BotConfig` -- a frozen dataclass with fields
`account_id`, `instrument`, `allowed_symbol`, `max_contracts`,
`incoming_dir`, `outgoing_dir`, `log_dir`, `telemetry_json_path`,
`telemetry_max_staleness_seconds`, `fill_timeout_seconds`,
`fill_poll_interval_seconds`.

```python
from tradovate_bot import config, executor

cfg = config.BotConfig.from_env()  # reproduces today's env-var configuration exactly
decision = executor.process_order(order, config=cfg)
```

`BotConfig.from_env()` reads the exact same env vars/defaults `executor.py`
reads at import time, so it's a drop-in stand-in for "the bot's current
configuration." This is purely additive (see
odd/tasks/tradovate-architecture-scaling.md task 3): every function in
`executor.py`/`risk_guard.py` keeps an optional `config=None` parameter
that, when omitted, reads the module-level globals exactly as before --
nothing here requires passing a config, and running a second
account/instrument side by side in the same process (instead of a second
process with different env vars) is now possible by constructing two
different `BotConfig`s and passing each to its own calls, though
`executor.py` has no multi-instance *loop* (e.g. a second `main()` running
concurrently against a second config) wired up yet -- that's a separate
future decision.

## Starting / stopping

```sh
python -m tradovate_bot.executor
```

Stop with Ctrl-C. For an always-on Windows process, use NSSM or Windows
Task Scheduler (not systemd -- the deployment target is the Windows VPS,
not Mac/Linux). See `odd/tasks/tradovate-execution-bot.md` task 7 for the
still-open VPS deployment runbook.

## State files

`tradovate_bot/state/decisions.log.jsonl` -- one JSON line per order
decision (pass or reject with reason), never silently dropped. Every record
carries a `stage` naming the check that wrote it: `normalize`, `risk_guard`,
`signals_guard`, `price_drift_gate`, `position_check`, `bracket_config`,
`order_placement`, `bracket_placement`, or `order_sent`. The first record of an
order (`risk_guard`) is written BEFORE the later checks, so its `allow=true`
only means risk_guard passed: an entry was handed to NT8 only when a later
`order_sent` record exists.
`tradovate_bot/state/fills.jsonl` -- one JSON line per placed order (OIF
command sent) and per NT8 outgoing confirmation file observed.

## What is NOT verified yet

1. The NinjaScript AddOn's behavior against a live/demo NT8 instance,
   including whether its explicit 17:00-Eastern day-boundary rollover
   matches the actual boundary Lucid/Tradovate use for the EOD drawdown
   floor recalculation.
2. The exact internal field layout of NT8 ATI's `outgoing` confirmation
   files.
3. Whether NT8 deletes, renames, or otherwise consumes files from
   `incoming` after processing them (affects nothing functionally here,
   since this bot only writes new uniquely-named files, but is worth
   knowing operationally).
4. The whole stack running unattended on the Windows VPS -- blocked on the
   remote-access question in the task doc's Open Questions.
