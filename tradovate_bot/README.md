# Tradovate Deterministic Execution Bot

A standalone, always-on process that places/closes MNQ orders on Tradovate
using fixed, non-LLM rules. It is the execution layer below Claude's
`day-trader-skill` pack (scanner/analyst/risk/broker), which stays the
signal layer. No LLM call exists anywhere in `tradovate_bot/`.

## Modules

- `client.py` -- Tradovate REST + WebSocket client (auth, token refresh,
  order placement/cancel/flatten, account/position queries, reconnecting
  WebSocket listener for fills/account updates).
- `risk_guard.py` -- pure, deterministic functions (no I/O) implementing
  the Lucid Trading 25k Flex Eval risk rules. Fails closed on any missing
  or malformed input.
- `executor.py` -- main loop: reads an approved order (same shape as
  `trader-broker`'s `trader/orders.json`), risk-checks it against live
  account state, places it only if every check passes, and logs every
  decision and fill.

## Required environment variables

| Variable | Purpose |
|---|---|
| `TRADOVATE_USERNAME` | Tradovate account username |
| `TRADOVATE_PASSWORD` | Tradovate account password |
| `TRADOVATE_API_KEY` | Tradovate app id / client id |
| `TRADOVATE_API_SECRET` | Tradovate app client secret |
| `TRADOVATE_ACCOUNT_ID` | Numeric Tradovate account id to trade |
| `TRADOVATE_DEVICE_ID` | Any stable per-machine id (e.g. hostname-derived) |
| `TRADOVATE_ENV` | `demo` (default) or `live` |

None of these are ever hardcoded in source. `client.py` raises
`TradovateConfigError` immediately if any required variable is missing.

## Demo vs. live

The bot defaults to Tradovate's **demo** environment. Switching to the live
(funded) endpoint requires explicitly setting:

```sh
export TRADOVATE_ENV=live
```

There is no code path that defaults to live -- it is opt-in only, by the
human setting this one variable.

## Starting / stopping

Simplest option, foreground:

```sh
python3 -m tradovate_bot.executor
```

Stop with Ctrl-C; it stops the WebSocket listener cleanly before exiting.

For a long-running background process, a minimal systemd user unit:

```ini
# ~/.config/systemd/user/tradovate-bot.service
[Unit]
Description=Tradovate deterministic execution bot

[Service]
WorkingDirectory=/path/to/Claude_Trading_v2
EnvironmentFile=/path/to/Claude_Trading_v2/.env.tradovate
ExecStart=/usr/bin/python3 -m tradovate_bot.executor
Restart=on-failure

[Install]
WantedBy=default.target
```

```sh
systemctl --user daemon-reload
systemctl --user enable --now tradovate-bot
systemctl --user status tradovate-bot
journalctl --user -u tradovate-bot -f
```

`.env.tradovate` holds the environment variables above (one `KEY=value`
per line); keep it out of version control.

## State files

`tradovate_bot/state/decisions.log.jsonl` -- one JSON line per order
decision (pass or reject with reason), never silently dropped.
`tradovate_bot/state/fills.jsonl` -- one JSON line per placed order /
WebSocket fill event.

## What is NOT verified yet

This was built without real Tradovate demo credentials. Unit tests mock
all HTTP/WebSocket responses. Before pointing this at any account, real
money or not:

1. Run against actual Tradovate demo credentials and confirm
   `authenticate()`, `place_order()`, `cancel_order()`, and
   `flatten_position()` match Tradovate's real response shapes (field
   names in this client are based on Tradovate's documented REST surface,
   not a live call made from this environment).
2. Confirm the WebSocket authorize handshake and frame format
   (`_split_frames`) against a real connection -- Tradovate's protocol
   detail (SockJS-style framing) was implemented from documentation, not
   observed live.
3. Confirm `fetch_account_state`'s field mapping
   (`netLiquidationValue`, `endOfDayNetLiquidation`, `realizedPnl`, etc.)
   against the real `/cashBalance/getcashbalancesnapshot` response for
   this specific account type.
