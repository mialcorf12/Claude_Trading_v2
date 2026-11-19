"""Pure, deterministic signal guardrails for the Tradovate execution bot.

No I/O, no network calls, no LLM calls -- same constraint as risk_guard.py
and position_state.py (see odd/tasks/tradovate-architecture-scaling.md
task 2's "no LLM call anywhere" constraint). Every function here is a
plain function of a list of OHLCV bars so it can be unit tested with
synthetic fixtures and reasoned about with certainty, instead of being
re-derived from memory by an LLM every cron cycle.

This formalizes two decision-quality rules established LIVE this session,
after root-causing two real consecutive losing trades (see
odd/tasks/tradovate-execution-bot.md, cycles 18-23):

1. pullback-over-chase (check_chase_guard): prefer an entry on a confirmed
   pullback over chasing an already-extended move that shows zero
   retracement. Cycle 18 bought a fourth consecutive higher close with no
   pullback at all off the 11:55 low and was stopped out minutes later --
   the real scenario CYCLE_18_CHASE_BARS in the test module is drawn from.
   Cycle 23, by contrast, bought AFTER a real retracement (27656 ->
   27643.25) resolved upward, and that trade won on take-profit.
2. repeated-reversal-is-chop (check_chop_guard): a price zone that has
   already reversed direction multiple times in the recent window reads
   as a weaker/choppier signal, even when the last few bars look
   directional, since a zone that keeps getting rejected is less likely
   to break cleanly. Cycle 20's 27540-27690 zone (breakdown, a bounce to
   a LOWER high than the prior swing, pulling back again) is the real
   scenario this codifies.

These guards do NOT replace the broader discretionary read (volume, news,
overall trend) -- they only formalize these two specific, previously
prose-only rules so they are testable and can't silently drift between
cron cycles. This is now wired into tradovate_bot.executor.process_order
(see odd/tasks/tradovate-architecture-scaling.md task 4): every entry
order's decision flow runs evaluate_signal before placing an order.

Bar shape: each bar is a plain dict with at least `open`, `high`, `low`,
`close` (finite floats) and an optional `timestamp`. This matches the OHLC
bars `get_local_candles` (local-market-data MCP) returns (5-minute
intervals, most recent last, `time` in ET) but
these functions never touch the network -- they take whatever list of
bars the caller already fetched.

Fail-closed contract (per the task's "cannot confirm, don't trade" edge
case): both guards treat malformed input (not a list of dicts, missing/
non-finite OHLC fields, an unrecognized direction) and too few bars to
evaluate as `flagged=True` -- i.e. as a positive guard hit, not a silent
pass. A caller can never read "we don't have enough data" as "the move is
clean."

Threshold choices, documented here so they don't silently drift either:
  - MIN_CHASE_STREAK = 4: cycle 18's own root-cause note says "four
    consecutive higher closes ... with no pullback yet" -- four
    consecutive new-extreme closes in one direction with zero
    retracement anywhere in the lookback window is the bar for "this is
    a chase," matching the actual loss that motivated this rule.
  - CHASE_LOOKBACK_BARS = 8: roughly 40 minutes of 5-minute bars -- long
    enough to catch a pullback that happened a few bars before the
    current one (cycle 23's pullback was ~3 bars before the resumption),
    short enough that a pullback from hours ago doesn't excuse chasing
    the current extension.
  - CHOP_WINDOW_BARS = 9 and CHOP_SWING_THRESHOLD = 3 (raised from 2 on
    2026-11-10): the task's own phrasing says to look at "roughly the
    last 30-45 minutes" -- 9 bars of 5-minute data is 45 minutes. A
    single normal pullback-then-continuation already produces exactly 2
    swing points (one high, one low), so a threshold of 2 fired on the
    very first clean retracement inside an otherwise healthy trend --
    confirmed live: three consecutive overnight cycles skipped a
    sustained uptrend (28198 -> 28216.75 -> 28220, each a HIGHER high
    than the last) because each one's lone pullback tripped the old
    threshold. Cycle 20's actual scenario (a bounce to a LOWER high than
    the prior swing, pulling back again) needs a genuine second reversal
    to reproduce, which takes 3+ swing points, not 2. Raising the
    threshold to 3 is meant to let a single confirmed pullback through
    (matching cycle 23's winning pattern) while still catching true
    repeated back-and-forth.
  - MIN_BARS_FOR_CHASE_CHECK / MIN_BARS_FOR_CHOP_CHECK = 5: a guard needs
    at least one full streak's worth of bars (MIN_CHASE_STREAK + 1) or
    enough bars to see a swing with two confirmed sides, respectively,
    before its verdict means anything; below that it fails closed.

Dynamic bracket sizing (select_bracket_points / average_bar_range), added
2026-11-13 per explicit user request for "tu criterio técnico" on when to
size the stop-loss/take-profit bracket wider vs narrower:
  - VOLATILITY_LOOKBACK_BARS = 9, the same window CHOP_WINDOW_BARS already
    uses -- the trailing 45 minutes of 5-minute bars.
  - VOLATILITY_THRESHOLD_POINTS = 40.0: grounded in real 2026-11-12/13
    session data (see odd/tasks/tradovate-execution-bot.md Progress log
    and the SL:TP sizing analysis referenced there). The quiet overnight
    chop period 00:00-00:16 ET 11/13 averaged roughly 20-30 points of
    high-low range per trailing 5-minute bar, while the 09:30-11:00 ET
    breakout earlier that same day averaged roughly 90-170 points per
    bar -- 40 points sits with wide margin between the two observed
    regimes, so it will not flip on ordinary noise inside either one.
  - NARROW_BRACKET_POINTS = 16.75 (the existing default, ~$100 at
    $6/point for 3 contracts) and WIDE_BRACKET_POINTS = 33.5 (~$200 at
    $6/point for 3 contracts): a narrow $100 bracket gets stopped out by
    the noise of a high-volatility regime before it can reach its
    target -- demonstrated empirically this week, where a backtest-style
    analysis found a wide-but-equal bracket produced a positive net
    result in a high-volatility regime while the narrow bracket lost
    money net in that same regime, even though the narrow bracket's win
    RATE was often similar or higher. Win rate and profitability are not
    the same thing, which is the whole reason this sizing is now
    volatility-aware instead of fixed.
  - select_bracket_points fails closed to NARROW_BRACKET_POINTS (not the
    wider one) whenever average_bar_range can't produce a value -- the
    smaller-dollar-risk option is the safer failure mode, consistent
    with this module's and risk_guard's general fail-closed-toward-
    caution posture.
"""
import math
from dataclasses import dataclass

