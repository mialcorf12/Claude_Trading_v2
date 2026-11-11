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
cron cycles. There is no caller inside the bot yet: wiring this into
executor.py / the cron prompt is a separate, later decision (task 2 is
this module and its tests only).

Bar shape: each bar is a plain dict with at least `open`, `high`, `low`,
`close` (finite floats) and an optional `timestamp`. This matches the OHLC
bars `mcp-tv-get-ohlcv` returns (5-minute intervals, most recent last) but
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
  - CHOP_WINDOW_BARS = 9 and CHOP_SWING_THRESHOLD = 2: the task's own
    phrasing says to look at "roughly the last 30-45 minutes" -- 9 bars
    of 5-minute data is 45 minutes. Two or more swing highs/lows in that
    window means price has reversed at least twice, i.e. "a price zone
    that has already reversed direction multiple times," per the rule's
    own wording -- not just a single pullback, which is normal and
    expected inside any real trend.
  - MIN_BARS_FOR_CHASE_CHECK / MIN_BARS_FOR_CHOP_CHECK = 5: a guard needs
    at least one full streak's worth of bars (MIN_CHASE_STREAK + 1) or
    enough bars to see a swing with two confirmed sides, respectively,
    before its verdict means anything; below that it fails closed.
"""
import math
from dataclasses import dataclass

VALID_DIRECTIONS = ("buy", "sell")

MIN_CHASE_STREAK = 4
CHASE_LOOKBACK_BARS = 8
MIN_BARS_FOR_CHASE_CHECK = MIN_CHASE_STREAK + 1  # 5

CHOP_WINDOW_BARS = 9
CHOP_SWING_THRESHOLD = 2
MIN_BARS_FOR_CHOP_CHECK = 5

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

def check_chase_guard(bars, direction):
    """Pullback-over-chase guard (rule 1, see module docstring).

    Looks at the trailing CHASE_LOOKBACK_BARS closes. If any bar-over-bar
    move against `direction` exists anywhere in that window, a real
    retracement has already happened -- not chasing, regardless of how
    long the subsequent run is (this is what lets cycle 23's pullback-
    then-continuation through). If NO such retracement exists anywhere in
    the window AND there are at least MIN_CHASE_STREAK bar-over-bar moves
    in favor of `direction`, price has done nothing but set new extremes
    with zero pullback -- this is a chase (cycle 18's exact scenario).

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

    favor_steps = 0
    pullback_found = False
    for prev, curr in zip(closes, closes[1:]):
        if direction == "buy":
            moved_favor, moved_against = curr > prev, curr < prev
        else:  # "sell"
            moved_favor, moved_against = curr < prev, curr > prev
        if moved_favor:
            favor_steps += 1
        elif moved_against:
            pullback_found = True

    if pullback_found:
        return GuardResult(
            False,
            "a retracement against the %s move was found in the trailing %d-bar window -- not chasing"
            % (direction, len(window)),
        )

    if favor_steps >= MIN_CHASE_STREAK:
        return GuardResult(
            True,
            "%d consecutive new extremes with zero retracement -- chasing an extended %s move "
            "(see cycle 18 root cause, odd/tasks/tradovate-execution-bot.md)" % (favor_steps, direction),
        )

    return GuardResult(
        False,
        "no pullback yet, but only %d/%d consecutive %s steps -- not extended enough to call it chasing"
        % (favor_steps, MIN_CHASE_STREAK, direction),
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


# ---------------------------------------------------------------- top-level verdict

def evaluate_signal(bars, direction):
    """Run both guards for a candidate direction and return one
    SignalVerdict. `clear=True` only when neither guard fired; otherwise
    `reasons` carries every guard's reason that did fire, so a blocked
    caller sees exactly what tripped (chase, chop, or both -- e.g. when
    bars are too few to confirm either).

    This is the one function an external caller (the cron prompt's
    procedure, or any future automated caller -- see this module's
    docstring, not wired into executor.py yet) actually needs: it does
    not replace the broader discretionary read, only formalizes these two
    specific guardrails."""
    if direction not in VALID_DIRECTIONS:
        return SignalVerdict(False, direction, ("direction must be 'buy' or 'sell', got %r" % (direction,),))

    chase = check_chase_guard(bars, direction)
    chop = check_chop_guard(bars)

    reasons = tuple(result.reason for result in (chase, chop) if result.flagged)
    return SignalVerdict(clear=not reasons, direction=direction, reasons=reasons)
