"""Offline replay harness for the signal guards and the bracket.

Several rules in this bot were decided (or rejected) by replaying a session of
5-minute candles: the direction guard, the late-bounce chase check, the
fill-slippage limit and the structural-stop idea (odd/tasks/direction-guard.md,
fill-slippage-and-chase-size.md, structural-stop.md). Each replay used an ad hoc
script. This module is the reusable version.

What it does
  - walks every window of `window` consecutive bars, takes each trade the guards
    would allow (both sides), and simulates it on the following bars: entry at the
    next bar's open, stop = target = signals.select_bracket_points(...), stop first
    when both are touched in the same bar, marked to the last close when neither
    is hit within `horizon` bars. P&L is in dollars (MNQ, $2 per point, 3
    contracts, no costs).
  - `guard_ablation` reports, for each guard on its own, what it removes and what
    it keeps: a guard that removes losers (negative net of removed) is earning its
    keep; one that removes winners is not.
  - windows that cross a market closure (bars not exactly 5 minutes apart) are
    skipped unless allow_gaps=True.
  - `--archive` merges candles/MNQ_5m.json (a rolling 200-bar file) into a growing
    history, so replays can cover many sessions instead of the last 16 hours.

Caveats, always: simulated fills, overlapping windows (trades are not
independent), no costs, and few sessions. Treat results as a smoke test that can
reject a rule, not as proof that one works.

Run:
  python -m tradovate_bot.replay                 # guard ablation on the history
  python -m tradovate_bot.replay --archive       # merge the live candles into the history
  python -m tradovate_bot.replay --allow-gaps --candles path/to/MNQ_5m.json
"""
import argparse
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from tradovate_bot import signals

POINT_VALUE = 2.0        # MNQ: $ per point per contract
CONTRACTS = 3
BAR_MINUTES = 5
WINDOW = 10              # bars the cron fetches (the guards read the trailing 9)
HORIZON = 12             # bars a simulated trade may stay open

CANDLES_DIR = Path(__file__).resolve().parents[1] / "candles"
LIVE_CANDLES = CANDLES_DIR / "MNQ_5m.json"
HISTORY = CANDLES_DIR / "archive" / "MNQ_5m_history.json"

_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


@dataclass(frozen=True)
class Trade:
    end: int            # index of the last bar the decision saw
    side: str
    window: tuple       # the bars the decision saw
    sl: float           # stop distance (= target distance), points
    pnl_points: float
    pnl_dollars: float


# ---------------------------------------------------------------- data

def load_bars(path):
    """Bars of an exported candles file (UTF-8 with or without BOM)."""
    with open(path, "r", encoding="utf-8-sig") as fh:
        return json.load(fh)["bars"]


def merge_history(src, history):
    """Merges the bars of `src` into the history file (created if missing):
    de-duplicated by time, sorted, the newer copy of a bar winning. Returns the
    number of bars whose time was not in the history before."""
    history = Path(history)
    existing = {b["time"]: b for b in load_bars(history)} if history.exists() else {}
    incoming = load_bars(src)
    added = sum(1 for b in incoming if b["time"] not in existing)
    for bar in incoming:
        existing[bar["time"]] = bar
    merged = [existing[t] for t in sorted(existing)]
    history.parent.mkdir(parents=True, exist_ok=True)
    tmp = history.with_suffix(history.suffix + ".tmp")
    tmp.write_text(json.dumps({"symbol": "MNQ", "timeframe": "5m", "bars": merged}), encoding="utf-8")
    os.replace(tmp, history)
    return added


# ---------------------------------------------------------------- simulation

def _ts(bar):
    return datetime.strptime(bar["time"], _TIME_FORMAT)


def _contiguous(bars):
    step = timedelta(minutes=BAR_MINUTES)
    return all(_ts(b) - _ts(a) == step for a, b in zip(bars, bars[1:]))


def candidate_windows(bars, window=WINDOW, horizon=HORIZON, allow_gaps=False):
    """Yields the index `end` of the last decision bar of every usable window:
    there must be a bar to enter on and `horizon` bars after it, and (unless
    allow_gaps) the whole span must be consecutive 5-minute bars."""
    for end in range(window - 1, len(bars)):
        if end + 1 + horizon > len(bars):
            break
        if not allow_gaps and not _contiguous(bars[end - window + 1: end + 1 + horizon]):
            continue
        yield end


def simulate_trade(bars, side, entry_index, sl, tp=None, horizon=HORIZON):
    """P&L in points of one bracketed trade entered at bars[entry_index]["open"]."""
    tp = sl if tp is None else tp
    entry = bars[entry_index]["open"]
    stop = entry - sl if side == "buy" else entry + sl
    target = entry + tp if side == "buy" else entry - tp
    for bar in bars[entry_index: entry_index + horizon]:
        hit_stop = bar["low"] <= stop if side == "buy" else bar["high"] >= stop
        hit_target = bar["high"] >= target if side == "buy" else bar["low"] <= target
        if hit_stop:
            return -sl                       # conservative: the stop wins a same-bar touch
        if hit_target:
            return tp
    last = bars[min(entry_index + horizon, len(bars)) - 1]["close"]
    return (last - entry) if side == "buy" else (entry - last)


