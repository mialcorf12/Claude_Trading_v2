"""Test-first unit tests for tradovate_bot.signals.

Codifies two decision-quality rules established LIVE this session, after
root-causing two real losing trades (see
odd/tasks/tradovate-execution-bot.md, cycles 18-23, and
odd/tasks/tradovate-architecture-scaling.md task 2):

1. pullback-over-chase: prefer an entry on a confirmed pullback over
   chasing an already-extended move with zero retracement. Cycle 18's BUY
   (four consecutive higher closes off the 11:55 low, no pullback) is the
   real scenario that caused a stop-out loss -- used here as
   CYCLE_18_CHASE_BARS.
2. repeated-reversal-is-chop: a zone that has already reversed direction
   multiple times in the recent window reads as weaker/choppier, even if
   the last few bars look directional (cycle 20's 27540-27690 range).

These tests are written before the implementation and drive its design,
per this module's test-first ODD policy. Confirm RED (signals doesn't
exist yet) before writing tradovate_bot/signals.py.
"""
import pytest

from tradovate_bot import signals


# ---------------------------------------------------------------- fixtures

# Cycle 18 (odd/tasks/tradovate-execution-bot.md lines 604-619): a clear
# reversal off the 11:55 low (27631.75), then four consecutive higher
# closes (27657.25, 27658.75, 27678.5, 27692.0), +60 pts with NO pullback
# yet. This chase was read as BUY and stopped out -- the real-world cause
# that motivated the chase guard.
CYCLE_18_CHASE_BARS = [
    {"open": 27625.00, "high": 27635.00, "low": 27620.00, "close": 27631.75},
    {"open": 27632.00, "high": 27660.00, "low": 27630.00, "close": 27657.25},
    {"open": 27657.00, "high": 27662.00, "low": 27655.00, "close": 27658.75},
    {"open": 27659.00, "high": 27682.00, "low": 27658.00, "close": 27678.50},
    {"open": 27679.00, "high": 27695.00, "low": 27677.00, "close": 27692.00},
]

# Mirror of the cycle-18 shape for the "sell" side: four consecutive LOWER
# closes, zero retracement -- should chase-flag symmetrically.
CHASE_SELL_BARS = [
    {"open": 100.0, "high": 101.0, "low": 95.0, "close": 97.0},
    {"open": 97.0, "high": 98.0, "low": 90.0, "close": 92.0},
    {"open": 92.0, "high": 93.0, "low": 85.0, "close": 87.0},
    {"open": 87.0, "high": 88.0, "low": 80.0, "close": 82.0},
    {"open": 82.0, "high": 83.0, "low": 75.0, "close": 77.0},
]

# Cycle 23 (odd/tasks/tradovate-execution-bot.md lines 702-717): a rally,
# a real pullback (27656 -> 27643.25), then resumption to a fresh high
# (27678.25) and a shallow current dip (27669) -- bought on the confirmed
# pullback, not chased. The chase guard must NOT flag this.
PULLBACK_CONTINUATION_BARS = [
    {"open": 27600.00, "high": 27610.00, "low": 27594.50, "close": 27605.00},
    {"open": 27605.00, "high": 27640.00, "low": 27603.00, "close": 27630.00},
    {"open": 27630.00, "high": 27660.00, "low": 27628.00, "close": 27656.00},
    {"open": 27656.00, "high": 27658.00, "low": 27640.00, "close": 27643.25},
    {"open": 27643.00, "high": 27665.00, "low": 27642.00, "close": 27660.00},
    {"open": 27660.00, "high": 27680.00, "low": 27659.00, "close": 27678.25},
    {"open": 27678.00, "high": 27679.00, "low": 27665.00, "close": 27669.00},
]

# Cycle 20-style zigzag (odd/tasks/tradovate-execution-bot.md lines
# 677-685): a 27540-27690 zone that has reversed repeatedly. Highs/lows
# zigzag so the trailing window carries several swing highs/lows. Closes
# also zigzag so this fixture isolates the chop guard (the chase guard
# finds its own retracement and stays clear on these same bars).
CHOP_BARS = [
    {"open": 99.0, "high": 100.0, "low": 95.0, "close": 100.0},
    {"open": 100.0, "high": 110.0, "low": 105.0, "close": 108.0},
    {"open": 108.0, "high": 95.0, "low": 90.0, "close": 92.0},
    {"open": 92.0, "high": 105.0, "low": 100.0, "close": 101.0},
    {"open": 101.0, "high": 92.0, "low": 88.0, "close": 89.0},
    {"open": 89.0, "high": 108.0, "low": 103.0, "close": 105.0},
    {"open": 105.0, "high": 90.0, "low": 85.0, "close": 93.0},
]

