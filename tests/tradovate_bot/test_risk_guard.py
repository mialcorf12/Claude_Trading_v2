"""Test-first unit tests for tradovate_bot.risk_guard.

Lucid Trading 25k Flex Eval parameters under test:
  - Daily loss limit: $600 intraday, resets daily.
  - Max loss limit: $1000, End-of-Day drawdown type (floor = EOD balance from
    last close - 1000, recalculated once per day, never adjusted intraday).
  - Consistency rule: 40% (no single day's realized profit may exceed 40% of
    the $1250 PROFIT_TARGET -- not 40% of profit realized so far, which would
    falsely reject the very first profitable day of an evaluation).
  - Trading hours: closed daily 16:00-18:00 ET Monday-Thursday, and closed
    from Friday 16:00 ET through Sunday 18:00 ET (weekend). All positions
    must be flat before the daily/weekend close.
  - Position sizing: MNQ only, hard cap 3 contracts.
  - Fail-closed on any missing/ambiguous/malformed account-state input.

These tests are written before/alongside the implementation and drive its
design, per the ODD test-first policy for this module.
"""
from datetime import datetime

import pytest

from tradovate_bot import risk_guard as rg
from tradovate_bot.time_utils import EASTERN

# Wednesday 2026-11-11, 10:00 ET -- unambiguously within trading hours, used
# as the default `now` for every test that isn't specifically about hours.
WITHIN_HOURS = datetime(2026, 11, 11, 10, 0, tzinfo=EASTERN)


def _evaluate(order, account_state, now=WITHIN_HOURS):
    return rg.evaluate_order(order, account_state, now)


def base_order(**overrides):
    order = {"symbol": "MNQ", "side": "buy", "qty": 1, "type": "market",
             "stop_price": 20000.0, "target_price": 20100.0}
    order.update(overrides)
    return order


def base_account_state(**overrides):
    state = {
        "daily_realized_pnl": 0.0,
        "eod_balance_last_close": 25000.0,
        "current_balance": 25000.0,
        "daily_profit_today": 0.0,
        "total_realized_profit": 0.0,
    }
    state.update(overrides)
    return state


# ---------------------------------------------------------------- pass case

def test_all_limits_satisfied_passes():
    decision = _evaluate(base_order(), base_account_state())
    assert decision.allow is True
    assert decision.reason == "all risk checks passed"


# ---------------------------------------------------------------- daily loss

def test_daily_loss_limit_breached_rejects():
    state = base_account_state(daily_realized_pnl=-600.0)
    decision = _evaluate(base_order(), state)
    assert decision.allow is False
    assert "daily loss" in decision.reason.lower()


def test_daily_loss_limit_just_under_passes():
    state = base_account_state(daily_realized_pnl=-599.99)
    decision = _evaluate(base_order(), state)
    assert decision.allow is True


def test_daily_loss_limit_exceeded_further_rejects():
    state = base_account_state(daily_realized_pnl=-750.0)
    decision = _evaluate(base_order(), state)
    assert decision.allow is False
    assert "daily loss" in decision.reason.lower()


# ---------------------------------------------------------------- EOD max loss

def test_eod_max_loss_breached_rejects():
    # floor = 25000 - 1000 = 24000; current balance below floor
    state = base_account_state(eod_balance_last_close=25000.0, current_balance=23999.0)
    decision = _evaluate(base_order(), state)
    assert decision.allow is False
    assert "max-loss" in decision.reason.lower() or "max loss" in decision.reason.lower()


def test_eod_max_loss_floor_is_not_adjusted_intraday():
    # Equity rose intraday to 26000 then the EOD close used for the floor
    # must still be the LAST close (25000), never today's higher equity.
    state = base_account_state(eod_balance_last_close=25000.0, current_balance=24500.0)
    decision = _evaluate(base_order(), state)
    # 24500 >= 25000 - 1000 = 24000 -> still passes
    assert decision.allow is True


