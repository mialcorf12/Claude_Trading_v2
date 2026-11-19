"""Own-clock tick generator for the overnight trading cycle.

The scheduler used by CronCreate adds a fixed per-job offset (up to half the
period), so it cannot fire at an exact minute. This script is run under the
Monitor tool instead: it prints one `CYCLE_TICK <iso-ET> late=<seconds>` line
at :05, :15, :25, :35, :45 and :55 ET, i.e. exactly when every second
5-minute bar closes (bars close at :x5:00 and :x0:00; NT8's LocalBarExporter
writes the file within ~1 s, so the cycle must verify that it is reading the
bar that just closed -- see tradovate_bot/CYCLE_PROMPT.md). Each line is an
event the session turns into one cycle.

Times are US Eastern (the VPS clock is ET). No network, no trading logic.

Run:  python -u -m tradovate_bot.cycle_clock
"""
import sys
import time
from datetime import timedelta

from tradovate_bot import time_utils

TICK_MINUTES = (5, 15, 25, 35, 45, 55)


def next_tick(now):
    """First tick strictly after `now` (timezone-aware), at second 0."""
    base = now.replace(second=0, microsecond=0)
    for minute in TICK_MINUTES:
        candidate = base.replace(minute=minute)
        if candidate > now:
            return candidate
    # past :56 -> first tick of the next hour (timedelta keeps wall-clock ET)
    return (base.replace(minute=0) + timedelta(hours=1)).replace(minute=TICK_MINUTES[0])


def format_tick_line(tick, now):
    late = max(0, int((now - tick).total_seconds()))
    return "CYCLE_TICK %s late=%d" % (tick.isoformat(), late)


def run(now_fn=None, sleep_fn=time.sleep, out=None, max_ticks=None):
    """Sleeps until each tick and prints its line. `max_ticks` is for tests."""
    now_fn = now_fn or time_utils.now_et
    out = out or sys.stdout
    emitted = 0
    tick = next_tick(now_fn())
    while max_ticks is None or emitted < max_ticks:
        remaining = (tick - now_fn()).total_seconds()
        if remaining > 0:
            # short sleeps so a clock adjustment or suspend is noticed quickly
            sleep_fn(min(remaining, 1.0))
            continue
        out.write(format_tick_line(tick, now_fn()) + "\n")
        out.flush()
        emitted += 1
        tick = next_tick(max(now_fn(), tick))


if __name__ == "__main__":
    run()
