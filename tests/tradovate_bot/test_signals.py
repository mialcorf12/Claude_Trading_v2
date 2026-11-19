"""Test-first unit tests for tradovate_bot.signals.

Codifies two decision-quality rules established LIVE this session, after
root-causing two real losing trades (see
odd/tasks/tradovate-execution-bot.md, cycles 18-23, and
odd/tasks/archive/tradovate-architecture-scaling.md task 2):

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

# A stale, early retracement (94 -> 97) followed by 4 consecutive LOWER
# closes with zero retracement SINCE the run started (97 -> 93 -> 89 -> 85
# -> 81). Modeled on the real 2026-11-16 ~14:37 ET cycle: the chase guard's
# old "any retracement anywhere in the lookback" check let this through as
# clear, even though the live run at the end is exactly cycle 18's
# zero-pullback chase -- the retracement just happened to predate it. The
# chase guard must flag this on the SELL side.
STALE_PULLBACK_THEN_CHASE_BARS = [
    {"open": 103.0, "high": 104.0, "low": 99.0, "close": 100.0},
    {"open": 100.0, "high": 101.0, "low": 93.0, "close": 94.0},
    {"open": 94.0, "high": 98.0, "low": 93.0, "close": 97.0},
    {"open": 97.0, "high": 98.0, "low": 92.0, "close": 93.0},
    {"open": 93.0, "high": 94.0, "low": 88.0, "close": 89.0},
    {"open": 89.0, "high": 90.0, "low": 84.0, "close": 85.0},
    {"open": 85.0, "high": 86.0, "low": 80.0, "close": 81.0},
]

TOO_FEW_BARS = [
    {"open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5},
    {"open": 1.5, "high": 2.5, "low": 1.0, "close": 2.0},
    {"open": 2.0, "high": 3.0, "low": 1.5, "close": 2.5},
]

# Known, exact ranges 10..90 in steps of 10 across 9 bars -- mean is
# trivially 50.0, used only to pin down average_bar_range's arithmetic.
KNOWN_RANGE_BARS = [
    {"open": 100.0, "high": 110.0, "low": 100.0, "close": 105.0},
    {"open": 100.0, "high": 120.0, "low": 100.0, "close": 110.0},
    {"open": 100.0, "high": 130.0, "low": 100.0, "close": 115.0},
    {"open": 100.0, "high": 140.0, "low": 100.0, "close": 120.0},
    {"open": 100.0, "high": 150.0, "low": 100.0, "close": 125.0},
    {"open": 100.0, "high": 160.0, "low": 100.0, "close": 130.0},
    {"open": 100.0, "high": 170.0, "low": 100.0, "close": 135.0},
    {"open": 100.0, "high": 180.0, "low": 100.0, "close": 140.0},
    {"open": 100.0, "high": 190.0, "low": 100.0, "close": 145.0},
]
KNOWN_RANGE_MEAN = 50.0

# Modeled on the real 00:00-00:16 ET 11/13 quiet overnight chop (roughly
# 20-30pt high-low range per trailing 5-minute bar) -- see
# odd/tasks/tradovate-execution-bot.md Progress log.
QUIET_VOLATILITY_BARS = [
    {"open": 28000.0, "high": 28020.0, "low": 28000.0, "close": 28015.0},
    {"open": 28015.0, "high": 28039.0, "low": 28015.0, "close": 28030.0},
    {"open": 28030.0, "high": 28058.0, "low": 28030.0, "close": 28045.0},
    {"open": 28045.0, "high": 28067.0, "low": 28045.0, "close": 28060.0},
    {"open": 28060.0, "high": 28086.0, "low": 28060.0, "close": 28075.0},
    {"open": 28075.0, "high": 28105.0, "low": 28075.0, "close": 28090.0},
    {"open": 28090.0, "high": 28114.0, "low": 28090.0, "close": 28105.0},
    {"open": 28105.0, "high": 28131.0, "low": 28105.0, "close": 28120.0},
    {"open": 28120.0, "high": 28148.0, "low": 28120.0, "close": 28135.0},
]

# Modeled on the real 09:30-11:00 ET 11/13 breakout earlier the same day
# (roughly 90-170pt high-low range per trailing 5-minute bar) -- see
# odd/tasks/tradovate-execution-bot.md Progress log.
VOLATILE_VOLATILITY_BARS = [
    {"open": 28000.0, "high": 28090.0, "low": 28000.0, "close": 28070.0},
    {"open": 28070.0, "high": 28180.0, "low": 28070.0, "close": 28160.0},
    {"open": 28160.0, "high": 28290.0, "low": 28160.0, "close": 28270.0},
    {"open": 28270.0, "high": 28420.0, "low": 28270.0, "close": 28400.0},
    {"open": 28400.0, "high": 28570.0, "low": 28400.0, "close": 28550.0},
    {"open": 28550.0, "high": 28690.0, "low": 28550.0, "close": 28670.0},
    {"open": 28670.0, "high": 28790.0, "low": 28670.0, "close": 28770.0},
    {"open": 28770.0, "high": 28930.0, "low": 28770.0, "close": 28910.0},
    {"open": 28910.0, "high": 29010.0, "low": 28910.0, "close": 28990.0},
]

# Boundary fixtures: every bar has the SAME range so the 9-bar average
# equals that exact range -- isolates the >= comparison at
# VOLATILITY_THRESHOLD_POINTS from any averaging noise.
BOUNDARY_AT_40_BARS = [
    {"open": 100.0, "high": 140.0, "low": 100.0, "close": 120.0},
] * 9

BOUNDARY_BELOW_40_BARS = [
    {"open": 100.0, "high": 139.0, "low": 100.0, "close": 120.0},
] * 9

BOUNDARY_ABOVE_40_BARS = [
    {"open": 100.0, "high": 141.0, "low": 100.0, "close": 120.0},
] * 9


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


def test_chase_guard_flags_chase_when_pullback_predates_the_current_run():
    result = signals.check_chase_guard(STALE_PULLBACK_THEN_CHASE_BARS, "sell")
    assert result.flagged is True
    assert "chasing" in result.reason.lower()


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


def test_evaluate_signal_blocks_on_the_chop_guard():
    # Contract change 2026-11-19: CHOP_BARS also fails the new direction guard
    # (its range retraces fully), so this no longer asserts "chop only".
    verdict = signals.evaluate_signal(CHOP_BARS, "buy")
    assert verdict.clear is False
    assert any("chop" in reason.lower() for reason in verdict.reasons)


def test_evaluate_signal_blocks_on_every_guard_when_bars_insufficient():
    # Contract change 2026-11-19: three guards (chase, chop, direction) fail closed.
    verdict = signals.evaluate_signal(TOO_FEW_BARS, "buy")
    assert verdict.clear is False
    assert len(verdict.reasons) == 3


def test_evaluate_signal_allows_when_neither_guard_fires():
    verdict = signals.evaluate_signal(NEITHER_FIRES_BARS, "buy")
    assert verdict.clear is True
    assert verdict.reasons == ()


def test_evaluate_signal_rejects_invalid_direction_fail_closed():
    verdict = signals.evaluate_signal(CYCLE_18_CHASE_BARS, "up")
    assert verdict.clear is False
    assert verdict.reasons


# ---------------------------------------------------------------- average_bar_range

def test_average_bar_range_computes_exact_mean_of_known_ranges():
    result = signals.average_bar_range(KNOWN_RANGE_BARS)
    assert result == pytest.approx(KNOWN_RANGE_MEAN)


def test_average_bar_range_malformed_bars_returns_none():
    assert signals.average_bar_range(None) is None
    assert signals.average_bar_range([]) is None
    assert signals.average_bar_range([{"high": 1.0}] * 10) is None


def test_average_bar_range_fewer_than_lookback_returns_none():
    assert signals.average_bar_range(TOO_FEW_BARS) is None


# ---------------------------------------------------------------- select_bracket_points

def test_select_bracket_points_quiet_regime_returns_narrow_bracket():
    assert signals.select_bracket_points(QUIET_VOLATILITY_BARS) == (16.75, 16.75)


def test_select_bracket_points_volatile_regime_returns_wide_bracket():
    assert signals.select_bracket_points(VOLATILE_VOLATILITY_BARS) == (33.5, 33.5)


def test_select_bracket_points_malformed_bars_fail_closed_to_narrow():
    assert signals.select_bracket_points(None) == (16.75, 16.75)
    assert signals.select_bracket_points(TOO_FEW_BARS) == (16.75, 16.75)


def test_select_bracket_points_boundary_at_threshold_is_wide():
    assert signals.select_bracket_points(BOUNDARY_AT_40_BARS) == (33.5, 33.5)


def test_select_bracket_points_boundary_just_below_threshold_is_narrow():
    assert signals.select_bracket_points(BOUNDARY_BELOW_40_BARS) == (16.75, 16.75)


def test_select_bracket_points_boundary_just_above_threshold_is_wide():
    assert signals.select_bracket_points(BOUNDARY_ABOVE_40_BARS) == (33.5, 33.5)


# ---------------------------------------------------------------- direction guard
# (odd/tasks/direction-guard.md) -- the guard that says whether the candidate
# direction is supported by the structure of the window.

def _mirror(bars, axis=60000.0):
    """Flip a bar list top-to-bottom: turns a rally into a decline of the
    same shape, so one fixture can test both directions symmetrically."""
    return [
        {"open": axis - b["open"], "high": axis - b["low"], "low": axis - b["high"], "close": axis - b["close"]}
        for b in bars
    ]


# The 9 bars (stamped 21:15..21:55 ET on 2026-11-18) behind the BUY that lost
# $200.50: a 102 pt rally (27407.75 -> 27510) that was FULLY retraced to a new
# low (27398.75), with lower highs afterwards. A failed rally, not a pullback.
FAILED_RALLY_BARS = [
    {"open": 27413.25, "high": 27496.50, "low": 27407.75, "close": 27493.25},
    {"open": 27493.00, "high": 27510.00, "low": 27468.00, "close": 27503.25},
    {"open": 27503.75, "high": 27506.75, "low": 27459.00, "close": 27488.25},
    {"open": 27488.75, "high": 27495.00, "low": 27455.00, "close": 27459.00},
    {"open": 27458.25, "high": 27461.50, "low": 27424.00, "close": 27430.50},
    {"open": 27431.50, "high": 27443.00, "low": 27410.00, "close": 27416.75},
    {"open": 27417.75, "high": 27428.75, "low": 27398.75, "close": 27405.00},
    {"open": 27405.25, "high": 27443.75, "low": 27401.00, "close": 27429.00},
    {"open": 27428.25, "high": 27463.00, "low": 27419.00, "close": 27459.50},
]


def test_direction_guard_flags_buy_after_a_fully_retraced_rally():
    result = signals.check_direction_guard(FAILED_RALLY_BARS, "buy")
    assert result.flagged is True
    assert "retraced" in result.reason.lower()


def test_direction_guard_does_not_flag_sell_on_the_same_failed_rally():
    # the down-leg 27510 -> 27398.75 is intact: the 27463 bounce retraced ~58%
    result = signals.check_direction_guard(FAILED_RALLY_BARS, "sell")
    assert result.flagged is False


def test_direction_guard_is_symmetric_for_a_fully_retraced_decline():
    mirrored = _mirror(FAILED_RALLY_BARS)
    sell = signals.check_direction_guard(mirrored, "sell")
    buy = signals.check_direction_guard(mirrored, "buy")
    assert sell.flagged is True and "retraced" in sell.reason.lower()
    assert buy.flagged is False


def test_direction_guard_allows_buy_on_a_real_pullback_continuation():
    # cycle 23: the pullback low (27640) stays far above the leg origin (27594.5)
    assert signals.check_direction_guard(PULLBACK_CONTINUATION_BARS, "buy").flagged is False


def test_direction_guard_allows_sell_on_a_mirrored_pullback_continuation():
    assert signals.check_direction_guard(_mirror(PULLBACK_CONTINUATION_BARS), "sell").flagged is False


def test_direction_guard_lets_a_still_extending_move_through_for_the_chase_guard():
    # swing high is the last bar: nothing to retrace yet (chase guard owns this case)
    assert signals.check_direction_guard(NEITHER_FIRES_BARS, "buy").flagged is False


def test_direction_guard_does_not_flag_sell_with_no_prior_down_leg():
    # Scope decision 2026-11-19: the replay showed refusing these removed winners (+234.5 pts).
    result = signals.check_direction_guard(NEITHER_FIRES_BARS, "sell")
    assert result.flagged is False
    assert "prior" in result.reason.lower()


def test_direction_guard_does_not_flag_buy_with_no_prior_up_leg():
    result = signals.check_direction_guard(_mirror(NEITHER_FIRES_BARS), "buy")
    assert result.flagged is False
    assert "prior" in result.reason.lower()


def test_direction_guard_does_not_flag_a_leg_too_small_to_be_a_trend():
    # four tiny bars (1 pt range, swing high 101.5 -> leg of 1.5 pts) followed by four wide
    # bars whose lows fall far below the origin: without the size exemption this would read
    # as a "fully retraced rally"; the leg is far smaller than the average range, so it is not.
    tiny = [{"open": 100.0, "high": 101.0, "low": 100.0, "close": 100.5} for _ in range(4)]
    tiny[3] = {"open": 100.0, "high": 101.5, "low": 100.0, "close": 100.5}  # highest high
    wide = [{"open": 100.0, "high": 101.0, "low": 80.0, "close": 90.0} for _ in range(4)]
    result = signals.check_direction_guard(tiny + wide, "buy")
    assert result.flagged is False
    assert "smaller" in result.reason.lower()


def test_direction_guard_fails_closed_on_bad_input():
    assert signals.check_direction_guard(FAILED_RALLY_BARS, "up").flagged is True
    assert signals.check_direction_guard(None, "buy").flagged is True
    assert signals.check_direction_guard([{"open": 1.0}] * 6, "buy").flagged is True
    assert signals.check_direction_guard(TOO_FEW_BARS, "buy").flagged is True


def test_direction_guard_only_looks_at_the_trailing_window():
    # an older, unrelated rally before the window must not leak into the verdict
    older = [{"open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5}] * 5
    assert signals.check_direction_guard(older + FAILED_RALLY_BARS, "buy").flagged is True
    assert signals.check_direction_guard(older + FAILED_RALLY_BARS, "sell").flagged is False


def test_evaluate_signal_blocks_buy_on_the_failed_rally_with_a_direction_reason():
    verdict = signals.evaluate_signal(FAILED_RALLY_BARS, "buy")
    assert verdict.clear is False
    assert any("retraced" in reason.lower() for reason in verdict.reasons)


def test_evaluate_signal_no_longer_says_clear_for_both_sides_on_the_failed_rally():
    buy = signals.evaluate_signal(FAILED_RALLY_BARS, "buy")
    sell = signals.evaluate_signal(FAILED_RALLY_BARS, "sell")
    assert not (buy.clear and sell.clear)
    assert not any("retraced" in reason.lower() for reason in sell.reasons)


# ---------------------------------------------------------------- late-bounce chase check
# (odd/tasks/fill-slippage-and-chase-size.md #4) A BUY that arrives after the bounce
# off a swing low has already recovered most of the prior down-leg is a late entry
# (a SELL: a decline that already gave back most of the prior up-leg). The literal
# "last bars moved more than 1.2 x the average range" rule was replayed and removed
# WINNERS, so the size dimension is the recovered fraction of the counter-leg.

def _late_bounce_bars(last_close):
    # down-leg 200 -> 150 (swing low 150), then a bounce whose last close is `last_close`
    return [
        {"open": 198.0, "high": 200.0, "low": 190.0, "close": 191.0},
        {"open": 191.0, "high": 192.0, "low": 180.0, "close": 181.0},
        {"open": 181.0, "high": 182.0, "low": 170.0, "close": 171.0},
        {"open": 171.0, "high": 172.0, "low": 160.0, "close": 161.0},
        {"open": 161.0, "high": 162.0, "low": 150.0, "close": 152.0},
        {"open": 152.0, "high": 170.0, "low": 152.0, "close": 165.0},
        {"open": 165.0, "high": 180.0, "low": 163.0, "close": 170.0},
        {"open": 170.0, "high": last_close + 2.0, "low": 168.0, "close": last_close},
    ]


def test_chase_guard_flags_a_buy_after_the_bounce_recovered_most_of_the_down_leg():
    result = signals.check_chase_guard(_late_bounce_bars(190.0), "buy")   # 80% of the 50 pt leg
    assert result.flagged is True
    assert "recovered" in result.reason.lower()


def test_chase_guard_allows_a_buy_when_the_bounce_recovered_only_half_the_down_leg():
    result = signals.check_chase_guard(_late_bounce_bars(175.0), "buy")   # 50%
    assert result.flagged is False


def test_chase_guard_late_bounce_check_is_symmetric_for_a_sell():
    mirrored = _mirror(_late_bounce_bars(190.0), axis=400.0)
    assert signals.check_chase_guard(mirrored, "sell").flagged is True
    assert signals.check_chase_guard(_mirror(_late_bounce_bars(175.0), axis=400.0), "sell").flagged is False


def test_chase_guard_late_bounce_check_ignores_a_window_with_no_prior_counter_leg():
    # the swing low is the first bar: nothing was recovered, so the check does not apply
    assert signals.check_chase_guard(PULLBACK_CONTINUATION_BARS, "buy").flagged is False
    assert signals.check_chase_guard(NEITHER_FIRES_BARS, "buy").flagged is False


def test_chase_guard_late_bounce_threshold_is_the_replayed_value():
    assert signals.MAX_COUNTER_LEG_RECOVERY == 0.75