def test_eod_max_loss_exactly_at_floor_passes():
    state = base_account_state(eod_balance_last_close=25000.0, current_balance=24000.0)
    decision = _evaluate(base_order(), state)
    assert decision.allow is True


# ---------------------------------------------------------------- consistency rule

def test_consistency_rule_breached_rejects():
    # today's profit (600) is more than 40% of the $1250 PROFIT_TARGET (500)
    state = base_account_state(daily_profit_today=600.0, total_realized_profit=1000.0)
    decision = _evaluate(base_order(), state)
    assert decision.allow is False
    assert "consistency" in decision.reason.lower()


def test_consistency_rule_at_exactly_40_percent_of_target_passes():
    # 0.40 * PROFIT_TARGET (1250) == 500.0, exactly at the boundary
    state = base_account_state(daily_profit_today=500.0, total_realized_profit=1000.0)
    decision = _evaluate(base_order(), state)
    assert decision.allow is True


def test_consistency_rule_no_profit_yet_passes():
    state = base_account_state(daily_profit_today=0.0, total_realized_profit=0.0)
    decision = _evaluate(base_order(), state)
    assert decision.allow is True


def test_consistency_rule_ignores_cumulative_total_not_the_fixed_target():
    # Regression for the overnight lockout (2026-11-10): a first-ever
    # profitable day made total_realized_profit == daily_profit_today, so
    # comparing against total_realized_profit always breached (100% > 40%).
    # The rule must compare against the fixed PROFIT_TARGET instead, so a
    # modest first-day profit should pass regardless of the cumulative total.
    state = base_account_state(daily_profit_today=235.5, total_realized_profit=235.5)
    decision = _evaluate(base_order(), state)
    assert decision.allow is True


# ---------------------------------------------------------------- trading hours

def test_trading_hours_closed_during_daily_gap_mon_thu():
    # Monday 2026-11-09, 16:30 ET -- inside the daily 16:00-18:00 gap
    now = datetime(2026, 11, 9, 16, 30, tzinfo=EASTERN)
    decision = _evaluate(base_order(), base_account_state(), now=now)
    assert decision.allow is False
    assert "trading hours" in decision.reason.lower() or "closed" in decision.reason.lower()


def test_trading_hours_closed_exactly_at_1600():
    # closing time itself is already closed, not "closed starting the next tick"
    now = datetime(2026, 11, 9, 16, 0, tzinfo=EASTERN)
    decision = _evaluate(base_order(), base_account_state(), now=now)
    assert decision.allow is False


def test_trading_hours_open_immediately_after_reopen_mon_thu():
    now = datetime(2026, 11, 9, 18, 0, tzinfo=EASTERN)
    decision = _evaluate(base_order(), base_account_state(), now=now)
    assert decision.allow is True


def test_trading_hours_closed_on_friday_evening():
    # Friday 2026-11-13, 20:00 ET -- weekend close already in effect
    now = datetime(2026, 11, 13, 20, 0, tzinfo=EASTERN)
    decision = _evaluate(base_order(), base_account_state(), now=now)
    assert decision.allow is False


def test_trading_hours_open_friday_before_1600():
    now = datetime(2026, 11, 13, 15, 59, tzinfo=EASTERN)
    decision = _evaluate(base_order(), base_account_state(), now=now)
    assert decision.allow is True


def test_trading_hours_closed_all_day_saturday():
    now = datetime(2026, 11, 14, 12, 0, tzinfo=EASTERN)
    decision = _evaluate(base_order(), base_account_state(), now=now)
    assert decision.allow is False


def test_trading_hours_closed_sunday_before_reopen():
    now = datetime(2026, 11, 15, 10, 0, tzinfo=EASTERN)
    decision = _evaluate(base_order(), base_account_state(), now=now)
    assert decision.allow is False