VALID_DIRECTIONS = ("buy", "sell")

MIN_CHASE_STREAK = 4
CHASE_LOOKBACK_BARS = 8
MIN_BARS_FOR_CHASE_CHECK = MIN_CHASE_STREAK + 1  # 5
# Late-bounce check (odd/tasks/fill-slippage-and-chase-size.md #4): refuse an entry
# when the counter-move has already recovered more than this fraction of the prior
# counter-leg. Replay of 2026-11-18 (97 trades the other guards allowed): >75%
# removed 25 trades netting -150.8 pts; >50% removed 31 netting -100.5 pts. The
# literal "last bars moved > k x average range" variants removed WINNERS, so they
# are not used. Small, in-sample evidence.
MAX_COUNTER_LEG_RECOVERY = 0.75

CHOP_WINDOW_BARS = 9
CHOP_SWING_THRESHOLD = 3
MIN_BARS_FOR_CHOP_CHECK = 5

DIRECTION_WINDOW_BARS = 9
MIN_BARS_FOR_DIRECTION_CHECK = 5
MIN_LEG_AVG_RANGES = 1.0   # a leg smaller than this many average bar ranges is not a trend

VOLATILITY_LOOKBACK_BARS = 9
NARROW_BRACKET_POINTS = 16.75   # ~$100 at $6/point (3 contracts), the existing default
WIDE_BRACKET_POINTS = 33.5      # ~$200 at $6/point (3 contracts)
VOLATILITY_THRESHOLD_POINTS = 40.0

_OHLC_FIELDS = ("open", "high", "low", "close")


