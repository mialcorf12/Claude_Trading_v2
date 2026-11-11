"""Pure, deterministic risk checks for the Tradovate execution bot.

No I/O, no network calls, no LLM calls, no clock access beyond what callers
pass in. Every function here is a plain function of its inputs so it can be
unit tested exhaustively and reasoned about with certainty on a funded
account.

Lucid Trading 25k Flex Eval parameters (confirmed by the user, see
odd/tasks/tradovate-execution-bot.md):
  - Profit target: $1250
  - Max loss limit: $1000, End-of-Day drawdown type: the floor is
    `eod_balance_last_close - 1000`, recalculated once per day at the last
    close, and is NEVER adjusted upward intraday even if equity rises.
  - Daily loss limit: $600 intraday, resets every day.
  - Consistency rule: 40% -- no single day's realized profit may exceed 40%
    of the $1250 PROFIT_TARGET. This is deliberately NOT 40% of profit
    realized so far: comparing against the cumulative total falsely rejects
    the very first profitable day of an evaluation, since any profit at all
    is then 100% of the (near-zero) running total (see
    odd/tasks/tradovate-execution-bot.md, 2026-11-10 overnight lockout).
  - Instrument: MNQ only, hard cap of 3 contracts per position.
  - Trading hours (confirmed by the user, 2026-11-10): closed daily
    16:00-18:00 ET Monday-Thursday, and closed from Friday 16:00 ET
    through Sunday 18:00 ET (weekend) -- all positions must be flat
    before the daily/weekend close, and no new entry may be placed while
    closed.

Fail-closed contract: `evaluate_order` returns `allow=False` for any
missing, malformed, or ambiguous input. There is no code path that defaults
to allowing a trade when the inputs can't be trusted.
"""
import math
from dataclasses import dataclass
from datetime import time

DAILY_LOSS_LIMIT = 600.0
MAX_LOSS_LIMIT = 1000.0
CONSISTENCY_PCT = 0.40
PROFIT_TARGET = 1250.0
ALLOWED_SYMBOL = "MNQ"
MAX_CONTRACTS = 3
MARKET_CLOSE_TIME = time(16, 0)
MARKET_REOPEN_TIME = time(18, 0)

REQUIRED_ACCOUNT_FIELDS = (
    "daily_realized_pnl",
    "eod_balance_last_close",
    "current_balance",
    "daily_profit_today",
    "total_realized_profit",
)
REQUIRED_ORDER_FIELDS = ("symbol", "side", "qty", "type")


@dataclass(frozen=True)
class Decision:
    allow: bool
    reason: str


def _is_finite_number(v):
    """True only for a real, finite int/float. Rejects bool, NaN, inf, str."""
    if isinstance(v, bool):
        return False
    if not isinstance(v, (int, float)):
        return False
    try:
        return math.isfinite(v)
    except (TypeError, ValueError):
        return False


# ---------------------------------------------------------------- individual checks

def check_daily_loss(daily_realized_pnl):
    """Intraday daily-loss kill switch: reject once today's realized loss
    reaches $600. Resets daily because the caller passes today's figure."""
    if not _is_finite_number(daily_realized_pnl):
        return False, "daily_realized_pnl is missing or not a finite number"
    if daily_realized_pnl <= -DAILY_LOSS_LIMIT:
        return False, "daily loss limit breached: %.2f <= -%.2f" % (daily_realized_pnl, DAILY_LOSS_LIMIT)
    return True, "daily loss within limit"


def check_max_loss(current_balance, eod_balance_last_close):
    """End-of-Day drawdown max-loss kill switch. The floor is fixed at the
    last close minus $1000 and is never recomputed intraday, so a higher
    intraday equity never raises (or lowers) the floor the caller supplies
    here -- that recalculation happens once per day, outside this function,
    by whoever determines eod_balance_last_close for today."""
    if not _is_finite_number(current_balance) or not _is_finite_number(eod_balance_last_close):
        return False, "current_balance or eod_balance_last_close is missing or not a finite number"
    floor = eod_balance_last_close - MAX_LOSS_LIMIT
    if current_balance < floor:
        return False, "EOD max-loss floor breached: balance %.2f < floor %.2f" % (current_balance, floor)
    return True, "max loss within EOD floor"


def check_consistency(daily_profit_today, total_realized_profit):
    """Consistency rule: no single day's realized profit may exceed 40% of
    the fixed PROFIT_TARGET ($1250) -- not 40% of total_realized_profit.
    total_realized_profit is still required and validated here (fail-closed)
    even though it no longer drives the comparison, since it remains a
    required account_state field other checks/consumers may depend on."""
    if not _is_finite_number(daily_profit_today) or not _is_finite_number(total_realized_profit):
        return False, "daily_profit_today or total_realized_profit is missing or not a finite number"
    if daily_profit_today > CONSISTENCY_PCT * PROFIT_TARGET:
        return False, "consistency rule breached: today's profit %.2f exceeds %.0f%% of profit target %.2f" % (
            daily_profit_today, CONSISTENCY_PCT * 100, PROFIT_TARGET)
    return True, "consistency rule satisfied"


