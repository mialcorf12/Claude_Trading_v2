"""Tests for tradovate_bot.price_drift (pre-trade price-drift gate)."""
import json
from datetime import datetime

import pytest

from tradovate_bot import price_drift, time_utils

NOW = datetime(2026, 11, 18, 21, 58, 15, tzinfo=time_utils.EASTERN)


def _write_candles(tmp_path, bars, name="MNQ_1m.json", bom=True):
    path = tmp_path / name
    text = json.dumps({"symbol": "MNQ", "timeframe": "1m", "bars": bars})
    path.write_text(text, encoding="utf-8-sig" if bom else "utf-8")
    return str(path)


def _bar(time_str, close):
    return {"time": time_str, "open": close, "high": close, "low": close, "close": close, "volume": 1}


def _order(decision_close=27459.5, stop=33.5, symbol="MNQ"):
    order = {
        "symbol": symbol,
        "side": "buy",
        "bars": [{"open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5},
                 {"open": 27428.25, "high": 27463.0, "low": 27419.0, "close": decision_close}],
    }
    if stop is not None:
        order["stop_loss_points"] = stop
    return order


def test_rejects_the_2026_11_18_scenario(tmp_path):
    # Decision close 27459.5; live 1m close 27438 at 21:57 -> 21.5 pts of drift.
    path = _write_candles(tmp_path, [_bar("2026-11-18 21:57:00", 27438.0)])
    verdict = price_drift.evaluate_entry_drift(_order(), candles_path=path, now=NOW)
    assert verdict.ok is False
    assert verdict.drift == pytest.approx(21.5)
    assert verdict.threshold == pytest.approx(0.25 * 33.5)
    assert any("drift" in reason for reason in verdict.reasons)


def test_allows_small_drift_within_threshold(tmp_path):
    path = _write_candles(tmp_path, [_bar("2026-11-18 21:57:00", 27455.0)])
    verdict = price_drift.evaluate_entry_drift(_order(), candles_path=path, now=NOW)
    assert verdict.ok is True
    assert verdict.reasons == ()
    assert verdict.drift == pytest.approx(4.5)


def test_drift_is_checked_in_both_directions(tmp_path):
    path = _write_candles(tmp_path, [_bar("2026-11-18 21:57:00", 27475.0)])
    verdict = price_drift.evaluate_entry_drift(_order(), candles_path=path, now=NOW)
    assert verdict.ok is False


def test_uses_last_bar_of_the_candles_file(tmp_path):
    bars = [_bar("2026-11-18 21:55:00", 27300.0), _bar("2026-11-18 21:57:00", 27458.0)]
    path = _write_candles(tmp_path, bars)
    verdict = price_drift.evaluate_entry_drift(_order(), candles_path=path, now=NOW)
    assert verdict.ok is True
    assert verdict.live_price == pytest.approx(27458.0)


def test_falls_back_to_default_points_when_no_stop(tmp_path):
    path = _write_candles(tmp_path, [_bar("2026-11-18 21:57:00", 27452.0)])  # 7.5 pts
    assert price_drift.evaluate_entry_drift(_order(stop=None), candles_path=path, now=NOW).ok is True
    path = _write_candles(tmp_path, [_bar("2026-11-18 21:57:00", 27450.0)])  # 9.5 pts
    assert price_drift.evaluate_entry_drift(_order(stop=None), candles_path=path, now=NOW).ok is False


def test_fails_closed_when_candles_file_is_missing(tmp_path):
    verdict = price_drift.evaluate_entry_drift(_order(), candles_path=str(tmp_path / "nope.json"), now=NOW)
    assert verdict.ok is False
    assert verdict.reasons


def test_fails_closed_on_malformed_json(tmp_path):
    path = tmp_path / "MNQ_1m.json"
    path.write_text("{not json", encoding="utf-8")
    assert price_drift.evaluate_entry_drift(_order(), candles_path=str(path), now=NOW).ok is False


def test_fails_closed_when_candles_have_no_bars(tmp_path):
    path = _write_candles(tmp_path, [])
    assert price_drift.evaluate_entry_drift(_order(), candles_path=path, now=NOW).ok is False


def test_fails_closed_on_unparseable_bar_time(tmp_path):
    path = _write_candles(tmp_path, [_bar("yesterday-ish", 27459.0)])
    assert price_drift.evaluate_entry_drift(_order(), candles_path=path, now=NOW).ok is False


def test_fails_closed_when_live_bar_is_stale(tmp_path):
    # 21:58:15 - 21:54:00 = 255 s > 150 s
    path = _write_candles(tmp_path, [_bar("2026-11-18 21:54:00", 27459.0)])
    verdict = price_drift.evaluate_entry_drift(_order(), candles_path=path, now=NOW)
    assert verdict.ok is False
    assert any("stale" in reason for reason in verdict.reasons)


def test_fails_closed_when_decision_price_is_missing(tmp_path):
    path = _write_candles(tmp_path, [_bar("2026-11-18 21:57:00", 27459.0)])
    order = _order()
    order["bars"] = []
    assert price_drift.evaluate_entry_drift(order, candles_path=path, now=NOW).ok is False
    order.pop("bars")
    assert price_drift.evaluate_entry_drift(order, candles_path=path, now=NOW).ok is False


def test_fails_closed_on_non_finite_prices(tmp_path):
    path = _write_candles(tmp_path, [_bar("2026-11-18 21:57:00", 27459.0)])
    order = _order(decision_close=float("nan"))
    assert price_drift.evaluate_entry_drift(order, candles_path=path, now=NOW).ok is False


def test_reads_bom_and_plain_utf8(tmp_path):
    path = _write_candles(tmp_path, [_bar("2026-11-18 21:57:00", 27459.0)], bom=False)
    assert price_drift.evaluate_entry_drift(_order(), candles_path=path, now=NOW).ok is True


def test_default_path_uses_symbol_specific_candles_file(tmp_path, monkeypatch):
    _write_candles(tmp_path, [_bar("2026-11-18 21:57:00", 27459.0)], name="NQ_1m.json")
    monkeypatch.setattr(price_drift, "DEFAULT_CANDLES_DIR", tmp_path)
    verdict = price_drift.evaluate_entry_drift(_order(symbol="NQ"), now=NOW)
    assert verdict.ok is True
