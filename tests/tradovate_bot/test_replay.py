"""Tests for tradovate_bot.replay (offline guard/bracket replay harness)."""
import json
from datetime import datetime, timedelta

import pytest

from tradovate_bot import replay

T0 = datetime(2026, 11, 18, 18, 5)


def _bar(i, o, h, l, c, gap_after=None):
    ts = T0 + timedelta(minutes=5 * i)
    return {"time": ts.strftime("%Y-%m-%d %H:%M:%S"), "open": o, "high": h, "low": l, "close": c, "volume": 1}


def _series(n, start=100.0):
    """n contiguous 5m bars drifting up 1 pt per bar."""
    return [_bar(i, start + i, start + i + 2, start + i - 1, start + i + 1) for i in range(n)]


# ---------------------------------------------------------------- simulate_trade

def test_simulate_buy_hits_target():
    bars = _series(3) + [_bar(3, 100, 111, 99, 110)]   # entry at bar 3 open=100, target +10
    assert replay.simulate_trade(bars, "buy", 3, sl=5, tp=10, horizon=4) == 10


def test_simulate_buy_hits_stop():
    bars = _series(3) + [_bar(3, 100, 101, 94, 95)]
    assert replay.simulate_trade(bars, "buy", 3, sl=5, tp=10, horizon=4) == -5


def test_simulate_takes_the_stop_when_both_levels_touch_in_one_bar():
    bars = _series(3) + [_bar(3, 100, 111, 94, 100)]
    assert replay.simulate_trade(bars, "buy", 3, sl=5, tp=10, horizon=4) == -5


def test_simulate_sell_mirrors_buy():
    win = _series(3) + [_bar(3, 100, 101, 89, 90)]
    loss = _series(3) + [_bar(3, 100, 106, 99, 105)]
    assert replay.simulate_trade(win, "sell", 3, sl=5, tp=10, horizon=4) == 10
    assert replay.simulate_trade(loss, "sell", 3, sl=5, tp=10, horizon=4) == -5


def test_simulate_marks_to_the_last_close_when_neither_level_is_hit():
    bars = _series(3) + [_bar(3, 100, 103, 98, 102), _bar(4, 102, 104, 99, 103)]
    assert replay.simulate_trade(bars, "buy", 3, sl=5, tp=10, horizon=2) == pytest.approx(3.0)
    assert replay.simulate_trade(bars, "sell", 3, sl=5, tp=10, horizon=2) == pytest.approx(-3.0)


# ---------------------------------------------------------------- summarize

def test_summarize_reports_counts_net_and_drawdown():
    s = replay.summarize([100.0, -50.0, -80.0, 200.0, -10.0])
    assert s["n"] == 5
    assert s["win_pct"] == pytest.approx(40.0)
    assert s["net"] == pytest.approx(160.0)
    assert s["avg"] == pytest.approx(32.0)
    assert s["worst"] == pytest.approx(-80.0)
    assert s["max_drawdown"] == pytest.approx(130.0)   # peak 100 -> trough -30


def test_summarize_of_nothing_is_all_zero():
    assert replay.summarize([]) == {"n": 0, "win_pct": 0.0, "net": 0.0, "avg": 0.0, "worst": 0.0, "max_drawdown": 0.0}


# ---------------------------------------------------------------- windows / gaps

def test_candidate_windows_skip_a_market_gap_by_default():
    bars = _series(15)
    gap = [dict(b) for b in bars]
    shift = timedelta(minutes=65)                    # the 17:00-18:05 style closure
    for b in gap[8:]:
        b["time"] = (datetime.strptime(b["time"], "%Y-%m-%d %H:%M:%S") + shift).strftime("%Y-%m-%d %H:%M:%S")
    contiguous = list(replay.candidate_windows(bars, window=5, horizon=3))
    with_gap = list(replay.candidate_windows(gap, window=5, horizon=3))
    with_gap_allowed = list(replay.candidate_windows(gap, window=5, horizon=3, allow_gaps=True))
    assert len(with_gap) < len(contiguous)
    assert len(with_gap_allowed) == len(contiguous)


def test_candidate_windows_leave_room_for_the_entry_and_the_horizon():
    bars = _series(12)
    ends = list(replay.candidate_windows(bars, window=5, horizon=3))
    assert ends[0] == 4
    assert ends[-1] + 1 + 3 <= len(bars)


# ---------------------------------------------------------------- build / partition / ablation

def _always(window, side):
    return True


def test_build_trades_uses_the_guard_and_both_sides():
    bars = _series(30)
    both = replay.build_trades(bars, guard=_always, window=10, horizon=4)
    none = replay.build_trades(bars, guard=lambda w, s: False, window=10, horizon=4)
    assert none == []
    assert {t.side for t in both} == {"buy", "sell"}
    assert all(t.pnl_dollars == pytest.approx(t.pnl_points * replay.CONTRACTS * replay.POINT_VALUE) for t in both)


def test_partition_splits_trades_by_predicate():
    trades = replay.build_trades(_series(30), guard=_always, window=10, horizon=4)
    removed, kept = replay.partition(trades, lambda t: t.side == "buy")
    assert len(removed) + len(kept) == len(trades)
    assert all(t.side == "buy" for t in removed) and all(t.side == "sell" for t in kept)


def test_guard_ablation_lists_every_guard_and_the_deployed_combination():
    report = replay.guard_ablation(_series(40), window=10, horizon=4)
    assert set(report) >= {"no guards", "chase", "chop", "direction", "deployed (all guards)"}
    base = report["no guards"]["n"]
    for name in ("chase", "chop", "direction"):
        assert report[name]["removed"]["n"] + report[name]["kept"]["n"] == base


# ---------------------------------------------------------------- history archive

def _write(path, bars, bom=True):
    path.write_text(json.dumps({"symbol": "MNQ", "timeframe": "5m", "bars": bars}), encoding="utf-8-sig" if bom else "utf-8")


def test_load_bars_reads_bom_and_plain_utf8(tmp_path):
    bars = _series(3)
    _write(tmp_path / "a.json", bars, bom=True)
    _write(tmp_path / "b.json", bars, bom=False)
    assert replay.load_bars(tmp_path / "a.json") == bars
    assert replay.load_bars(tmp_path / "b.json") == bars


def test_merge_history_dedupes_sorts_and_is_idempotent(tmp_path):
    src, hist = tmp_path / "src.json", tmp_path / "history.json"
    first, second = _series(5), _series(8)           # overlapping, second extends first
    _write(src, second[3:])                          # bars 3..7 (newer)
    replay.merge_history(_write_and_return(tmp_path / "older.json", first), hist)
    added = replay.merge_history(src, hist)
    again = replay.merge_history(src, hist)
    merged = replay.load_bars(hist)
    assert [b["time"] for b in merged] == [b["time"] for b in second]
    assert added == 3 and again == 0


def _write_and_return(path, bars):
    _write(path, bars)
    return path


def test_merge_history_newer_copy_of_a_bar_wins(tmp_path):
    older, newer, hist = tmp_path / "o.json", tmp_path / "n.json", tmp_path / "h.json"
    bar = _bar(0, 100, 102, 99, 101)
    _write(older, [bar])
    _write(newer, [dict(bar, close=555.0)])
    replay.merge_history(older, hist)
    replay.merge_history(newer, hist)
    assert replay.load_bars(hist)[0]["close"] == 555.0
