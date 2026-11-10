"""Single source of truth for business timestamps across tradovate_bot.

All business-facing timestamps (decision/fill logs, account-state staleness
checks) use explicit US Eastern time (America/New_York) rather than UTC, per
project convention. Non-business clock reads (OAuth token expiry in
client.py, filename-uniqueness epochs in oif_writer.py) are out of scope and
untouched.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")


def now_et() -> datetime:
    return datetime.now(EASTERN)


def now_et_iso() -> str:
    return now_et().isoformat(timespec="seconds")
