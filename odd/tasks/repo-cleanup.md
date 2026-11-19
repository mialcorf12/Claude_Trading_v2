# Feature: Repo cleanup (high-confidence deletions)

Source: the 2026-11-18 read-only repo audit (Engram topic
`cleanup/repo-audit-2026-11-18`). The user approved only the high-confidence
deletions on 2026-11-19 and asked to KEEP `Restart_VSC.txt`.

## Done
- Deleted `tradovate_bot/client.py` (only importer: its own test) and
  `tests/tradovate_bot/test_client.py`.
- Deleted `tradovate_bot/RUNBOOK_DEMO.md` (superseded, macOS-only).
- README: removed the `client.py` module bullet and the "Historical/moot env
  vars" section (client-only `TRADOVATE_USERNAME/PASSWORD/API_KEY/API_SECRET/
  ACCOUNT_ID/DEVICE_ID/ENV`), with a pointer to git history.
- `time_utils.py` docstring no longer mentions `client.py`; `RUNBOOK_VPS.md`
  installs only `pytest` (`requests` was used only by `client.py`).
- NOT touched: `TRADOVATE_ORDERS_PATH`, `TRADOVATE_POLL_INTERVAL_SECONDS`,
  `TRADOVATE_FILL_TIMEOUT_SECONDS`, `TRADOVATE_FILL_POLL_INTERVAL_SECONDS` (used
  by `executor.py`/`config.py`); historical `odd/tasks/*.md` mentions; and
  `Restart_VSC.txt` (kept by request).

## Still open from the audit (not approved yet)
Medium confidence: DONE 2026-11-19 (see "Archived" below); `config.py`/`BotConfig`
is unused scaffolding.
Tidy: hard-coded `DATA_DIR` and bare `from time_utils import` in
`localMarketDataMCP.py` (DONE 2026-11-19: see mcp-tidy.md; absolute paths in `.mcp.json` remain),
env defaults duplicated between `config.py` and `executor.py` (the contract roll
needs two edits), `.gitignore` gaps (`trader/`, caches), the 2 failing
risk_guard consistency tests, README `python3 -m pytest`.

## Archived (2026-11-19)
Moved verbatim to `odd/tasks/archive/` (nothing deleted or rewritten except path
references): `tradovate-execution-no-necessary.md` (228 KB progress log),
`eastern-time-migration.md` (all 7 tasks done) and
`tradovate-architecture-scaling.md` (all 4 tasks and every acceptance criterion
done; task 3, `BotConfig`, is implemented in `config.py`). The ~40 references in
code comments, tests and docs now point at `odd/tasks/archive/...`.