def check_trading_hours(now):
    """Weekly trading schedule: closed daily 16:00-18:00 ET Monday-Thursday,
    and closed from Friday 16:00 ET through Sunday 18:00 ET (weekend).
    `now` must be a timezone-aware datetime (e.g. tradovate_bot.time_utils.
    now_et()) -- this module has no clock access of its own, per its module
    docstring, so the caller always supplies "now" explicitly."""
    if now is None or now.tzinfo is None:
        return False, "now is missing or not a timezone-aware datetime"
    weekday = now.weekday()  # Monday=0 ... Sunday=6
    t = now.time()
    if weekday in (0, 1, 2, 3):  # Monday-Thursday
        closed = MARKET_CLOSE_TIME <= t < MARKET_REOPEN_TIME
    elif weekday == 4:  # Friday
        closed = t >= MARKET_CLOSE_TIME
    elif weekday == 5:  # Saturday
        closed = True
    else:  # Sunday
        closed = t < MARKET_REOPEN_TIME
    if closed:
        return False, (
            "market closed: outside trading hours (closed daily 16:00-18:00 ET "
            "Mon-Thu, and Fri 16:00 ET - Sun 18:00 ET)"
        )
    return True, "within trading hours"


def check_position_size(symbol, qty, allowed_symbol=None, max_contracts=None):
    """MNQ only, hard cap of 3 contracts. Rejects anything else outright.

    allowed_symbol/max_contracts are optional overrides (see
    odd/tasks/tradovate-architecture-scaling.md task 3): when omitted or
    None, this falls back to the module-level ALLOWED_SYMBOL/MAX_CONTRACTS
    exactly as before -- purely additive, no change to the no-argument
    call shape every existing caller/test already uses."""
    if allowed_symbol is None:
        allowed_symbol = ALLOWED_SYMBOL
    if max_contracts is None:
        max_contracts = MAX_CONTRACTS
    if not isinstance(symbol, str) or symbol.strip().upper() != allowed_symbol:
        return False, "symbol must be %s (got %r)" % (allowed_symbol, symbol)
    if not _is_finite_number(qty) or isinstance(qty, float) and not qty.is_integer():
        return False, "qty is missing, not a finite number, or not a whole number"
    qty_int = int(qty)
    if qty_int <= 0:
        return False, "qty must be a positive number of contracts, got %r" % qty
    if qty_int > max_contracts:
        return False, "position size over cap: %d contracts exceeds the %d %s contract limit" % (
            qty_int, max_contracts, allowed_symbol)
    return True, "position size within MNQ contract cap"


# ---------------------------------------------------------------- top-level gate

def evaluate_order(order, account_state, now, config=None):
    """Run every risk check for a proposed order against current account
    state. Returns a Decision(allow, reason). Fails closed: any missing,
    malformed, or ambiguous order, account_state, or now field results in
    allow=False rather than a guess.

    `now` is required (not defaulted) so a caller can never silently skip
    the trading-hours check by forgetting to pass it -- see
    check_trading_hours and this module's "no clock access of its own"
    contract.

    `config` is an optional explicit identity/transport bundle (see
    tradovate_bot/config.py and odd/tasks/tradovate-architecture-scaling.md
    task 3): when given, its `.allowed_symbol`/`.max_contracts` (read via
    getattr, so this module stays duck-typed and never imports config.py --
    see check_position_size's call below) override the module-level
    ALLOWED_SYMBOL/MAX_CONTRACTS for check_position_size, for this call
    only. When `config` is None (the default), check_position_size is
    called exactly as before -- no behavior change for any existing
    caller."""
    if not isinstance(order, dict):
        return Decision(False, "order is missing or not a record")
    if not isinstance(account_state, dict):
        return Decision(False, "account_state is missing or not a record")

    for field in REQUIRED_ORDER_FIELDS:
        if field not in order or order[field] is None:
            return Decision(False, "order is missing required field: %s" % field)
    for field in REQUIRED_ACCOUNT_FIELDS:
        if field not in account_state:
            return Decision(False, "account_state is missing required field: %s" % field)

    ok, reason = check_trading_hours(now)
    if not ok:
        return Decision(False, reason)

    if config is None:
        ok, reason = check_position_size(order.get("symbol"), order.get("qty"))
    else:
        ok, reason = check_position_size(
            order.get("symbol"), order.get("qty"),
            allowed_symbol=getattr(config, "allowed_symbol", None),
            max_contracts=getattr(config, "max_contracts", None),
        )
    if not ok:
        return Decision(False, reason)

    ok, reason = check_daily_loss(account_state.get("daily_realized_pnl"))
    if not ok:
        return Decision(False, reason)

    ok, reason = check_max_loss(
        account_state.get("current_balance"), account_state.get("eod_balance_last_close")
    )
    if not ok:
        return Decision(False, reason)

    ok, reason = check_consistency(
        account_state.get("daily_profit_today"), account_state.get("total_realized_profit")
    )
    if not ok:
        return Decision(False, reason)

    return Decision(True, "all risk checks passed")
