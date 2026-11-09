"""Unit tests for tradovate_bot.account_state -- the AddOn telemetry reader.

No real NT8/NinjaScript AddOn is touched; these only exercise the JSON file
contract and the fail-closed behavior on every bad-input shape.
"""
import json
from datetime import datetime, timedelta, timezone

from tradovate_bot import account_state

FIXED_NOW = datetime(2026, 11, 9, 14, 32, 30, tzinfo=timezone.utc)


def _good_payload(updated_at="2026-11-09T14:32:05Z"):
    return {
        "updated_at": updated_at,
        "daily_realized_pnl": -50.0,
        "eod_balance_last_close": 25000.0,
        "current_balance": 24950.0,
        "daily_profit_today": 0.0,
        "total_realized_profit": 100.0,
    }


def _write(tmp_path, payload_text, name="telemetry.json"):
    path = tmp_path / name
    path.write_text(payload_text, encoding="utf-8")
    return str(path)


def test_good_file_maps_correctly(tmp_path):
    path = _write(tmp_path, json.dumps(_good_payload()))
    state = account_state.read_account_state(path, now=FIXED_NOW)
    assert state == {
        "daily_realized_pnl": -50.0,
        "eod_balance_last_close": 25000.0,
        "current_balance": 24950.0,
        "daily_profit_today": 0.0,
        "total_realized_profit": 100.0,
    }


def test_missing_file_returns_empty_dict(tmp_path):
    state = account_state.read_account_state(str(tmp_path / "nope.json"), now=FIXED_NOW)
    assert state == {}


def test_malformed_json_returns_empty_dict(tmp_path):
    path = _write(tmp_path, "{not valid json")
    assert account_state.read_account_state(path, now=FIXED_NOW) == {}


def test_missing_required_field_returns_empty_dict(tmp_path):
    payload = _good_payload()
    del payload["daily_realized_pnl"]
    path = _write(tmp_path, json.dumps(payload))
    assert account_state.read_account_state(path, now=FIXED_NOW) == {}


def test_non_numeric_field_returns_empty_dict(tmp_path):
    payload = _good_payload()
    payload["current_balance"] = "not a number"
    path = _write(tmp_path, json.dumps(payload))
    assert account_state.read_account_state(path, now=FIXED_NOW) == {}


def test_stale_updated_at_returns_empty_dict(tmp_path):
    stale_time = (FIXED_NOW - timedelta(seconds=31)).strftime("%Y-%m-%dT%H:%M:%SZ")
    path = _write(tmp_path, json.dumps(_good_payload(updated_at=stale_time)))
    assert account_state.read_account_state(path, now=FIXED_NOW) == {}


def test_updated_at_within_staleness_window_is_accepted(tmp_path):
    fresh_time = (FIXED_NOW - timedelta(seconds=29)).strftime("%Y-%m-%dT%H:%M:%SZ")
    path = _write(tmp_path, json.dumps(_good_payload(updated_at=fresh_time)))
    state = account_state.read_account_state(path, now=FIXED_NOW)
    assert state != {}


def test_missing_updated_at_returns_empty_dict(tmp_path):
    payload = _good_payload()
    del payload["updated_at"]
    path = _write(tmp_path, json.dumps(payload))
    assert account_state.read_account_state(path, now=FIXED_NOW) == {}


def test_non_dict_json_returns_empty_dict(tmp_path):
    path = _write(tmp_path, json.dumps([1, 2, 3]))
    assert account_state.read_account_state(path, now=FIXED_NOW) == {}


def test_custom_max_staleness_seconds(tmp_path):
    time_60s_ago = (FIXED_NOW - timedelta(seconds=60)).strftime("%Y-%m-%dT%H:%M:%SZ")
    path = _write(tmp_path, json.dumps(_good_payload(updated_at=time_60s_ago)))
    assert account_state.read_account_state(path, max_staleness_seconds=30, now=FIXED_NOW) == {}
    assert account_state.read_account_state(path, max_staleness_seconds=120, now=FIXED_NOW) != {}
