# Feature: Tidy the local market-data MCP

From the 2026-11-18 repo audit ("para ordenar"), approved 2026-11-19.

## Scope
- Rename `tradovate_bot/localMarketDataMCP.py` -> `local_market_data_mcp.py`
  (snake_case like its siblings); update `.mcp.json`, `README.md`,
  `tradovate_bot/README.md`.
- Replace the hard-coded absolute `DATA_DIR` with a repo-relative default
  (`Path(__file__).resolve().parents[1] / "candles"`), overridable with the
  `CANDLES_DIR` env var.
- Robust `updated_utc` parsing (the old string splicing broke on a timestamp
  without fractional seconds and reported `stale: true` for fresh data).
- Make the bare `from time_utils import ...` work both as a script (how
  `.mcp.json` runs it) and as a package import (tests).
- Tests: there were none.
- Small hardening found on the way: `limit <= 0` returned every bar
  (`bars[-0:]`); `symbol`/`timeframe` come from the model and went straight
  into a file path, so both are validated.

## Risk
The MCP server is in use by the overnight cycle. The running process keeps the
code it already loaded, so the rename does not break it; it must be reconnected
with `/mcp` (like after the earlier edits) to pick up the new path.

## Tasks
- [x] T1 Tests first (RED: ImportError, the module did not exist yet): 26 tests.
- [x] T2 Rename + refactor (GREEN): `local_market_data_mcp.py`, repo-relative
      `DATA_DIR` (+ `CANDLES_DIR`), `_parse_utc`, validated symbol/timeframe,
      `limit >= 1`, package-or-script import of `time_utils`.
- [x] T3 `.mcp.json`, README.md, tradovate_bot/README.md updated; suite 263 passed +
      2 known risk_guard failures. Script-mode import verified from another cwd.
- [x] T4 The user reconnected with `/mcp` at ~00:58 ET; the live tool answers with
      fresh data and rejects `../x` with the new error JSON, so the new module
      is the one running. A stray old server process (localMarketDataMCP.py, PID
      5156) was left behind by the reconnect: harmless, it dies when Claude Code exits.

## Acceptance criteria
- The module imports as `tradovate_bot.local_market_data_mcp` and runs as a script.
- `updated_utc` with 7, 6 or 0 fractional digits, with `Z` or an offset, parses.
- No absolute path in the code; `CANDLES_DIR` overrides the default.
- A path-like symbol or an unknown timeframe returns an error JSON.

## Progress
(2026-11-19) All tasks done and verified live. Not committed yet.
