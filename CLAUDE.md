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

## Market data

Bars come only from the local NT8 feed (`get_local_candles` in the
`local-market-data` MCP). Do not use TradingView or Yahoo. Skip the cycle
when `freshness.stale` is true or the response has an `error`. See
"Local market data" in `tradovate_bot/README.md`.

## Docs

Never edit `odd/tasks/*.md` from a cron cycle; log only via
`executor.log_cycle` to `tradovate_bot/state/cycles.log.jsonl`.