def test_trading_hours_open_sunday_after_reopen():
    now = datetime(2026, 11, 15, 18, 0, tzinfo=EASTERN)
    decision = _evaluate(base_order(), base_account_state(), now=now)
    assert decision.allow is True


def test_trading_hours_rejects_naive_datetime():
    now = datetime(2026, 11, 11, 10, 0)  # no tzinfo
    decision = _evaluate(base_order(), base_account_state(), now=now)
    assert decision.allow is False


def test_trading_hours_rejects_none():
    decision = _evaluate(base_order(), base_account_state(), now=None)
    assert decision.allow is False


# ---------------------------------------------------------------- position sizing

def test_position_size_over_cap_rejects():
    decision = _evaluate(base_order(qty=4), base_account_state())
    assert decision.allow is False
    assert "position size" in decision.reason.lower() or "contract" in decision.reason.lower()


def test_position_size_at_cap_passes():
    decision = _evaluate(base_order(qty=3), base_account_state())
    assert decision.allow is True


def test_non_mnq_symbol_rejects():
    decision = _evaluate(base_order(symbol="ES"), base_account_state())
    assert decision.allow is False
    assert "mnq" in decision.reason.lower()


# ---------------------------------------------------------------- fail closed

def test_missing_account_state_field_fails_closed():
    state = base_account_state()
    del state["daily_realized_pnl"]
    decision = _evaluate(base_order(), state)
    assert decision.allow is False


def test_none_account_state_fails_closed():
    decision = _evaluate(base_order(), None)
    assert decision.allow is False


def test_malformed_account_state_value_fails_closed():
    state = base_account_state(current_balance="not-a-number")
    decision = _evaluate(base_order(), state)
    assert decision.allow is False


def test_missing_order_field_fails_closed():
    order = base_order()
    del order["qty"]
    decision = _evaluate(order, base_account_state())
    assert decision.allow is False


def test_none_order_fails_closed():
    decision = _evaluate(None, base_account_state())
    assert decision.allow is False


def test_zero_or_negative_qty_fails_closed():
    decision = _evaluate(base_order(qty=0), base_account_state())
    assert decision.allow is False
    decision2 = _evaluate(base_order(qty=-1), base_account_state())
    assert decision2.allow is False


def test_nan_in_account_state_fails_closed():
    decision = _evaluate(base_order(), base_account_state(current_balance=float("nan")))
    assert decision.allow is False


# ---------------------------------------------------------------- individual check functions

def test_check_daily_loss_function_directly():
    ok, reason = rg.check_daily_loss(-100.0)
    assert ok is True
    ok, reason = rg.check_daily_loss(-600.0)
    assert ok is False


def test_check_max_loss_function_directly():
    ok, reason = rg.check_max_loss(current_balance=24500.0, eod_balance_last_close=25000.0)
    assert ok is True
    ok, reason = rg.check_max_loss(current_balance=23000.0, eod_balance_last_close=25000.0)
    assert ok is False


def test_check_consistency_function_directly():
    # threshold is 0.40 * PROFIT_TARGET (1250) = 500.0, regardless of
    # total_realized_profit
    ok, reason = rg.check_consistency(daily_profit_today=300.0, total_realized_profit=1000.0)
    assert ok is True
    ok, reason = rg.check_consistency(daily_profit_today=600.0, total_realized_profit=1000.0)
    assert ok is False
    # same daily_profit_today, wildly different total_realized_profit ->
    # same outcome, since the cumulative total no longer drives the decision
    ok, reason = rg.check_consistency(daily_profit_today=300.0, total_realized_profit=1.0)
    assert ok is True


def test_check_consistency_rejects_non_finite_total_realized_profit():
    # total_realized_profit no longer drives the comparison, but it's still
    # a required account_state field, so a malformed value must still fail
    # closed rather than be silently ignored.
    ok, reason = rg.check_consistency(daily_profit_today=100.0, total_realized_profit=float("nan"))
    assert ok is False


