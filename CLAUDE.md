# Claude_Trading_v2 -- project instructions

## Model routing (token saving)

Edit the tables below to change which model does what. Values are the
`model` override of the Agent tool: `haiku`, `sonnet`, `opus`.

The main session's own model is set by the user with `/model` and cannot be
changed by Claude. A row only takes effect where work is delegated to a
subagent; anything not delegated runs on the main session's model.
Cron cycles fire into the main session.

### General tasks

| Task                                                         | Model  |
| ------------------------------------------------------------ | ------ |
| git status / diff / add / commit / push                      | haiku  |
| Log tailing, file moves, mechanical doc edits (exact text given) | haiku |
| Design, debugging, code changes in `tradovate_bot/*.py`      | sonnet |
| Signal / risk logic, code review                             | sonnet |

Git rules: Conventional Commits, no `Co-Authored-By` or AI attribution.
Commit and push only when the user asks.

### Overnight cron cycle (steps from `odd/tasks/tradovate-execution-bot.md`, "Cron procedure")

| Step | What                                                              | Model  |
| ---- | ----------------------------------------------------------------- | ------ |
| 1    | Position / branch / time / NSSM checks (one batched Bash call)    | haiku  |
| 2    | Fetch bars: `get_local_candles` MNQ 5m x10, check `freshness`     | haiku  |
| 3    | Judge BUY / SELL from structure (pullback vs chase, chop)         | sonnet |
| 4    | Skip decision when there is no clear edge                         | sonnet |
| 5    | `signals.select_bracket_points(bars)` for SL/TP                   | haiku  |
| 6    | Write `trader/orders.json` (list shape, with `bars`)              | sonnet |
| 7    | Run the executor                                                  | sonnet |
| 8    | Verify outcome against NT8 log and outgoing/incoming folders      | sonnet |
| 9    | Handle risk_guard / signals-guard rejections                      | sonnet |
| 10   | Sanity-check branch, live code and `TradovateExecutor` service    | haiku  |
| 11   | `executor.log_cycle(...)` record                                  | haiku  |
| 12   | Cleanup of one-shot order files                                   | haiku  |

Steps 6-9 touch real orders, so they stay on sonnet. Do not move them to
haiku without a deliberate decision.

### Cycle scheduler (own clock, not CronCreate)

CronCreate adds a fixed per-job offset (up to half the period), so it cannot
fire at an exact minute. The cycle is driven by `tradovate_bot/cycle_clock.py`
under the Monitor tool: it prints `CYCLE_TICK <iso-ET> late=<s>` at :05, :15,
:25, :35, :45 and :55 ET (the exact second every second 5m bar closes).
On each tick, read `tradovate_bot/CYCLE_PROMPT.md` and run that cycle.

- Start / re-arm: `Monitor(command="cd C:/TradingPlatform_v2/Claude_Trading_v2
  && python -u -m tradovate_bot.cycle_clock 2>&1", timeout_ms=1800000)`.
- A Monitor lives at most 30 minutes (the tool caps `timeout_ms` at 1800000,
  longer values are cut) and dies with the session: re-arm it on every expiry
  notice, and re-create it when a new session starts. Do not also run a
  CronCreate cycle (duplicate cycles).
- Re-arm schedule: at minute :10 and :40 of every hour. A Monitor armed at :10
  covers the ticks :15/:25/:35 and expires at :40, between ticks, so an expiry
  never lands on a tick. If a late re-arm or a clock jump leaves it off that
  grid, stop it and realign to the next :10/:40.
- The machine clock must be right: `cycle_clock` and the freshness checks use
  it. If the VPS was suspended, check it against an external source
  (`curl -sI https://www.microsoft.com | grep -i ^date`) and resync with
  `w32tm /resync /force` (the Windows Time service must be running; on
  2026-11-19 it was Disabled and the clock stayed ~14 min behind).
- Change the cycle by editing `CYCLE_PROMPT.md`, not the Monitor.

## Market data

Bars come only from the local NT8 feed (`get_local_candles` in the
`local-market-data` MCP). Do not use TradingView or Yahoo. Skip the cycle
when `freshness.stale` is true or the response has an `error`. See
"Local market data" in `tradovate_bot/README.md`.

## Docs

Never edit `odd/tasks/*.md` from a cron cycle; log only via
`executor.log_cycle` to `tradovate_bot/state/cycles.log.jsonl`.
