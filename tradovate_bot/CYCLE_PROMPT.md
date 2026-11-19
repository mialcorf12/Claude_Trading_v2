# MNQ overnight trading cycle -- prompt executed on every clock tick

Triggered by a `CYCLE_TICK <iso-ET> late=<s>` line from
`python -u -m tradovate_bot.cycle_clock` (run under the Monitor tool; ticks at
:05, :15, :25, :35, :45, :55 ET). Edit THIS file to change the cycle; the
session re-reads it on every tick. If `late` is above 120 s, say so in the
cycle notes: the bars are older than planned, so rely on the drift gate and
the freshness block rather than on timing.

Authoritative procedure: the "Cron procedure (current, authoritative)" section
of `odd/tasks/tradovate-execution-bot.md` -- steps 1-12 and its Execution
notes. Read-only on that doc: NEVER Edit/Write either `odd/tasks` doc from
this cycle; log only via `executor.log_cycle` to
`tradovate_bot/state/cycles.log.jsonl`.

TIMING: the tick arrives at the exact second a 5-minute bar closes (:x5:00 /
:x0:00), and NT8's exporter writes that bar within ~1 s. Latency is the enemy:
the decision uses the last closed bar and the order fills later. Be fast and
add no extra steps. The ONE allowed retry is the bar check below.

MODEL ROUTING: read the "Overnight cron cycle" table in `CLAUDE.md` at the
start of every cycle. For each step whose model is `haiku`, delegate that step
to a subagent (Agent tool, `model` set to the table's value) with the exact
commands/inputs and ask for a short structured result (no narrative). Steps
marked `sonnet` run inline. If the table changed, follow it as it stands now.
Subagents never place orders or write `trader/orders.json`.

## Flow

**1+2 (haiku) -- launch BOTH subagents in the SAME message so they run in
parallel:**
1. Batched read-only Bash checks: git branch/status, current ET time via plain
   `date`, position via `tradovate_bot.position_state.read_position_state(NT8
   log dir, "Sim101")`, `risk_guard.check_trading_hours`, NSSM
   `sc queryex TradovateExecutor` + process start time. Return: branch, ET
   time, position, within_hours, service state/PID/start.
2. The `local-market-data` MCP `get_local_candles` (MNQ, 5m, limit 10) --
   NEVER TradingView (`mcp-tv-*`) or Yahoo -- return the raw bars plus the
   `freshness` block.

Bar check (race with the exporter at the tick): the last bar returned by step 2
must be the bar that just closed, i.e. its `time` equals the latest 5-minute
boundary at or before the tick (tick :05 -> bar stamped :05; tick :15 -> :15).
If `last_bar_age_seconds` is above ~120 s, the exporter has not written the new
bar yet: wait about 3 s and re-fetch ONCE (a single haiku call). If it is still
the old bar, skip to step 11 and log it as a stale-bar skip; never decide on a
bar one step behind.

If step 1 says not Flat or outside trading hours, ignore the bars and skip to
step 11. If step 2 has `error` or `freshness.stale == true`, skip to step 11
and log the freshness fields as the reason.

**3-4 (sonnet): judge BUY/SELL from structure.** Prefer a CONFIRMED pullback
over chasing an extended move; a zone that reversed several times recently is
chop. Stricter rules from the 2026-11-18 post-mortem (a BUY on a "pullback" was
stopped out in 2 minutes):
- (a) name the trend you are pulling back WITHIN; a rally that was fully
  retraced (price back at or below its origin, new low) is a failed rally, NOT
  a pullback;
- (b) a "higher low" must be higher by clearly more than a couple of ticks,
  otherwise it is a double bottom/top;
- (c) do not buy into the middle of a support-turned-resistance zone, and check
  that the +TP target is not sitting on a ceiling tested several times;
- (d) two closes off a low is not confirmation.

`signals.evaluate_signal(bars, 'buy'|'sell')` is lowercase-only. It runs three
guards: chase, chop and a direction guard that refuses a BUY after a fully
retraced rally (a SELL after a fully retraced decline). It is still only a
backstop: it does not confirm that the trend exists or that the stop is
structural, so "clear" is not an edge. No clear edge -> skip.

**5 (haiku):** if a signal exists, compute `signals.select_bracket_points(bars)`
on the same 9-bar window and return the (SL, TP) pair.

**6-9 (sonnet, inline):**

*Thesis (required before any order).* State ONE line and keep it: `TREND:
<up|down> leg <origin> -> <extreme>; pullback low/high <x> vs origin <y>
(retraced <n>%); <why now>`. If you cannot name a trend and a leg whose origin
was NOT reached again by the pullback, it is a skip, not an order. Put the
thesis at the start of `signal_reason` in the cycle log.

*Re-read right before writing (one call, no loop).* Call `get_local_candles`
(MNQ, timeframe `1m`, limit 3) and compare its last close with `bars[-1].close`
of the 5m decision. If they differ by more than 0.25 x the stop (the same limit
the executor enforces, about 8.4 pts for a 33.5 pt stop), skip and log "pre-order
drift: decision <a>, live <b>". Do not re-decide on the fresh price.

*Write.* `trader/orders.json` as a JSON list: symbol MNQ, side lowercase, qty
3, type market, `stop_loss_points`, `take_profit_points` and the exact `bars`
list from the MCP. Write it IMMEDIATELY after the re-read. The executor's
price-drift gate uses
`bars[-1].close` as the decision price and compares it with the live 1m close
in `candles/MNQ_1m.json`: it refuses an entry that drifted more than 0.25 x
stop (about 8.4 pts), or when the live 1m price is unreadable or stale. If
`decisions.log.jsonl` shows a "price-drift gate" refusal, do NOT rewrite or
retry; log the cycle as skipped with that reason.

*Confirm placement.* Every record in `tradovate_bot/state/decisions.log.jsonl`
has a `stage`. The first one (`risk_guard`) has `allow=true` BEFORE the signals,
drift and position checks, so it does NOT mean an order was placed. An order is
placed only when a later record has `stage: "order_sent"`; a refusal ends with a
record whose `stage` names the guard that refused it (`signals_guard`,
`price_drift_gate`, `position_check`, `bracket_config`, ...). Then confirm NT8's
own confirmation (position + bracket in the NT8 log), and respect any
risk_guard/signals rejection -- never route around it.

**10:** folded into step 1 (service started after the last
`tradovate_bot/*.py` change; uncommitted edits are not visible to `git log`).

**11 (haiku):** log every cycle, skips included, with `executor.log_cycle` in
one python call (state outcome, signal_reason and notes explicitly in the
delegation). If a position opened by a previous cycle has since closed
(stop/target), say so in the notes with entry, exit and P&L from the NT8 log.

**12:** nothing to clean unless a one-shot order file was written outside
`run_once`.