def test_check_position_size_function_directly():
    ok, reason = rg.check_position_size("MNQ", 3)
    assert ok is True
    ok, reason = rg.check_position_size("MNQ", 4)
    assert ok is False
    ok, reason = rg.check_position_size("ES", 1)
    assert ok is False


def test_check_trading_hours_function_directly():
    ok, reason = rg.check_trading_hours(WITHIN_HOURS)
    assert ok is True
    ok, reason = rg.check_trading_hours(datetime(2026, 11, 9, 17, 0, tzinfo=EASTERN))
    assert ok is False


# ---------------------------------------------------------------- optional explicit config
# (odd/tasks/tradovate-architecture-scaling.md task 3: check_position_size's
# allowed_symbol/max_contracts and evaluate_order's config are purely
# additive -- every test above calls with no config/overrides at all and
# must keep behaving exactly as it always has.)

class _FakeConfig:
    """Duck-typed stand-in for tradovate_bot.config.BotConfig -- evaluate_order
    only reads .allowed_symbol/.max_contracts via getattr, so any object
    with those attributes works, same as the real BotConfig."""

    def __init__(self, allowed_symbol, max_contracts):
        self.allowed_symbol = allowed_symbol
        self.max_contracts = max_contracts


def test_evaluate_order_with_config_allows_a_symbol_the_default_rejects():
    # Sanity: without a config, ES is rejected by the default ALLOWED_SYMBOL.
    decision_default = _evaluate(base_order(symbol="ES"), base_account_state())
    assert decision_default.allow is False

    config = _FakeConfig(allowed_symbol="ES", max_contracts=3)
    decision = rg.evaluate_order(base_order(symbol="ES"), base_account_state(), WITHIN_HOURS, config=config)
    assert decision.allow is True


def test_evaluate_order_with_config_rejects_a_symbol_the_default_allows():
    # Sanity: without a config, MNQ passes.
    decision_default = _evaluate(base_order(symbol="MNQ"), base_account_state())
    assert decision_default.allow is True

    config = _FakeConfig(allowed_symbol="ES", max_contracts=3)
    decision = rg.evaluate_order(base_order(symbol="MNQ"), base_account_state(), WITHIN_HOURS, config=config)
    assert decision.allow is False
    assert "es" in decision.reason.lower()


def test_evaluate_order_with_config_enforces_a_different_max_contracts():
    config = _FakeConfig(allowed_symbol="MNQ", max_contracts=1)
    decision_over = rg.evaluate_order(base_order(qty=2), base_account_state(), WITHIN_HOURS, config=config)
    assert decision_over.allow is False
    decision_at_cap = rg.evaluate_order(base_order(qty=1), base_account_state(), WITHIN_HOURS, config=config)
    assert decision_at_cap.allow is True


def test_evaluate_order_with_config_none_behaves_exactly_like_omitting_it():
    decision_omitted = rg.evaluate_order(base_order(), base_account_state(), WITHIN_HOURS)
    decision_explicit_none = rg.evaluate_order(base_order(), base_account_state(), WITHIN_HOURS, config=None)
    assert decision_omitted.allow is True
    assert decision_explicit_none.allow is True
    assert decision_omitted.reason == decision_explicit_none.reason == "all risk checks passed"


def test_check_position_size_with_explicit_overrides():
    ok, reason = rg.check_position_size("ES", 2, allowed_symbol="ES", max_contracts=5)
    assert ok is True
    ok, reason = rg.check_position_size("MNQ", 2, allowed_symbol="ES", max_contracts=5)
    assert ok is False


def test_check_position_size_defaults_when_overrides_are_none():
    ok_default, reason_default = rg.check_position_size("MNQ", 3)
    ok_explicit_none, reason_explicit_none = rg.check_position_size(
        "MNQ", 3, allowed_symbol=None, max_contracts=None
    )
    assert ok_default is True
    assert ok_explicit_none is True
    assert reason_default == reason_explicit_none