@dataclass(frozen=True)
class GuardResult:
    """One guard's verdict: `flagged=True` means the guard fired (block /
    be skeptical), with `reason` explaining why -- same explain-the-
    reasoning convention as risk_guard.Decision, but named separately
    since a guard firing is a problem being flagged, not a permission
    being granted."""
    flagged: bool
    reason: str


@dataclass(frozen=True)
class SignalVerdict:
    """Combined verdict from every guard for one candidate direction.
    `clear=True` only when no guard fired; `reasons` holds every guard's
    reason that did fire (empty tuple when clear)."""
    clear: bool
    direction: str
    reasons: tuple


def _is_finite_number(v):
    """True only for a real, finite int/float. Rejects bool, NaN, inf,
    str -- same helper/contract as risk_guard._is_finite_number, kept
    local to this module so signals.py stays a standalone pure module
    with no cross-import on risk_guard."""
    if isinstance(v, bool):
        return False
    if not isinstance(v, (int, float)):
        return False
    try:
        return math.isfinite(v)
    except (TypeError, ValueError):
        return False


def _valid_bars(bars):
    """True only if bars is a non-empty list of dicts, each carrying
    finite open/high/low/close numbers. A single malformed bar (missing
    field, NaN, wrong type) invalidates the whole evaluation -- there is
    no "skip the bad bar and keep going," per this module's fail-closed
    contract."""
    if not isinstance(bars, list) or not bars:
        return False
    for bar in bars:
        if not isinstance(bar, dict):
            return False
        for field in _OHLC_FIELDS:
            if not _is_finite_number(bar.get(field)):
                return False
    return True


# ---------------------------------------------------------------- individual guards

def _counter_leg_recovery(bars, direction):
    """Fraction of the prior counter-leg already recovered, or None when it does
    not apply. For a buy: the swing low is the last bar with the lowest low in
    the trailing DIRECTION_WINDOW_BARS, the counter-leg runs from the highest
    high BEFORE it down to that low, and the recovery is (last close - swing
    low) / leg. A sell mirrors it. None when the swing is the first bar (no
    counter-leg) or the leg is zero."""
    window = bars[-DIRECTION_WINDOW_BARS:]
    highs = [b["high"] for b in window]
    lows = [b["low"] for b in window]
    last = window[-1]["close"]
    if direction == "buy":
        extreme = min(lows)
        i_ext = len(lows) - 1 - lows[::-1].index(extreme)
        before = highs[:i_ext]
        if not before:
            return None
        leg = max(before) - extreme
        return (last - extreme) / leg if leg > 0 else None
    extreme = max(highs)
    i_ext = len(highs) - 1 - highs[::-1].index(extreme)
    before = lows[:i_ext]
    if not before:
        return None
    leg = extreme - min(before)
    return (extreme - last) / leg if leg > 0 else None


