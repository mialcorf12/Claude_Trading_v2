"""Reads NT8's own per-instance diagnostic log for the current market
position on a given account, so one-position-at-a-time and auto-flatten-
on-close can be real, tested Python code instead of relying on an LLM
grepping this same log by hand every cron cycle (see
odd/tasks/tradovate-architecture-scaling.md task 1).

This is NOT the ATI "outgoing" folder oif_writer.py tails -- it is NT8's
own windowed diagnostic log, written under
"...NinjaTrader 8/log/*.en.txt", one file per NT8 instance/session. A
sample line looks like:

    2026-11-10 18:52:06:109|1|64|Instrument='MNQ 12-26' Account='Sim101' Average price=27971.75 Quantity=2 Market position=Long Operation=Update

Fail-closed contract: `parse_position_state`/`read_position_state` return
PositionState(side="unknown", ...) for anything that can't be confidently
read as Flat/Long/Short for the requested account -- a missing/unreadable
log directory, no `*.en.txt` file, no matching `Account='<name>'` line, or
empty content. "unknown" is a distinct side, never coerced to "Flat": a
caller must explicitly check for it and refuse a new entry / flatten
defensively, exactly like an ambiguous account_state read in
account_state.py. No I/O exception escapes read_position_state -- it
never raises.

NT8 also writes execution-leg lines (per-fill fragments, containing
"Execution=") that carry their own Account=/Market position=/Quantity=
describing that one fill, not the account's net position -- these are
skipped; only the net-position line it writes alongside each leg
(containing "Average price=") is authoritative. Verified against a real
live NT8 log, 2026-11-10.
"""
import glob
import os
import re
from dataclasses import dataclass

_ACCOUNT_RE = re.compile(r"Account='([^']*)'")
_POSITION_RE = re.compile(r"Market position=(\S+)")
_QUANTITY_RE = re.compile(r"Quantity=(-?\d+(?:\.\d+)?)")

_VALID_SIDES = ("Flat", "Long", "Short")


@dataclass(frozen=True)
class PositionState:
    side: str  # "Flat" | "Long" | "Short" | "unknown"
    qty: float = 0.0


_UNKNOWN = PositionState(side="unknown", qty=0.0)


def parse_position_state(log_text, account):
    """Pure function: scans log_text line by line for the most recent
    `Account='<account>'` line that also carries a recognized
    `Market position=` value, and returns its side/qty as a PositionState.
    Later lines win over earlier ones (NT8 appends chronologically, so the
    last match in the text is the most recent). Returns PositionState
    "unknown" if account is falsy, log_text isn't a string, or no matching
    line is found -- never guesses Flat or an open side."""
    if not account or not isinstance(log_text, str):
        return _UNKNOWN

    latest = None
    for line in log_text.splitlines():
        if "Execution=" in line:
            # Execution-leg lines (per-fill fragments, one per partial
            # fill) also carry Account=/Market position=/Quantity=, but
            # those describe that one fill, not the account's net
            # position -- only the net-position line NT8 writes alongside
            # each leg (recognizable by "Average price=", no "Execution=")
            # is authoritative. Verified against a real live NT8 log,
            # 2026-11-10 (see odd/tasks/tradovate-architecture-scaling.md).
            continue
        account_match = _ACCOUNT_RE.search(line)
        if not account_match or account_match.group(1) != account:
            continue
        position_match = _POSITION_RE.search(line)
        if not position_match or position_match.group(1) not in _VALID_SIDES:
            continue
        quantity_match = _QUANTITY_RE.search(line)
        qty = float(quantity_match.group(1)) if quantity_match else 0.0
        latest = PositionState(side=position_match.group(1), qty=qty)

    return latest if latest is not None else _UNKNOWN


def find_latest_log_file(log_dir):
    """Finds the newest `*.en.txt` file in log_dir by mtime (the same idea
    as the `ls -t *.en.txt | head -1` pattern used manually this session).
    Returns None if log_dir is falsy, not a directory, or has no matching
    file -- never raises."""
    if not log_dir:
        return None
    try:
        if not os.path.isdir(log_dir):
            return None
        candidates = glob.glob(os.path.join(log_dir, "*.en.txt"))
    except OSError:
        return None
    if not candidates:
        return None
    try:
        return max(candidates, key=os.path.getmtime)
    except OSError:
        return None


def read_position_state(log_dir, account):
    """Thin wrapper: finds the newest `*.en.txt` file under log_dir (see
    find_latest_log_file) and parses it for account's current position.
    Fail-closed and exception-free: an unconfigured/missing log_dir, a
    missing/unreadable log file, or no matching line all return
    PositionState "unknown" rather than raising or guessing Flat."""
    if not log_dir or not account:
        return _UNKNOWN

    path = find_latest_log_file(log_dir)
    if path is None:
        return _UNKNOWN

    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return _UNKNOWN

    return parse_position_state(text, account)
