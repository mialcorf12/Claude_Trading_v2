# Eastern time migration (America/New_York)

## Objective

Replace `timezone.utc` usage across the project with explicit US Eastern
time (America/New_York), and fix the already-flagged local-midnight
day-boundary risk in the NinjaScript AddOn by rolling the trading day over
explicitly at 17:00 Eastern instead of local VPS machine time.

## Why

User preference: all project dates should use Eastern time instead of UTC.
Confirmed scope: also fix the AddOn's day-boundary EOD-drawdown kill-switch
clock, which the AddOn's own comments already flagged as unverified risk
(`DateTime.Now.Date` = local machine time, not an explicit market-relevant
boundary). User specified the boundary must be 17:00 New York time.

## Scope / constraints

- No DB/ORM, no requirements.txt/pyproject.toml exist in this repo.
- Python 3.9+ stdlib `zoneinfo` is available; no new dependency needed.
- Cross-language contract: C# AddOn (producer of `updated_at`) and
  `account_state.py` (consumer) must change in lockstep.
- Out of scope: `client.py` (OAuth token epoch), `oif_writer.py` (filename
  uniqueness epoch), `risk_guard.py` (no clock access), `RUNBOOK_VPS.md`
  (unrelated pre-existing uncommitted rewrite, no UTC/Eastern mentions).

## Allowed edit surfaces

```
tradovate_bot/time_utils.py
tradovate_bot/executor.py
tradovate_bot/account_state.py
tests/tradovate_bot/test_account_state.py
tests/tradovate_bot/test_executor.py
ninjatrader_addon/AccountTelemetryAddOn.cs
tradovate_bot/README.md
```

## Tasks

- [x] T1. Create `tradovate_bot/time_utils.py` with `EASTERN`, `now_et()`,
      `now_et_iso()`.
- [x] T2. Update `executor.py` to use `time_utils.now_et_iso()`; drop
      `_utc_now_iso()` and the unused `timezone` import.
- [x] T3. Update `account_state.py`: docstring example, naive-fallback tz,
      `read_account_state`'s `now` default.
- [x] T4. Update `AccountTelemetryAddOn.cs`: `EasternZone` field,
      Eastern-offset `updated_at`, explicit 17:00-ET day-boundary rollover
      (rename `currentTradingDay` -> `currentTradingDayBoundary`), rewritten
      comment.
- [x] T5. Update `tradovate_bot/README.md`'s "What is NOT verified yet"
      item 1 wording.
- [x] T6. Update `test_account_state.py` fixtures to Eastern offsets
      (EST `-05:00` for 2026-11-09) and `test_executor.py`'s fixture.
- [x] T7. Run `python -m pytest tests\tradovate_bot\ -q`, confirm all pass.

## Acceptance criteria

- No remaining `timezone.utc` / `DateTime.UtcNow` usage for business
  timestamps in the listed files (epoch-based non-business timestamps in
  `client.py`/`oif_writer.py` are explicitly excluded).
- Full test suite passes.
- C# changes cannot be compiled/run here (no NT8/remote access) — flagged
  as still-unverified-live in the README, same as before.

## Progress / verification evidence

Implementation delegated to one writer agent (7-file change, writer
trigger). Writer ran the pytest command directly:

```
python -m pytest tests\tradovate_bot\ -q
.....................................................................    [100%]
69 passed in 0.80s
```

69 passed, same count as before the change (no tests added or removed; all
existing timestamp-dependent tests, including the staleness-window ones in
`test_account_state.py`, pass with the Eastern-offset fixtures). The C#
AddOn change (`AccountTelemetryAddOn.cs`) cannot be compiled/run in this
environment (no NT8/.NET toolchain) -- it remains unverified-by-build,
consistent with the README wording.

## Route declaration

Delegated direct (writer trigger: 2+ non-trivial files, 7 total).
