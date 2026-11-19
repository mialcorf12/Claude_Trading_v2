"""The bot's identity/transport configuration: ONE place that reads the env
vars, owns their defaults and owns the MNQ contract name.

`BotConfig.from_env()` reads every env var (NT8 account, ATI folders, NT8 log
dir, telemetry path/staleness, fill-wait timing) with its default, and takes
`allowed_symbol`/`max_contracts` from `risk_guard`. `executor.py` builds its
module-level names (NT8_ACCOUNT_ID, NT8_INSTRUMENT, FILL_CONFIRMATION_*, ...)
from `BotConfig.from_env()` at import time, so they can never drift from this
class -- before 2026-11-19 each value was read in both files and the quarterly
contract roll needed two edits. Functions in executor.py/risk_guard.py still
accept an explicit `config` (default None = those module-level names), so two
configs can run side by side in tests or in one process; see
odd/tasks/archive/tradovate-architecture-scaling.md task 3.

`instrument` is NOT env-backed, by explicit choice: NT8 rejects the generic
"MNQ" root and needs its own per-contract-roll display name ("<ROOT> <MM-YY>",
e.g. "MNQ 12-26"). MNQ rolls quarterly (Mar/Jun/Sep/Dec): on each roll edit
`DEFAULT_INSTRUMENT` below, the only place, to match NT8's display name for the
new front-month contract.

This module must not import executor.py (executor.py imports it).
"""
import os
from dataclasses import dataclass

from tradovate_bot import risk_guard

# The MNQ contract name NT8 expects. THE one place to edit on the quarterly roll.
DEFAULT_INSTRUMENT = "MNQ 12-26"


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
            instrument=DEFAULT_INSTRUMENT,
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
