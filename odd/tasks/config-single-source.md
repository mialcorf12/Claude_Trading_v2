# Feature: Single source for the bot's configuration defaults

From the 2026-11-18 repo audit, approved 2026-11-19 together with the .gitignore
and .mcp.json items and "BotConfig is unused scaffolding".

## Problem
`executor.py` read eight env vars at import time and defined
`NT8_INSTRUMENT = "MNQ 12-26"`; `config.BotConfig.from_env()` repeated all of it
(and a second copy of the contract name), so every default change and every
quarterly MNQ roll needed two edits. `BotConfig` was only used by tests, and its
docstring claimed `executor.py` imported it (it did not).

## Done
- `config.DEFAULT_INSTRUMENT` (public) is the only place with the contract name;
  `BotConfig.from_env()` is the only reader of the env vars.
- `executor.py` builds `NT8_*`, `ACCOUNT_TELEMETRY_*` and `FILL_CONFIRMATION_*`
  from `BotConfig.from_env()` (module-level names kept: functions and tests read
  and patch them). `BotConfig` is now used in production.
- Tests first (2 red): the contract name is defined once; `executor.py` has no
  second `os.environ.get` for these vars; a subprocess check that the executor's
  globals follow the env through `BotConfig`. Suite 266 passed + 2 known failures.
- `TradovateExecutor` restarted 2026-11-19 01:11:44 ET (PID 2784, Flat): boots
  with identical values (account Sim101, same ATI folders, same telemetry path).
- README: the quarterly roll instruction now points at `config.DEFAULT_INSTRUMENT`.
- `.gitignore`: added `trader/`, `*.log`, `*.jsonl`, `.mypy_cache/`, `.ruff_cache/`
  (no tracked file matches; verified with `git check-ignore`).

## `.mcp.json` absolute path: NOT changed (decision)
The Claude Code MCP docs do not state the working directory of a stdio server and
document `${CLAUDE_PROJECT_DIR}` only for the server's own environment; in a
project `.mcp.json` it needs a default (`${CLAUDE_PROJECT_DIR:-.}`), which then
resolves to `.`, relative to an undocumented cwd. The absolute path is the only
documented-reliable option, a wrong path would cut the overnight cycle's data
source, and the project lives on one fixed VPS. Revisit only with an empirical
test (a second, throw-away server entry using the relative form, checked in
`/mcp`) when the repo needs to move.