def check_chase_guard(bars, direction):
    """Pullback-over-chase guard (rule 1, see module docstring).

    Looks at the trailing CHASE_LOOKBACK_BARS closes and measures the
    CURRENT unbroken run of bar-over-bar moves in favor of `direction`,
    counted backwards from the most recent bar. If that trailing run is
    at least MIN_CHASE_STREAK steps long, price has set that many new
    extremes in a row with zero retracement SINCE the run started -- a
    chase (cycle 18's exact scenario), regardless of whether an earlier,
    now-stale retracement sits further back in the window. If the
    trailing run is shorter than MIN_CHASE_STREAK, it is not extended
    enough to call it chasing, whether or not an earlier retracement
    exists -- this is what lets cycle 23's pullback-then-continuation
    through (the pullback is the most recent break in the run, so the
    run restarts clean afterward and is short at read time).

    Fixed 2026-11-16: the original version treated ANY retracement
    anywhere in the lookback as clearing the guard permanently, even for
    a run that kept extending with zero pullback long after that
    retracement -- confirmed live (~14:37 ET) when a 4-bar straight
    decline cleared as "not chasing" solely because of an unrelated
    uptick several bars earlier. Recency, not mere presence, is what the
    rule was always meant to require.

    A second, size-based condition (added 2026-11-19): flagged as a "late
    entry" when the counter-move has already recovered more than
    MAX_COUNTER_LEG_RECOVERY of the prior counter-leg (see
    _counter_leg_recovery and the constant's comment for the replay evidence).

    Fails closed (flagged=True) for an unrecognized direction, malformed
    bars, or fewer than MIN_BARS_FOR_CHASE_CHECK bars -- there is no
    "assume clean" path when a pullback can't actually be confirmed."""
    if direction not in VALID_DIRECTIONS:
        return GuardResult(True, "direction must be 'buy' or 'sell', got %r" % (direction,))
    if not _valid_bars(bars):
        return GuardResult(True, "bars missing or malformed -- cannot confirm a pullback, failing closed")
    if len(bars) < MIN_BARS_FOR_CHASE_CHECK:
        return GuardResult(
            True,
            "insufficient bars (%d < %d) to confirm a pullback exists -- failing closed, can't rule out chasing"
            % (len(bars), MIN_BARS_FOR_CHASE_CHECK),
        )

    window = bars[-CHASE_LOOKBACK_BARS:]
    closes = [b["close"] for b in window]

    steps = []
    for prev, curr in zip(closes, closes[1:]):
        if direction == "buy":
            moved_favor, moved_against = curr > prev, curr < prev
        else:  # "sell"
            moved_favor, moved_against = curr < prev, curr > prev
        steps.append(True if moved_favor else False if moved_against else None)

    run = 0
    for step in reversed(steps):
        if step is True:
            run += 1
        else:
            break

    if run >= MIN_CHASE_STREAK:
        return GuardResult(
            True,
            "%d consecutive new extremes with zero retracement since the run started -- chasing an "
            "extended %s move (see cycle 18 root cause, odd/tasks/tradovate-execution-bot.md)"
            % (run, direction),
        )

    recovered = _counter_leg_recovery(bars, direction)
    if recovered is not None and recovered > MAX_COUNTER_LEG_RECOVERY:
        return GuardResult(
            True,
            "late entry: the counter-move has already recovered %.0f%% of the prior %s counter-leg "
            "(limit %.0f%%) -- chasing a bounce that is mostly spent"
            % (100 * recovered, "down" if direction == "buy" else "up", 100 * MAX_COUNTER_LEG_RECOVERY),
        )

    pullback_found = any(step is False for step in steps)
    if pullback_found:
        return GuardResult(
            False,
            "a retracement against the %s move was found in the trailing %d-bar window, and the "
            "current run (%d step(s)) is short of the %d-step chase threshold -- not chasing"
            % (direction, len(window), run, MIN_CHASE_STREAK),
        )

    return GuardResult(
        False,
        "no pullback yet, but only %d/%d consecutive %s steps -- not extended enough to call it chasing"
        % (run, MIN_CHASE_STREAK, direction),
    )


def check_chop_guard(bars):
    """Repeated-reversal-is-chop guard (rule 2, see module docstring).

    Counts interior swing highs/lows -- a bar whose high is above both
    neighbors' highs, or whose low is below both neighbors' lows -- within
    the trailing CHOP_WINDOW_BARS. CHOP_SWING_THRESHOLD or more swing
    points means the zone has reversed direction at least that many times
    in roughly the last 30-45 minutes (cycle 20's 27540-27690 range),
    which reads as chop regardless of which way the last bar or two leans.

    Direction-agnostic on purpose: a choppy zone is choppy for either
    side, unlike the chase guard which is specific to the candidate
    direction.

    Fails closed (flagged=True) for malformed bars or fewer than
    MIN_BARS_FOR_CHOP_CHECK bars -- there is no "assume clean" path when
    there isn't enough window to see whether it has reversed."""
    if not _valid_bars(bars):
        return GuardResult(True, "bars missing or malformed -- cannot rule out chop, failing closed")
    if len(bars) < MIN_BARS_FOR_CHOP_CHECK:
        return GuardResult(
            True,
            "insufficient bars (%d < %d) to assess the trailing window for repeated reversals -- failing closed"
            % (len(bars), MIN_BARS_FOR_CHOP_CHECK),
        )

    window = bars[-CHOP_WINDOW_BARS:]
    highs = [b["high"] for b in window]
    lows = [b["low"] for b in window]

    swings = 0
    for i in range(1, len(window) - 1):
        is_swing_high = highs[i] > highs[i - 1] and highs[i] > highs[i + 1]
        is_swing_low = lows[i] < lows[i - 1] and lows[i] < lows[i + 1]
        if is_swing_high or is_swing_low:
            swings += 1

    if swings >= CHOP_SWING_THRESHOLD:
        return GuardResult(
            True,
            "%d direction reversal(s) in the trailing %d-bar window -- zone reads as chop "
            "(see cycle 20, odd/tasks/tradovate-execution-bot.md), not a clean break"
            % (swings, len(window)),
        )

    return GuardResult(False, "only %d direction reversal(s) in the trailing window -- not chop" % swings)


