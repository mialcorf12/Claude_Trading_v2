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
    of the total realized profit counted toward the profit target.
  - Instrument: MNQ only, hard cap of 3 contracts per position.

Fail-closed contract: `evaluate_order` returns `allow=False` for any
missing, malformed, or ambiguous input. There is no code path that defaults
to allowing a trade when the inputs can't be trusted.
"""
import math
from dataclasses import dataclass

DAILY_LOSS_LIMIT = 600.0
MAX_LOSS_LIMIT = 1000.0
CONSISTENCY_PCT = 0.40
PROFIT_TARGET = 1250.0
ALLOWED_SYMBOL = "MNQ"
MAX_CONTRACTS = 3

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
    total realized profit counted toward the $1250 target. With zero or
    negative total profit there is nothing to be inconsistent with, so the
    check passes (there is no profit to over-concentrate)."""
    if not _is_finite_number(daily_profit_today) or not _is_finite_number(total_realized_profit):
        return False, "daily_profit_today or total_realized_profit is missing or not a finite number"
    if total_realized_profit <= 0:
        return True, "no realized profit yet, consistency rule not applicable"
    if daily_profit_today > CONSISTENCY_PCT * total_realized_profit:
        return False, "consistency rule breached: today's profit %.2f exceeds %.0f%% of total %.2f" % (
            daily_profit_today, CONSISTENCY_PCT * 100, total_realized_profit)
    return True, "consistency rule satisfied"


def check_position_size(symbol, qty):
    """MNQ only, hard cap of 3 contracts. Rejects anything else outright."""
    if not isinstance(symbol, str) or symbol.strip().upper() != ALLOWED_SYMBOL:
        return False, "symbol must be %s (got %r)" % (ALLOWED_SYMBOL, symbol)
    if not _is_finite_number(qty) or isinstance(qty, float) and not qty.is_integer():
        return False, "qty is missing, not a finite number, or not a whole number"
    qty_int = int(qty)
    if qty_int <= 0:
        return False, "qty must be a positive number of contracts, got %r" % qty
    if qty_int > MAX_CONTRACTS:
        return False, "position size over cap: %d contracts exceeds the %d MNQ contract limit" % (
            qty_int, MAX_CONTRACTS)
    return True, "position size within MNQ contract cap"


# ---------------------------------------------------------------- top-level gate

def evaluate_order(order, account_state):
    """Run every risk check for a proposed order against current account
    state. Returns a Decision(allow, reason). Fails closed: any missing,
    malformed, or ambiguous order or account_state field results in
    allow=False rather than a guess."""
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

    ok, reason = check_position_size(order.get("symbol"), order.get("qty"))
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