# A single clean directional move: highs and lows each strictly higher
# than the previous bar's, so no interior bar is a local high or low.
# Reused for the "no reversals" chop case. Also naturally a chase (6
# strictly higher closes), used by chase tests that want a long clean run.
CLEAN_TREND_BARS = [
    {"open": 100.0, "high": 102.0, "low": 98.0, "close": 101.0},
    {"open": 101.0, "high": 106.0, "low": 100.0, "close": 105.0},
    {"open": 105.0, "high": 110.0, "low": 104.0, "close": 109.0},
    {"open": 109.0, "high": 114.0, "low": 108.0, "close": 113.0},
    {"open": 113.0, "high": 118.0, "low": 112.0, "close": 117.0},
    {"open": 117.0, "high": 122.0, "low": 116.0, "close": 121.0},
    {"open": 121.0, "high": 126.0, "low": 120.0, "close": 125.0},
]

# Neither guard should fire: one down-close pullback (clears the chase
# guard) while highs/lows still climb monotonically with no interior peak
# or trough (clears the chop guard).
NEITHER_FIRES_BARS = [
    {"open": 99.0, "high": 101.0, "low": 95.0, "close": 100.0},
    {"open": 100.0, "high": 110.0, "low": 100.0, "close": 105.0},
    {"open": 105.0, "high": 115.0, "low": 105.0, "close": 103.0},
    {"open": 103.0, "high": 120.0, "low": 108.0, "close": 108.0},
    {"open": 108.0, "high": 125.0, "low": 111.0, "close": 112.0},
    {"open": 112.0, "high": 130.0, "low": 114.0, "close": 115.0},
]

TOO_FEW_BARS = [
    {"open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5},
    {"open": 1.5, "high": 2.5, "low": 1.0, "close": 2.0},
    {"open": 2.0, "high": 3.0, "low": 1.5, "close": 2.5},
]


# ---------------------------------------------------------------- check_chase_guard

def test_chase_guard_flags_pure_chase_with_no_pullback():
    result = signals.check_chase_guard(CYCLE_18_CHASE_BARS, "buy")
    assert result.flagged is True
    assert "chasing" in result.reason.lower()


def test_chase_guard_flags_pure_chase_sell_side_symmetrically():
    result = signals.check_chase_guard(CHASE_SELL_BARS, "sell")
    assert result.flagged is True
    assert "chasing" in result.reason.lower()


def test_chase_guard_allows_confirmed_pullback_then_continuation():
    result = signals.check_chase_guard(PULLBACK_CONTINUATION_BARS, "buy")
    assert result.flagged is False


def test_chase_guard_rejects_invalid_direction_fail_closed():
    result = signals.check_chase_guard(CYCLE_18_CHASE_BARS, "long")
    assert result.flagged is True
    assert "direction" in result.reason.lower()


def test_chase_guard_too_few_bars_fails_closed():
    result = signals.check_chase_guard(TOO_FEW_BARS, "buy")
    assert result.flagged is True
    assert "insufficient" in result.reason.lower()


def test_chase_guard_malformed_bars_fail_closed():
    assert signals.check_chase_guard(None, "buy").flagged is True
    assert signals.check_chase_guard([], "buy").flagged is True
    assert signals.check_chase_guard([{"high": 1.0}] * 6, "buy").flagged is True


# ---------------------------------------------------------------- check_chop_guard

def test_chop_guard_flags_repeatedly_reversing_range():
    result = signals.check_chop_guard(CHOP_BARS)
    assert result.flagged is True
    assert "chop" in result.reason.lower()


def test_chop_guard_allows_single_clean_directional_move():
    result = signals.check_chop_guard(CLEAN_TREND_BARS)
    assert result.flagged is False


def test_chop_guard_too_few_bars_fails_closed():
    result = signals.check_chop_guard(TOO_FEW_BARS)
    assert result.flagged is True
    assert "insufficient" in result.reason.lower()


def test_chop_guard_malformed_bars_fail_closed():
    assert signals.check_chop_guard(None).flagged is True
    assert signals.check_chop_guard([]).flagged is True
    assert signals.check_chop_guard([{"low": 1.0}] * 6).flagged is True


# ---------------------------------------------------------------- evaluate_signal

def test_evaluate_signal_blocks_on_chase_guard_only():
    verdict = signals.evaluate_signal(CYCLE_18_CHASE_BARS, "buy")
    assert verdict.clear is False
    assert verdict.direction == "buy"
    assert len(verdict.reasons) == 1
    assert "chasing" in verdict.reasons[0].lower()


def test_evaluate_signal_blocks_on_chop_guard_only():
    verdict = signals.evaluate_signal(CHOP_BARS, "buy")
    assert verdict.clear is False
    assert len(verdict.reasons) == 1
    assert "chop" in verdict.reasons[0].lower()


def test_evaluate_signal_blocks_on_both_guards_when_bars_insufficient():
    verdict = signals.evaluate_signal(TOO_FEW_BARS, "buy")
    assert verdict.clear is False
    assert len(verdict.reasons) == 2


def test_evaluate_signal_allows_when_neither_guard_fires():
    verdict = signals.evaluate_signal(NEITHER_FIRES_BARS, "buy")
    assert verdict.clear is True
    assert verdict.reasons == ()


def test_evaluate_signal_rejects_invalid_direction_fail_closed():
    verdict = signals.evaluate_signal(CYCLE_18_CHASE_BARS, "up")
    assert verdict.clear is False
    assert verdict.reasons