def _deployed(window, side):
    return signals.evaluate_signal(window, side).clear


def build_trades(bars, guard=None, window=WINDOW, horizon=HORIZON, allow_gaps=False):
    """Every trade `guard(window_bars, side) -> bool` allows (default: the deployed
    signals.evaluate_signal), both sides, simulated."""
    guard = guard or _deployed
    trades = []
    for end in candidate_windows(bars, window, horizon, allow_gaps):
        w = bars[end - window + 1: end + 1]
        sl = signals.select_bracket_points(w[-signals.VOLATILITY_LOOKBACK_BARS:])[0]
        for side in ("buy", "sell"):
            if not guard(w, side):
                continue
            points = simulate_trade(bars, side, end + 1, sl, horizon=horizon)
            trades.append(Trade(end, side, tuple(w), sl, points, points * CONTRACTS * POINT_VALUE))
    return trades


# ---------------------------------------------------------------- reporting

def summarize(dollars):
    """n, win %, net, average, worst trade and max drawdown of a P&L list ($)."""
    dollars = list(dollars)
    if not dollars:
        return {"n": 0, "win_pct": 0.0, "net": 0.0, "avg": 0.0, "worst": 0.0, "max_drawdown": 0.0}
    cumulative = peak = drawdown = 0.0
    for x in dollars:
        cumulative += x
        peak = max(peak, cumulative)
        drawdown = max(drawdown, peak - cumulative)
    return {
        "n": len(dollars),
        "win_pct": 100.0 * sum(1 for x in dollars if x > 0) / len(dollars),
        "net": sum(dollars),
        "avg": sum(dollars) / len(dollars),
        "worst": min(dollars),
        "max_drawdown": drawdown,
    }


def partition(trades, predicate):
    """(removed, kept): the trades the predicate flags and the rest."""
    removed = [t for t in trades if predicate(t)]
    kept = [t for t in trades if not predicate(t)]
    return removed, kept


_GUARDS = {
    "chase": lambda t: signals.check_chase_guard(list(t.window), t.side).flagged,
    "chop": lambda t: signals.check_chop_guard(list(t.window)).flagged,
    "direction": lambda t: signals.check_direction_guard(list(t.window), t.side).flagged,
}


def guard_ablation(bars, window=WINDOW, horizon=HORIZON, allow_gaps=False):
    """For each guard alone: what it removes and keeps out of ALL candidate
    trades; plus the deployed combination."""
    everything = build_trades(bars, guard=lambda w, s: True, window=window, horizon=horizon, allow_gaps=allow_gaps)
    report = {"no guards": summarize(t.pnl_dollars for t in everything)}
    for name, flagged in _GUARDS.items():
        removed, kept = partition(everything, flagged)
        report[name] = {
            "removed": summarize(t.pnl_dollars for t in removed),
            "kept": summarize(t.pnl_dollars for t in kept),
        }
    allowed = [t for t in everything if not any(f(t) for f in _GUARDS.values())]
    report["deployed (all guards)"] = summarize(t.pnl_dollars for t in allowed)
    return report


def _fmt(s):
    return "n=%4d win%%=%3.0f net=$%8.0f avg=$%7.1f worst=$%6.0f maxDD=$%7.0f" % (
        s["n"], s["win_pct"], s["net"], s["avg"], s["worst"], s["max_drawdown"])


def format_report(report):
    lines = ["%-24s %s" % ("no guards (base)", _fmt(report["no guards"]))]
    for name in _GUARDS:
        lines.append("%-24s removed: %s" % (name, _fmt(report[name]["removed"])))
        lines.append("%-24s kept:    %s" % ("", _fmt(report[name]["kept"])))
    lines.append("%-24s %s" % ("deployed (all guards)", _fmt(report["deployed (all guards)"])))
    return "\n".join(lines)


# ---------------------------------------------------------------- CLI

def main(argv=None):
    parser = argparse.ArgumentParser(description="Replay the signal guards on exported 5m candles.")
    parser.add_argument("--candles", help="candles file to replay (default: the history, else the live file)")
    parser.add_argument("--history", default=str(HISTORY), help="history file used by --archive and as the default input")
    parser.add_argument("--archive", action="store_true", help="merge the live candles file into the history and exit")
    parser.add_argument("--allow-gaps", action="store_true", help="also use windows that cross a market closure")
    parser.add_argument("--window", type=int, default=WINDOW)
    parser.add_argument("--horizon", type=int, default=HORIZON)
    args = parser.parse_args(argv)

    if args.archive:
        added = merge_history(LIVE_CANDLES, args.history)
        print("history %s: +%d new bars, %d total" % (args.history, added, len(load_bars(args.history))))
        return 0

    path = args.candles or (args.history if Path(args.history).exists() else str(LIVE_CANDLES))
    bars = load_bars(path)
    print("replaying %s: %d bars, %s -> %s" % (path, len(bars), bars[0]["time"], bars[-1]["time"]))
    print(format_report(guard_ablation(bars, args.window, args.horizon, args.allow_gaps)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
