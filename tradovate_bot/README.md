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

## Required environment variables (current, ATI path)

| Variable | Purpose |
|---|---|
| `NT8_INCOMING_DIR` | NT8 ATI's `incoming` folder (OIF commands are written here) |
| `NT8_OUTGOING_DIR` | NT8 ATI's `outgoing` folder (confirmation files are tailed from here; optional -- logging only) |
| `NT8_ACCOUNT_ID` | The NT8 account *name* string (e.g. `Sim101`), not a Tradovate numeric id |
| `ACCOUNT_TELEMETRY_JSON_PATH` | Path to the AddOn's telemetry JSON file |
| `ACCOUNT_TELEMETRY_MAX_STALENESS_SECONDS` | Max age of telemetry before it's treated as stale (default `30`) |
| `ACCOUNT_TELEMETRY_EXPORT_INTERVAL_SECONDS` | Read by the NinjaScript AddOn only (not `executor.py`): how often it re-exports the telemetry JSON (default `5`) |
| `TRADOVATE_ORDERS_PATH` | Path to the approved-orders file (default `trader/orders.json`) |
| `TRADOVATE_POLL_INTERVAL_SECONDS` | Main loop poll interval (default `5`) |

`executor.main()` refuses to start (exits non-zero) rather than run against
an unconfigured/ambiguous transport if `NT8_INCOMING_DIR`, `NT8_ACCOUNT_ID`,
or `ACCOUNT_TELEMETRY_JSON_PATH` is unset.

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
decision (pass or reject with reason), never silently dropped.
`tradovate_bot/state/fills.jsonl` -- one JSON line per placed order (OIF
command sent) and per NT8 outgoing confirmation file observed.

## What is NOT verified yet

1. The NinjaScript AddOn's behavior against a live/demo NT8 instance,
   including whether its local-midnight day-boundary logic matches the
   actual boundary Lucid/Tradovate use for the EOD drawdown floor
   recalculation.
2. The exact internal field layout of NT8 ATI's `outgoing` confirmation
   files.
3. Whether NT8 deletes, renames, or otherwise consumes files from
   `incoming` after processing them (affects nothing functionally here,
   since this bot only writes new uniquely-named files, but is worth
   knowing operationally).
4. The whole stack running unattended on the Windows VPS -- blocked on the
   remote-access question in the task doc's Open Questions.