def check_direction_guard(bars, direction):
    """Direction-support guard (rule 3, odd/tasks/direction-guard.md).

    The chase and chop guards never look at direction, so on 2026-11-18 a
    failed rally (a 102 pt rally FULLY retraced to a new low, lower highs
    after) was 'clear' for both buy and sell and a BUY on it lost $200.50.
    This guard asks whether the candidate direction is actually supported
    by the structure of the trailing DIRECTION_WINDOW_BARS bars.

    For a buy: the swing high is the LAST bar carrying the window's highest
    high; the leg's origin is the lowest low BEFORE it. Flagged ONLY when
    the leg is meaningful (at least MIN_LEG_AVG_RANGES average bar ranges)
    and the lowest low AFTER the swing high is at or below the origin: the
    rally was fully retraced, a failed rally and not a pullback. A sell
    mirrors this with highs and lows swapped. Not flagged: a move still
    extending (the swing is the last bar -- the chase guard owns that
    case), a swing with no prior leg, or a leg too small to be a trend.

    Scope decision 2026-11-19: a first version also refused "no prior leg"
    and "leg too small". Replaying the 2026-11-18 session (191 windows, 5m
    bars, simulated 1:1 bracket) showed those two extra rules removed
    winners (+234.5 and +67.0 pts) while the retrace rule removed losers
    (-150.8 pts), so only the retrace rule is enforced. Small, in-sample
    evidence; see odd/tasks/direction-guard.md.

    Fails closed (flagged=True) for an unrecognized direction, malformed
    bars, or fewer than MIN_BARS_FOR_DIRECTION_CHECK bars."""
    if direction not in VALID_DIRECTIONS:
        return GuardResult(True, "direction must be 'buy' or 'sell', got %r" % (direction,))
    if not _valid_bars(bars):
        return GuardResult(True, "bars missing or malformed -- cannot confirm the direction is supported, failing closed")
    if len(bars) < MIN_BARS_FOR_DIRECTION_CHECK:
        return GuardResult(
            True,
            "insufficient bars (%d < %d) to confirm the %s direction is supported -- failing closed"
            % (len(bars), MIN_BARS_FOR_DIRECTION_CHECK, direction),
        )

    window = bars[-DIRECTION_WINDOW_BARS:]
    highs = [b["high"] for b in window]
    lows = [b["low"] for b in window]
    avg_range = sum(h - l for h, l in zip(highs, lows)) / len(window)

    if direction == "buy":
        extreme = max(highs)
        i_ext = len(highs) - 1 - highs[::-1].index(extreme)
        before, after = lows[:i_ext], lows[i_ext + 1:]
        leg_name, trend_name = "up-leg", "rally"
        if not before:
            return GuardResult(
                False,
                "no prior up-leg in the trailing %d-bar window (the swing high is its first bar): nothing "
                "to retrace, not flagged" % len(window),
            )
        origin = min(before)
        leg = extreme - origin
        retraced = bool(after) and min(after) <= origin
        retrace_low = min(after) if after else None
    else:  # "sell"
        extreme = min(lows)
        i_ext = len(lows) - 1 - lows[::-1].index(extreme)
        before, after = highs[:i_ext], highs[i_ext + 1:]
        leg_name, trend_name = "down-leg", "decline"
        if not before:
            return GuardResult(
                False,
                "no prior down-leg in the trailing %d-bar window (the swing low is its first bar): nothing "
                "to retrace, not flagged" % len(window),
            )
        origin = max(before)
        leg = origin - extreme
        retraced = bool(after) and max(after) >= origin
        retrace_low = max(after) if after else None

    if leg < MIN_LEG_AVG_RANGES * avg_range:
        return GuardResult(
            False,
            "the prior %s (%.2f pts) is smaller than %.1f average bar ranges (%.2f pts): no meaningful trend, "
            "not flagged" % (leg_name, leg, MIN_LEG_AVG_RANGES, avg_range),
        )
    if retraced:
        return GuardResult(
            True,
            "the %s (%.2f -> %.2f) was fully retraced (pullback reached %.2f, at or beyond its origin): "
            "a failed %s, not a pullback (see 2026-11-18, odd/tasks/direction-guard.md)"
            % (trend_name, origin, extreme, retrace_low, trend_name),
        )
    return GuardResult(
        False,
        "the %s (%.2f pts) is intact: %s" % (
            leg_name, leg,
            "no retracement yet" if not after else "the pullback did not reach its origin",
        ),
    )


