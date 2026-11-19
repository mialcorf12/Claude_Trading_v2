"""Tests for tradovate_bot.local_market_data_mcp (the get_local_candles MCP tool)."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tradovate_bot import local_market_data_mcp as mcp_module
from tradovate_bot import time_utils

NOW_ET = datetime(2026, 11, 19, 0, 45, 15, tzinfo=time_utils.EASTERN)
NOW_UTC = NOW_ET.astimezone(timezone.utc)


def _bars(last_time="2026-11-19 00:45:00", n=3, step=5):
    last = datetime.strptime(last_time, "%Y-%m-%d %H:%M:%S")
    out = []
    for i in range(n):
        ts = last - timedelta(minutes=step * (n - 1 - i))
        out.append({"time": ts.strftime("%Y-%m-%d %H:%M:%S"), "open": 100.0 + i, "high": 102.0 + i,
                    "low": 99.0 + i, "close": 101.0 + i, "volume": 10 + i})
    return out


def _write(tmp_path, name="MNQ_5m.json", bars=None, updated="2026-11-19T05:45:00.1293119Z", bom=True):
    payload = {"symbol": name.split("_")[0], "timeframe": name.split("_")[1][:-5],
               "updated_utc": updated, "bars": bars if bars is not None else _bars()}
    (tmp_path / name).write_text(json.dumps(payload), encoding="utf-8-sig" if bom else "utf-8")


@pytest.fixture(autouse=True)
def fixed_clock(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_module, "DATA_DIR", tmp_path)
    monkeypatch.setattr(mcp_module, "now_et", lambda: NOW_ET)
    monkeypatch.setattr(mcp_module, "_now_utc", lambda: NOW_UTC)
    yield tmp_path


def _call(**kwargs):
    return json.loads(mcp_module.get_local_candles(**kwargs))


# ---------------------------------------------------------------- timeframe / symbol

@pytest.mark.parametrize("raw,expected", [("5m", "5m"), ("5Minute", "5m"), ("5MIN", "5m"), (" 5 ", "5m"),
                                          ("1m", "1m"), ("1minute", "1m"), ("1", "1m")])
def test_timeframe_variants_are_normalized(raw, expected):
    assert mcp_module._normalize_timeframe(raw) == expected


def test_unknown_timeframe_and_path_like_symbol_return_an_error_json(tmp_path):
    _write(tmp_path)
    assert "error" in _call(timeframe="15m")
    for bad in ("..\\..\\secret", "MNQ/../x", "", "M N Q", "TOOLONGSYMBOL1"):
        assert "error" in _call(symbol=bad)


# ---------------------------------------------------------------- reading

def test_reads_a_bom_file_and_returns_the_last_bars_with_freshness(tmp_path):
    _write(tmp_path, bars=_bars(n=10))
    result = _call(limit=3)
    assert result["count_returned"] == 3 and len(result["bars"]) == 3
    assert result["bars"][-1]["time"] == "2026-11-19 00:45:00"
    assert result["freshness"]["stale"] is False
    assert result["freshness"]["last_bar_age_seconds"] == 15


def test_reads_plain_utf8_too(tmp_path):
    _write(tmp_path, bom=False)
    assert "error" not in _call()


def test_limit_zero_or_negative_still_returns_at_least_one_bar_not_all(tmp_path):
    _write(tmp_path, bars=_bars(n=10))
    assert _call(limit=0)["count_returned"] == 1
    assert _call(limit=-5)["count_returned"] == 1


def test_falls_back_between_mnq_and_nq_when_only_one_file_exists(tmp_path):
    _write(tmp_path, name="NQ_5m.json")
    result = _call(symbol="MNQ")
    assert "error" not in result and result["symbol"] == "NQ"


def test_missing_data_returns_an_error_listing_the_available_files(tmp_path):
    _write(tmp_path, name="MNQ_1m.json")
    result = _call(symbol="MNQ", timeframe="5m")
    assert "error" in result and "MNQ_1m.json" in result["archivos_disponibles"]


def test_a_corrupt_file_returns_an_error_json(tmp_path):
    (tmp_path / "MNQ_5m.json").write_text("{not json", encoding="utf-8")
    assert "error" in _call()


# ---------------------------------------------------------------- freshness

def test_a_stale_last_bar_is_flagged(tmp_path):
    _write(tmp_path, bars=_bars(last_time="2026-11-19 00:30:00"))   # 15 min old
    assert _call()["freshness"]["stale"] is True


def test_the_one_minute_limit_is_tighter_than_the_five_minute_one(tmp_path):
    stamp = "2026-11-19T05:42:00.0000000Z"
    _write(tmp_path, name="MNQ_1m.json", bars=_bars(last_time="2026-11-19 00:42:00", step=1), updated=stamp)
    _write(tmp_path, name="MNQ_5m.json", bars=_bars(last_time="2026-11-19 00:42:00"), updated=stamp)
    assert _call(timeframe="1m")["freshness"]["stale"] is True     # 3 min old vs a 2 min limit
    assert _call(timeframe="5m")["freshness"]["stale"] is False    # 3 min old vs a 6 min limit


def test_an_old_export_file_is_flagged_even_with_a_recent_bar(tmp_path):
    _write(tmp_path, updated="2026-11-19T05:20:00.0000000Z")        # file 25 min old
    assert _call()["freshness"]["stale"] is True


def test_unparseable_timestamps_fail_closed(tmp_path):
    _write(tmp_path, updated="not a date")
    freshness = _call()["freshness"]
    assert freshness["stale"] is True and "freshness_error" in freshness
    _write(tmp_path, bars=_bars()[:-1] + [{"time": "garbage", "close": 1}])
    assert _call()["freshness"]["stale"] is True


@pytest.mark.parametrize("stamp", [
    "2026-11-19T05:45:00.1293119Z",        # NT8 DateTime.UtcNow:O (7 digits)
    "2026-11-19T05:45:00.129311Z",         # 6 digits
    "2026-11-19T05:45:00Z",                # no fractional seconds (the old code broke here)
    "2026-11-19T05:45:00.5+00:00",         # explicit offset
    "2026-11-19T05:45:00",                 # naive: treated as UTC
])
def test_updated_utc_variants_parse_to_the_same_instant(stamp):
    parsed = mcp_module._parse_utc(stamp)
    assert parsed.tzinfo is not None
    assert abs((parsed - datetime(2026, 11, 19, 5, 45, 0, tzinfo=timezone.utc)).total_seconds()) < 1


def test_updated_utc_rejects_garbage():
    with pytest.raises(ValueError):
        mcp_module._parse_utc("tomorrow")


# ---------------------------------------------------------------- configuration

def test_default_data_dir_is_relative_to_the_repo_not_absolute():
    expected = Path(mcp_module.__file__).resolve().parents[1] / "candles"
    assert mcp_module._default_data_dir(env={}) == expected


def test_candles_dir_env_overrides_the_default(tmp_path):
    assert mcp_module._default_data_dir(env={"CANDLES_DIR": str(tmp_path)}) == tmp_path
