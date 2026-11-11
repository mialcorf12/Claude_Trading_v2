"""Explicit, optional configuration object for the Tradovate execution
bot's identity/transport values -- account id, instrument, allowed symbol,
max contracts, NT8 ATI folders, telemetry path/staleness, and fill-wait
timing.

Why this exists: `executor.py` and `risk_guard.py` read these values as
module-level globals (mostly `os.environ.get(...)` at import time), so
running a second account or instrument today means a second *process*
with different env vars, not a parameterized call -- see
odd/tasks/tradovate-architecture-scaling.md task 3. `BotConfig` lets a
caller pass an explicit identity/transport bundle into `risk_guard.
evaluate_order`/`executor.*` instead, so two configs could eventually run
side by side in the same process/tests without monkeypatching module
globals.

Purely additive: nothing here is required. Every function in executor.py/
risk_guard.py keeps reading its module-level globals exactly as before
when no `BotConfig` is passed (`config=None`, the default everywhere);
passing one overrides those globals for that call only.

`BotConfig.from_env()` reads the exact same env vars, with the exact same
defaults, that `executor.py` reads at import time (for `allowed_symbol`/
`max_contracts`, it defaults from `risk_guard.ALLOWED_SYMBOL`/
`risk_guard.MAX_CONTRACTS`), so it reproduces today's real configuration
exactly -- a drop-in stand-in for "the bot's current configuration"
wherever a caller wants to start threading an explicit config through.

`instrument` is NOT env-backed here either, for the same reason it isn't
in executor.py: NT8 rejects the generic "MNQ" root outright and needs its
own per-contract-roll display name (e.g. "MNQ 12-26"). Update
`_DEFAULT_INSTRUMENT` below by hand on the same quarterly roll schedule as
`executor.NT8_INSTRUMENT` -- the two are intentionally kept as separate
constants (not one importing the other) so this module has no import-time
dependency on executor.py.
"""
import os
from dataclasses import dataclass

from tradovate_bot import risk_guard

# Mirrors executor.NT8_INSTRUMENT. Kept as its own constant (not imported
# from executor.py) to avoid a module-level import cycle: executor.py
# imports this module to build its own default config, so this module
# must not import executor.py back.
_DEFAULT_INSTRUMENT = "MNQ 12-26"


@dataclass(frozen=True)
class BotConfig:
    account_id: str
    instrument: str
    allowed_symbol: str
    max_contracts: int
    incoming_dir: str
    outgoing_dir: str
    log_dir: str
    telemetry_json_path: str
    telemetry_max_staleness_seconds: float
    fill_timeout_seconds: float
    fill_poll_interval_seconds: float

    @classmethod
    def from_env(cls):
        """Reproduces executor.py's current real configuration: the exact
        same env vars and defaults it reads at import time."""
        return cls(
            account_id=os.environ.get("NT8_ACCOUNT_ID", ""),
            instrument=_DEFAULT_INSTRUMENT,
            allowed_symbol=risk_guard.ALLOWED_SYMBOL,
            max_contracts=risk_guard.MAX_CONTRACTS,
            incoming_dir=os.environ.get("NT8_INCOMING_DIR", ""),
            outgoing_dir=os.environ.get("NT8_OUTGOING_DIR", ""),
            log_dir=os.environ.get("NT8_LOG_DIR", ""),
            telemetry_json_path=os.environ.get("ACCOUNT_TELEMETRY_JSON_PATH", ""),
            telemetry_max_staleness_seconds=float(
                os.environ.get("ACCOUNT_TELEMETRY_MAX_STALENESS_SECONDS", "30")
            ),
            fill_timeout_seconds=float(os.environ.get("TRADOVATE_FILL_TIMEOUT_SECONDS", "5")),
            fill_poll_interval_seconds=float(
                os.environ.get("TRADOVATE_FILL_POLL_INTERVAL_SECONDS", "0.5")
            ),
        )