# ---------------------------------------------------------------- top-level verdict

def evaluate_signal(bars, direction):
    """Run every guard for a candidate direction and return one
    SignalVerdict. `clear=True` only when no guard fired; otherwise
    `reasons` carries every guard's reason that did fire, in the order
    chase, chop, direction, so a blocked caller sees exactly what tripped
    (e.g. all three when bars are too few to confirm anything).

    The direction guard (added 2026-11-19) is what stops this returning
    'clear' for both buy and sell on the same bars. This is the one
    function an external caller (executor.process_order, and the cron
    prompt's procedure) actually needs: it does not replace the broader
    discretionary read, only formalizes these specific guardrails."""
    if direction not in VALID_DIRECTIONS:
        return SignalVerdict(False, direction, ("direction must be 'buy' or 'sell', got %r" % (direction,),))

    chase = check_chase_guard(bars, direction)
    chop = check_chop_guard(bars)
    direction_support = check_direction_guard(bars, direction)

    reasons = tuple(result.reason for result in (chase, chop, direction_support) if result.flagged)
    return SignalVerdict(clear=not reasons, direction=direction, reasons=reasons)


# ---------------------------------------------------------------- bracket sizing

def average_bar_range(bars, lookback=VOLATILITY_LOOKBACK_BARS):
    """Average high-low range across the trailing `lookback` bars, in
    points. Returns None (fail-closed: caller must not assume a
    volatility regime when this can't be computed) for malformed bars or
    fewer than `lookback` bars -- same fail-closed contract as
    check_chop_guard/check_chase_guard in this module: there is no
    "assume quiet" path when range can't actually be confirmed."""
    if not _valid_bars(bars):
        return None
    if len(bars) < lookback:
        return None

    window = bars[-lookback:]
    ranges = [b["high"] - b["low"] for b in window]
    return sum(ranges) / len(ranges)


def select_bracket_points(bars):
    """Returns (stop_loss_points, take_profit_points) as an equal pair,
    chosen by recent volatility: WIDE_BRACKET_POINTS when the trailing
    VOLATILITY_LOOKBACK_BARS-bar average range is >=
    VOLATILITY_THRESHOLD_POINTS, else NARROW_BRACKET_POINTS. Fails closed
    to the narrower (smaller-risk) bracket -- not the wider one -- whenever
    average_bar_range() can't produce a value, since defaulting to the
    smaller-dollar-risk option is the safer failure mode, consistent with
    this module's and risk_guard's general fail-closed-toward-caution
    posture."""
    avg = average_bar_range(bars)
    if avg is None:
        return (NARROW_BRACKET_POINTS, NARROW_BRACKET_POINTS)
    if avg >= VOLATILITY_THRESHOLD_POINTS:
        return (WIDE_BRACKET_POINTS, WIDE_BRACKET_POINTS)
    return (NARROW_BRACKET_POINTS, NARROW_BRACKET_POINTS)
