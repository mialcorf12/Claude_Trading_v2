"""Reads account-state telemetry produced by the read-only NinjaScript AddOn
(`ninjatrader_addon/AccountTelemetryAddOn.cs`) and maps it onto exactly the
fields `risk_guard.evaluate_order` requires.

This is the ONLY account-state channel risk_guard's kill switches trust.
NT8 ATI's `outgoing` order/position confirmation files (see oif_writer.py)
are a separate, lower-trust channel used for fill logging only -- they do
not feed this function, by design (see odd/tasks/tradovate-execution-bot.md
pivot #3: "no account balance / realized P&L file exists in ATI's outgoing
set").

Expected JSON contract written by the AddOn (flat, 1:1 field names):

    {
      "updated_at": "<ISO 8601 with explicit America/New_York offset, e.g. 2026-11-09T09:32:05-05:00>",
      "daily_realized_pnl": <number>,
      "eod_balance_last_close": <number>,
      "current_balance": <number>,
      "daily_profit_today": <number>,
      "total_realized_profit": <number>
    }

Fail-closed contract: a missing file, unparsable JSON, a missing or
non-numeric required field, or a stale `updated_at` (older than
max_staleness_seconds -- meaning the AddOn has stopped updating, e.g. NT8
crashed or disconnected) all return `{}`. An empty dict makes
risk_guard.evaluate_order reject for "account_state is missing required
field", the same fail-closed path as any other ambiguous input. This
function never raises for a file/parse/data problem.
"""
import json
import math
import os
from datetime import datetime

from tradovate_bot import time_utils

REQUIRED_FIELDS = (
    "daily_realized_pnl",
    "eod_balance_last_close",
    "current_balance",
    "daily_profit_today",
    "total_realized_profit",
)


def _is_finite_number(v):
    if isinstance(v, bool):
        return False
    if not isinstance(v, (int, float)):
        return False
    try:
        return math.isfinite(v)
    except (TypeError, ValueError):
        return False


def _parse_updated_at(value):
    """Parses an ISO 8601 timestamp with an explicit America/New_York offset,
    e.g. "2026-11-09T09:32:05-05:00". Also accepts a trailing "Z" (UTC) as
    harmless backward-compatible defensive parsing. Returns None if value
    isn't a parseable string."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=time_utils.EASTERN)
    return parsed


def read_account_state(json_path, max_staleness_seconds=30, now=None):
    """Reads and validates the AddOn's telemetry JSON file. Returns a dict
    with exactly REQUIRED_FIELDS on success, or {} on any failure."""
    now = now if now is not None else time_utils.now_et()

    try:
        with open(json_path, encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}

    if not isinstance(payload, dict):
        return {}

    updated_at = _parse_updated_at(payload.get("updated_at"))
    if updated_at is None:
        return {}
    age_seconds = (now - updated_at).total_seconds()
    if age_seconds < 0 or age_seconds > max_staleness_seconds:
        return {}

    state = {}
    for field in REQUIRED_FIELDS:
        value = payload.get(field)
        if not _is_finite_number(value):
            return {}
        state[field] = value
    return state
