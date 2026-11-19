"""Pre-trade price-drift gate.

An entry order is decided from the last CLOSED 5-minute bar, but it reaches
NT8 a couple of minutes later. Stop and target are anchored to the actual
fill (see executor.compute_bracket_prices_from_fill), so if the market moves
in between, the whole bracket silently shifts and the trade taken is not the
trade that was analysed. On 2026-11-18 a BUY decided at 27459.5 filled at
27425.25 (-34.25 pts, 1.02x the 33.5 pt stop) and was stopped out two
minutes later. See odd/tasks/price-drift-gate.md.

This module compares the decision price (close of the last bar handed to the
executor in order["bars"]) with the live price (close of the newest
completed 1-minute bar exported by NT8's LocalBarExporter) and refuses the
entry when they differ by more than a fraction of the stop distance.

Fail-closed, like risk_guard and signals: a missing, unreadable, malformed or
stale candles file, or a missing/non-finite decision price, refuses the
entry. The "live" price is the last COMPLETED 1m close, so it can lag by up
to about a minute: this bounds drift, it does not see intra-minute moves.

Pure apart from reading one JSON file; never touches the network.
"""
import json
import math
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from tradovate_bot import time_utils

DEFAULT_CANDLES_DIR = Path(__file__).resolve().parents[1] / "candles"

# Reject when |live - decision| exceeds this fraction of the stop distance
# (0.25 x 33.5 = 8.375 pts for the usual bracket).
MAX_DRIFT_FRACTION = float(os.environ.get("PRICE_DRIFT_MAX_FRACTION", "0.25"))
# Absolute limit used when the order carries no stop_loss_points.
DEFAULT_MAX_DRIFT_POINTS = float(os.environ.get("PRICE_DRIFT_MAX_POINTS", "8.0"))
# The newest 1m bar must be at most this old (it is stamped at bar close, so
# a healthy exporter is never more than ~60 s behind).
MAX_LIVE_BAR_AGE_SECONDS = float(os.environ.get("PRICE_DRIFT_MAX_AGE_SECONDS", "150"))
# Tolerated clock skew for a bar stamped slightly in the future.
FUTURE_SKEW_SECONDS = 30.0

_BAR_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


@dataclass(frozen=True)
class DriftVerdict:
    ok: bool
    reasons: tuple = ()
    decision_price: float = None
    live_price: float = None
    drift: float = None
    threshold: float = None


def _refuse(reason, **fields):
    return DriftVerdict(False, (reason,), **fields)


def _finite(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _decision_price(order):
    bars = order.get("bars")
    if not isinstance(bars, list) or not bars or not isinstance(bars[-1], dict):
        return None
    return _finite(bars[-1].get("close"))


def _threshold(order):
    stop = _finite(order.get("stop_loss_points"))
    if stop is not None and stop > 0:
        return MAX_DRIFT_FRACTION * stop
    return DEFAULT_MAX_DRIFT_POINTS


def _candles_path(order, candles_path):
    if candles_path is not None:
        return str(candles_path)
    symbol = str(order.get("symbol") or "MNQ").strip().upper()
    return str(Path(DEFAULT_CANDLES_DIR) / ("%s_1m.json" % symbol))


def evaluate_entry_drift(order, candles_path=None, now=None):
    """Returns a DriftVerdict. `ok` is True only when the live 1m close is
    fresh and within the allowed distance of the decision price."""
    decision_price = _decision_price(order)
    if decision_price is None:
        return _refuse("price-drift gate: order has no usable decision price (bars[-1].close)")

    threshold = _threshold(order)
    path = _candles_path(order, candles_path)
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            payload = json.load(fh)
        last_bar = payload["bars"][-1]
        live_price = _finite(last_bar["close"])
        bar_time = datetime.strptime(str(last_bar["time"]), _BAR_TIME_FORMAT)
    except (OSError, ValueError, KeyError, IndexError, TypeError) as exc:
        return _refuse(
            "price-drift gate: cannot read a live 1m price from %s (%s: %s)" % (path, type(exc).__name__, exc),
            decision_price=decision_price, threshold=threshold,
        )
    if live_price is None:
        return _refuse("price-drift gate: live 1m close is not a finite number",
                       decision_price=decision_price, threshold=threshold)

    now = now if now is not None else time_utils.now_et()
    age = (now - bar_time.replace(tzinfo=time_utils.EASTERN)).total_seconds()
    if age > MAX_LIVE_BAR_AGE_SECONDS or age < -FUTURE_SKEW_SECONDS:
        return _refuse(
            "price-drift gate: live 1m bar is stale or mis-stamped (age %.0f s, allowed -%.0f..%.0f s); "
            "the exporter may be down" % (age, FUTURE_SKEW_SECONDS, MAX_LIVE_BAR_AGE_SECONDS),
            decision_price=decision_price, live_price=live_price, threshold=threshold,
        )

    drift = abs(live_price - decision_price)
    if drift > threshold:
        return _refuse(
            "price-drift gate: live price %.2f is %.2f pts from the decision price %.2f "
            "(limit %.2f pts); the setup no longer exists" % (live_price, drift, decision_price, threshold),
            decision_price=decision_price, live_price=live_price, drift=drift, threshold=threshold,
        )
    return DriftVerdict(True, (), decision_price, live_price, drift, threshold)
