"""Tests for tradovate_bot.cycle_clock (own-clock tick generator for the cron cycle)."""
from datetime import datetime

from tradovate_bot import cycle_clock, time_utils

ET = time_utils.EASTERN


def _et(*args):
    return datetime(*args, tzinfo=ET)


def test_tick_minutes_fire_on_every_second_five_minute_bar_close():
    assert cycle_clock.TICK_MINUTES == (5, 15, 25, 35, 45, 55)


def test_next_tick_within_the_same_hour():
    assert cycle_clock.next_tick(_et(2026, 11, 18, 23, 28, 10)) == _et(2026, 11, 18, 23, 35)


def test_next_tick_is_strictly_after_an_exact_tick():
    assert cycle_clock.next_tick(_et(2026, 11, 18, 23, 35, 0)) == _et(2026, 11, 18, 23, 45)


def test_next_tick_just_before_a_tick():
    assert cycle_clock.next_tick(_et(2026, 11, 18, 23, 34, 59, 900000)) == _et(2026, 11, 18, 23, 35)


def test_next_tick_rolls_into_the_next_hour():
    assert cycle_clock.next_tick(_et(2026, 11, 18, 22, 57, 0)) == _et(2026, 11, 18, 23, 5)


def test_next_tick_rolls_into_the_next_day():
    assert cycle_clock.next_tick(_et(2026, 11, 18, 23, 56, 30)) == _et(2026, 11, 19, 0, 5)


def test_next_tick_before_the_first_tick_of_the_hour():
    assert cycle_clock.next_tick(_et(2026, 11, 18, 21, 0, 0)) == _et(2026, 11, 18, 21, 5)


def test_next_tick_drops_seconds_and_microseconds():
    tick = cycle_clock.next_tick(_et(2026, 11, 18, 21, 10, 33, 123456))
    assert (tick.second, tick.microsecond) == (0, 0)
    assert tick == _et(2026, 11, 18, 21, 15)


def test_format_tick_line_is_machine_greppable_and_reports_lateness():
    tick = _et(2026, 11, 18, 23, 35)
    now = _et(2026, 11, 18, 23, 35, 2, 400000)
    line = cycle_clock.format_tick_line(tick, now)
    assert line == "CYCLE_TICK 2026-11-18T23:35:00-05:00 late=2"


def test_format_tick_line_never_reports_negative_lateness():
    tick = _et(2026, 11, 18, 23, 35)
    now = _et(2026, 11, 18, 23, 34, 59, 900000)
    assert cycle_clock.format_tick_line(tick, now).endswith("late=0")
