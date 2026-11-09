"""Test-first unit tests for tradovate_bot.risk_guard.

Lucid Trading 25k Flex Eval parameters under test:
  - Daily loss limit: $600 intraday, resets daily.
  - Max loss limit: $1000, End-of-Day drawdown type (floor = EOD balance from
    last close - 1000, recalculated once per day, never adjusted intraday).
  - Consistency rule: 40% (no single day's realized profit may exceed 40% of
    total realized profit counted toward the $1250 profit target).
  - Position sizing: MNQ only, hard cap 3 contracts.
  - Fail-closed on any missing/ambiguous/malformed account-state input.

These tests are written before/alongside the implementation and drive its
design, per the ODD test-first policy for this module.
"""
import pytest

from tradovate_bot import risk_guard as rg


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
    decision = rg.evaluate_order(base_order(), base_account_state())
    assert decision.allow is True
    assert decision.reason == "all risk checks passed"


# ---------------------------------------------------------------- daily loss

def test_daily_loss_limit_breached_rejects():
    state = base_account_state(daily_realized_pnl=-600.0)
    decision = rg.evaluate_order(base_order(), state)
    assert decision.allow is False
    assert "daily loss" in decision.reason.lower()


def test_daily_loss_limit_just_under_passes():
    state = base_account_state(daily_realized_pnl=-599.99)
    decision = rg.evaluate_order(base_order(), state)
    assert decision.allow is True


def test_daily_loss_limit_exceeded_further_rejects():
    state = base_account_state(daily_realized_pnl=-750.0)
    decision = rg.evaluate_order(base_order(), state)
    assert decision.allow is False
    assert "daily loss" in decision.reason.lower()


# ---------------------------------------------------------------- EOD max loss

def test_eod_max_loss_breached_rejects():
    # floor = 25000 - 1000 = 24000; current balance below floor
    state = base_account_state(eod_balance_last_close=25000.0, current_balance=23999.0)
    decision = rg.evaluate_order(base_order(), state)
    assert decision.allow is False
    assert "max-loss" in decision.reason.lower() or "max loss" in decision.reason.lower()


def test_eod_max_loss_floor_is_not_adjusted_intraday():
    # Equity rose intraday to 26000 then the EOD close used for the floor
    # must still be the LAST close (25000), never today's higher equity.
    state = base_account_state(eod_balance_last_close=25000.0, current_balance=24500.0)
    decision = rg.evaluate_order(base_order(), state)
    # 24500 >= 25000 - 1000 = 24000 -> still passes
    assert decision.allow is True


def test_eod_max_loss_exactly_at_floor_passes():
    state = base_account_state(eod_balance_last_close=25000.0, current_balance=24000.0)
    decision = rg.evaluate_order(base_order(), state)
    assert decision.allow is True


# ---------------------------------------------------------------- consistency rule

def test_consistency_rule_breached_rejects():
    # today's profit is more than 40% of total realized profit
    state = base_account_state(daily_profit_today=600.0, total_realized_profit=1000.0)
    decision = rg.evaluate_order(base_order(), state)
    assert decision.allow is False
    assert "consistency" in decision.reason.lower()


def test_consistency_rule_at_exactly_40_percent_passes():
    state = base_account_state(daily_profit_today=400.0, total_realized_profit=1000.0)
    decision = rg.evaluate_order(base_order(), state)
    assert decision.allow is True


def test_consistency_rule_no_profit_yet_passes():
    state = base_account_state(daily_profit_today=0.0, total_realized_profit=0.0)
    decision = rg.evaluate_order(base_order(), state)
    assert decision.allow is True


# ---------------------------------------------------------------- position sizing

def test_position_size_over_cap_rejects():
    decision = rg.evaluate_order(base_order(qty=4), base_account_state())
    assert decision.allow is False
    assert "position size" in decision.reason.lower() or "contract" in decision.reason.lower()


def test_position_size_at_cap_passes():
    decision = rg.evaluate_order(base_order(qty=3), base_account_state())
    assert decision.allow is True


def test_non_mnq_symbol_rejects():
    decision = rg.evaluate_order(base_order(symbol="ES"), base_account_state())
    assert decision.allow is False
    assert "mnq" in decision.reason.lower()


# ---------------------------------------------------------------- fail closed

def test_missing_account_state_field_fails_closed():
    state = base_account_state()
    del state["daily_realized_pnl"]
    decision = rg.evaluate_order(base_order(), state)
    assert decision.allow is False


def test_none_account_state_fails_closed():
    decision = rg.evaluate_order(base_order(), None)
    assert decision.allow is False


def test_malformed_account_state_value_fails_closed():
    state = base_account_state(current_balance="not-a-number")
    decision = rg.evaluate_order(base_order(), state)
    assert decision.allow is False


def test_missing_order_field_fails_closed():
    order = base_order()
    del order["qty"]
    decision = rg.evaluate_order(order, base_account_state())
    assert decision.allow is False


def test_none_order_fails_closed():
    decision = rg.evaluate_order(None, base_account_state())
    assert decision.allow is False


def test_zero_or_negative_qty_fails_closed():
    decision = rg.evaluate_order(base_order(qty=0), base_account_state())
    assert decision.allow is False
    decision2 = rg.evaluate_order(base_order(qty=-1), base_account_state())
    assert decision2.allow is False


def test_nan_in_account_state_fails_closed():
    decision = rg.evaluate_order(base_order(), base_account_state(current_balance=float("nan")))
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
    ok, reason = rg.check_consistency(daily_profit_today=300.0, total_realized_profit=1000.0)
    assert ok is True
    ok, reason = rg.check_consistency(daily_profit_today=500.0, total_realized_profit=1000.0)
    assert ok is False


def test_check_position_size_function_directly():
    ok, reason = rg.check_position_size("MNQ", 3)
    assert ok is True
    ok, reason = rg.check_position_size("MNQ", 4)
    assert ok is False
    ok, reason = rg.check_position_size("ES", 1)
    assert ok is False
