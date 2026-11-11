"""Unit tests for tradovate_bot.executor against the NT8 ATI (OIF) transport.

These exercise the executor's order normalization, risk-gating, OIF command
building, and logging logic. No real NT8 instance is touched: the OIF
writer's filesystem call is monkeypatched to capture its arguments, and
account state comes from a real temp JSON fixture file exercised through
the actual account_state.read_account_state function.
"""
import json
import os
from datetime import datetime
from unittest import mock

import pytest

from tradovate_bot import config as config_module
from tradovate_bot import executor, position_state, time_utils

# Wednesday 2026-11-11, 10:00 ET -- unambiguously within trading hours, so
# executor tests never depend on the real wall-clock time when they run.
WITHIN_HOURS = datetime(2026, 11, 11, 10, 0, tzinfo=time_utils.EASTERN)

# ---------------------------------------------------------------- signals.py bars fixtures
# (odd/tasks/tradovate-architecture-scaling.md task 4: process_order now
# runs signals.evaluate_signal(order["bars"], order["side"]) before the
# one-position-at-a-time check, so every pre-existing test whose raw_order
# needs to reach that check or anything after it needs a `bars` list that
# clears both of signals.py's guards. Adapted from
# tests/tradovate_bot/test_signals.py's own NEITHER_FIRES_BARS/
# CYCLE_18_CHASE_BARS fixtures: one down-close pullback (clears the chase
# guard for either "buy" or "sell") with highs/lows climbing monotonically
# so no interior bar is a swing high/low (clears the chop guard too).
CLEAN_SIGNAL_BARS = [
    {"open": 99.0, "high": 101.0, "low": 95.0, "close": 100.0},
    {"open": 100.0, "high": 110.0, "low": 100.0, "close": 105.0},
    {"open": 105.0, "high": 115.0, "low": 105.0, "close": 103.0},
    {"open": 103.0, "high": 120.0, "low": 108.0, "close": 108.0},
    {"open": 108.0, "high": 125.0, "low": 111.0, "close": 112.0},
    {"open": 112.0, "high": 130.0, "low": 114.0, "close": 115.0},
]

# Cycle 18's exact chase scenario (see tests/tradovate_bot/test_signals.py
# and tradovate_bot/signals.py's module docstring): four consecutive higher
# closes off the 11:55 low with zero retracement -- trips the chase guard
# for a "buy".
CHASE_FLAGGED_BARS = [
    {"open": 27625.00, "high": 27635.00, "low": 27620.00, "close": 27631.75},
    {"open": 27632.00, "high": 27660.00, "low": 27630.00, "close": 27657.25},
    {"open": 27657.00, "high": 27662.00, "low": 27655.00, "close": 27658.75},
    {"open": 27659.00, "high": 27682.00, "low": 27658.00, "close": 27678.50},
    {"open": 27679.00, "high": 27695.00, "low": 27677.00, "close": 27692.00},
]


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(executor, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(executor, "DECISIONS_LOG", str(tmp_path / "decisions.log.jsonl"))
    monkeypatch.setattr(executor, "FILLS_LOG", str(tmp_path / "fills.jsonl"))
    monkeypatch.setattr(executor, "CYCLE_LOG", str(tmp_path / "cycles.log.jsonl"))
    monkeypatch.setattr(executor, "NT8_INCOMING_DIR", str(tmp_path / "incoming"))
    monkeypatch.setattr(executor, "NT8_OUTGOING_DIR", str(tmp_path / "outgoing"))
    monkeypatch.setattr(executor, "NT8_ACCOUNT_ID", "SIM101")
    monkeypatch.setattr(executor, "NT8_INSTRUMENT", "MNQ 12-26")
    monkeypatch.setattr(time_utils, "now_et", lambda: WITHIN_HOURS)
    # Default to Flat so existing tests that don't care about the
    # one-position-at-a-time safeguard aren't affected by it -- tests that
    # exercise that safeguard override this explicitly.
    monkeypatch.setattr(executor, "current_position", lambda: position_state.PositionState("Flat", 0))
    yield tmp_path


def _read_jsonl(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _write_telemetry(tmp_path, monkeypatch, **overrides):
    payload = {
        "updated_at": time_utils.now_et_iso(),
        "daily_realized_pnl": 0.0,
        "eod_balance_last_close": 25000.0,
        "current_balance": 25000.0,
        "daily_profit_today": 0.0,
        "total_realized_profit": 0.0,
    }
    payload.update(overrides)
    path = tmp_path / "telemetry.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(executor, "ACCOUNT_TELEMETRY_JSON_PATH", str(path))
    return str(path)


def test_normalize_order_maps_trader_broker_shape():
    raw = {"symbol": "mnq", "side": "Buy", "qty": "2", "type": "market",
           "stop_loss_points": 33.25, "take_profit_points": 16.75}
    order = executor.normalize_order(raw)
    assert order["symbol"] == "MNQ"
    assert order["side"] == "buy"
    assert order["qty"] == 2.0
    assert order["stop_loss_points"] == 33.25
    assert order["take_profit_points"] == 16.75


def test_normalize_order_rejects_missing_fields():
    assert executor.normalize_order({"symbol": "MNQ"}) is None
    assert executor.normalize_order(None) is None
    assert executor.normalize_order("not a dict") is None


def test_build_place_command_maps_order_onto_oif_grammar():
    order = executor.normalize_order({"symbol": "MNQ", "side": "buy", "qty": 2, "type": "market"})
    command = executor.build_place_command(order, account="SIM101")
    assert command == "PLACE;SIM101;MNQ 12-26;BUY;2;MARKET;;;DAY;;;;"


def test_process_order_rejects_when_normalization_fails(isolated_state, monkeypatch):
    _write_telemetry(isolated_state, monkeypatch)
    write_command = mock.Mock()
    monkeypatch.setattr(executor.oif_writer, "write_command", write_command)

    decision = executor.process_order({"symbol": "MNQ"})

    assert decision.allow is False
    write_command.assert_not_called()
    rows = _read_jsonl(executor.DECISIONS_LOG)
    assert len(rows) == 1
    assert rows[0]["allow"] is False


def test_process_order_rejects_when_risk_guard_fails(isolated_state, monkeypatch):
    _write_telemetry(isolated_state, monkeypatch, daily_realized_pnl=-650.0)  # breaches $600 daily loss limit
    write_command = mock.Mock()
    monkeypatch.setattr(executor.oif_writer, "write_command", write_command)
    raw_order = {"symbol": "MNQ", "side": "buy", "qty": 1, "type": "market"}

    decision = executor.process_order(raw_order)

    assert decision.allow is False
    write_command.assert_not_called()
    rows = _read_jsonl(executor.DECISIONS_LOG)
    assert len(rows) == 1
    assert "daily loss" in rows[0]["reason"].lower()


def test_process_order_rejects_when_telemetry_is_missing(isolated_state, monkeypatch):
    monkeypatch.setattr(executor, "ACCOUNT_TELEMETRY_JSON_PATH", str(isolated_state / "does_not_exist.json"))
    write_command = mock.Mock()
    monkeypatch.setattr(executor.oif_writer, "write_command", write_command)
    raw_order = {"symbol": "MNQ", "side": "buy", "qty": 1, "type": "market"}

    decision = executor.process_order(raw_order)

    assert decision.allow is False
    write_command.assert_not_called()


def test_process_order_places_order_when_all_checks_pass(isolated_state, monkeypatch):
    _write_telemetry(isolated_state, monkeypatch)
    write_command = mock.Mock(return_value=str(isolated_state / "incoming" / "oif_fake.txt"))
    monkeypatch.setattr(executor.oif_writer, "write_command", write_command)
    raw_order = {"symbol": "MNQ", "side": "buy", "qty": 2, "type": "market", "bars": CLEAN_SIGNAL_BARS}

    decision = executor.process_order(raw_order)

    assert decision.allow is True
    write_command.assert_called_once()
    incoming_dir_arg, command_arg = write_command.call_args[0]
    assert incoming_dir_arg == executor.NT8_INCOMING_DIR
    assert command_arg == "PLACE;SIM101;MNQ 12-26;BUY;2;MARKET;;;DAY;;;;"

    decisions = _read_jsonl(executor.DECISIONS_LOG)
    fills = _read_jsonl(executor.FILLS_LOG)
    assert decisions[0]["allow"] is True
    assert fills[0]["status"] == "sent"
    assert fills[0]["command"] == command_arg


# ---------------------------------------------------------------- signals.py guard wiring
# (odd/tasks/tradovate-architecture-scaling.md task 4)

def test_process_order_refuses_entry_with_no_bars(isolated_state, monkeypatch):
    _write_telemetry(isolated_state, monkeypatch)
    write_command = mock.Mock()
    monkeypatch.setattr(executor.oif_writer, "write_command", write_command)
    raw_order = {"symbol": "MNQ", "side": "buy", "qty": 1, "type": "market"}  # no "bars" key at all

    decision = executor.process_order(raw_order)

    assert decision.allow is False
    assert "signal" in decision.reason.lower() or "bars" in decision.reason.lower()
    write_command.assert_not_called()
    rows = _read_jsonl(executor.DECISIONS_LOG)
    assert rows[-1]["allow"] is False


def test_process_order_refuses_entry_when_chase_guard_flags_bars(isolated_state, monkeypatch):
    _write_telemetry(isolated_state, monkeypatch)
    write_command = mock.Mock()
    monkeypatch.setattr(executor.oif_writer, "write_command", write_command)
    raw_order = {"symbol": "MNQ", "side": "buy", "qty": 1, "type": "market", "bars": CHASE_FLAGGED_BARS}

    decision = executor.process_order(raw_order)

    assert decision.allow is False
    assert "signal" in decision.reason.lower()
    write_command.assert_not_called()


def test_process_order_proceeds_when_bars_pass_signal_guard(isolated_state, monkeypatch):
    _write_telemetry(isolated_state, monkeypatch)
    write_command = mock.Mock(return_value=str(isolated_state / "incoming" / "oif_fake.txt"))
    monkeypatch.setattr(executor.oif_writer, "write_command", write_command)
    raw_order = {"symbol": "MNQ", "side": "buy", "qty": 2, "type": "market", "bars": CLEAN_SIGNAL_BARS}

    decision = executor.process_order(raw_order)

    assert decision.allow is True  # reaches the existing one-position-at-a-time/placement logic unchanged
    write_command.assert_called_once()


def test_process_order_rejects_outside_trading_hours(isolated_state, monkeypatch):
    monkeypatch.setattr(time_utils, "now_et", lambda: datetime(2026, 11, 9, 17, 0, tzinfo=time_utils.EASTERN))
    _write_telemetry(isolated_state, monkeypatch)
    write_command = mock.Mock()
    monkeypatch.setattr(executor.oif_writer, "write_command", write_command)
    raw_order = {"symbol": "MNQ", "side": "buy", "qty": 1, "type": "market"}

    decision = executor.process_order(raw_order)

    assert decision.allow is False
    assert "trading hours" in decision.reason.lower() or "closed" in decision.reason.lower()
    write_command.assert_not_called()


def test_process_order_rejects_oversized_mnq_position(isolated_state, monkeypatch):
    _write_telemetry(isolated_state, monkeypatch)
    write_command = mock.Mock()
    monkeypatch.setattr(executor.oif_writer, "write_command", write_command)
    raw_order = {"symbol": "MNQ", "side": "buy", "qty": 5, "type": "market"}

    decision = executor.process_order(raw_order)

    assert decision.allow is False
    write_command.assert_not_called()


def test_fetch_account_state_returns_empty_dict_when_telemetry_unreadable(isolated_state, monkeypatch):
    monkeypatch.setattr(executor, "ACCOUNT_TELEMETRY_JSON_PATH", str(isolated_state / "does_not_exist.json"))
    assert executor.fetch_account_state() == {}


def test_fetch_account_state_returns_empty_dict_when_unconfigured(monkeypatch):
    monkeypatch.setattr(executor, "ACCOUNT_TELEMETRY_JSON_PATH", "")
    assert executor.fetch_account_state() == {}


def test_load_orders_handles_missing_file(tmp_path):
    assert executor.load_orders(str(tmp_path / "nope.json")) == []


def test_load_orders_handles_bare_list_and_wrapped_dict(tmp_path):
    list_path = tmp_path / "list.json"
    list_path.write_text(json.dumps([{"symbol": "MNQ"}]))
    assert executor.load_orders(str(list_path)) == [{"symbol": "MNQ"}]

    dict_path = tmp_path / "wrapped.json"
    dict_path.write_text(json.dumps({"orders": [{"symbol": "MNQ"}]}))
    assert executor.load_orders(str(dict_path)) == [{"symbol": "MNQ"}]


def test_run_once_processes_every_order(isolated_state, monkeypatch, tmp_path):
    _write_telemetry(isolated_state, monkeypatch)
    write_command = mock.Mock(return_value=str(isolated_state / "incoming" / "oif_fake.txt"))
    monkeypatch.setattr(executor.oif_writer, "write_command", write_command)
    orders_path = tmp_path / "orders.json"
    orders_path.write_text(json.dumps({"orders": [
        {"symbol": "MNQ", "side": "buy", "qty": 1, "type": "market", "bars": CLEAN_SIGNAL_BARS},
        {"symbol": "MNQ", "side": "sell", "qty": 1, "type": "market", "bars": CLEAN_SIGNAL_BARS},
    ]}))

    decisions = executor.run_once(orders_path=str(orders_path))

    assert len(decisions) == 2
    assert all(d.allow for d in decisions)
    assert write_command.call_count == 2


def test_poll_outgoing_once_logs_fills_and_advances_seen_files(isolated_state, monkeypatch):
    outgoing = isolated_state / "outgoing"
    outgoing.mkdir()
    (outgoing / "SIM101_MNQ_ord.txt").write_text("ORD1;FILLED;2;20100.25\n", encoding="utf-8")

    seen = executor.poll_outgoing_once(set())

    fills = _read_jsonl(executor.FILLS_LOG)
    assert len(fills) == 1
    assert fills[0]["source"] == "nt8_outgoing"
    assert "SIM101_MNQ_ord.txt" in seen

    seen_again = executor.poll_outgoing_once(seen)
    fills_again = _read_jsonl(executor.FILLS_LOG)
    assert len(fills_again) == 1  # no re-logging of the same file


def test_build_bracket_exit_commands_returns_empty_when_no_exit_prices():
    order = executor.normalize_order({"symbol": "MNQ", "side": "sell", "qty": 3, "type": "market"})
    assert executor.build_bracket_exit_commands(order, None, None, account="SIM101") == []


def test_build_bracket_exit_commands_builds_stop_and_target_with_shared_oco():
    order = executor.normalize_order({"symbol": "MNQ", "side": "sell", "qty": 3, "type": "market"})
    commands = executor.build_bracket_exit_commands(order, 27990.0, 27850.0, account="SIM101")

    assert len(commands) == 2
    stop_cmd, target_cmd = commands
    stop_fields = stop_cmd.split(";")
    target_fields = target_cmd.split(";")
    assert stop_fields[0:6] == ["PLACE", "SIM101", "MNQ 12-26", "BUY", "3", "STOPMARKET"]
    assert stop_fields[7] == "27990.0"  # stop price field
    assert target_fields[0:6] == ["PLACE", "SIM101", "MNQ 12-26", "BUY", "3", "LIMIT"]
    assert target_fields[6] == "27850.0"  # limit price field
    assert stop_fields[9] == target_fields[9]  # shared OCO id
    assert stop_fields[9] != ""


def test_build_bracket_exit_commands_builds_only_stop_when_target_missing():
    order = executor.normalize_order({"symbol": "MNQ", "side": "buy", "qty": 2, "type": "market"})
    commands = executor.build_bracket_exit_commands(order, 27500.0, None, account="SIM101")

    assert len(commands) == 1
    fields = commands[0].split(";")
    assert fields[0:6] == ["PLACE", "SIM101", "MNQ 12-26", "SELL", "2", "STOPMARKET"]
    assert fields[9] == ""  # no OCO when there's only one exit leg


# ---------------------------------------------------------------- fill-driven bracket pricing

def test_compute_bracket_prices_from_fill_for_buy():
    order = executor.normalize_order({
        "symbol": "MNQ", "side": "buy", "qty": 3, "type": "market",
        "stop_loss_points": 33.25, "take_profit_points": 16.75,
    })
    stop_price, target_price = executor.compute_bracket_prices_from_fill(order, fill_price=27800.0)
    assert stop_price == 27766.75  # below fill, protects a long
    assert target_price == 27816.75  # above fill


def test_compute_bracket_prices_from_fill_for_sell():
    order = executor.normalize_order({
        "symbol": "MNQ", "side": "sell", "qty": 3, "type": "market",
        "stop_loss_points": 33.25, "take_profit_points": 16.75,
    })
    stop_price, target_price = executor.compute_bracket_prices_from_fill(order, fill_price=27800.0)
    assert stop_price == 27833.25  # above fill, protects a short
    assert target_price == 27783.25  # below fill


def test_compute_bracket_prices_from_fill_rounds_to_nearest_quarter_tick():
    order = executor.normalize_order({
        "symbol": "MNQ", "side": "buy", "qty": 3, "type": "market",
        "stop_loss_points": 33.25, "take_profit_points": 16.75,
    })
    # a multi-lot average fill price is rarely on the 0.25 grid
    stop_price, target_price = executor.compute_bracket_prices_from_fill(order, fill_price=27977.916666666668)
    assert stop_price == 27944.75
    assert target_price == 27994.75


def test_compute_bracket_prices_from_fill_returns_none_when_points_not_requested():
    order = executor.normalize_order({"symbol": "MNQ", "side": "buy", "qty": 3, "type": "market"})
    stop_price, target_price = executor.compute_bracket_prices_from_fill(order, fill_price=27800.0)
    assert stop_price is None
    assert target_price is None


# ---------------------------------------------------------------- waiting for the real fill

def test_wait_for_entry_fill_returns_filled_price_from_outgoing_record(isolated_state, monkeypatch):
    monkeypatch.setattr(executor.time, "sleep", lambda s: None)
    responses = iter([
        ([], {"a"}),
        ([{"raw_fields": ["FILLED", "3", "27977.9166666667"]}], {"a", "b"}),
    ])
    monkeypatch.setattr(executor.oif_writer, "tail_outgoing", lambda d, seen: next(responses))

    result = executor.wait_for_entry_fill(seen_files=set(), timeout_seconds=1.0, poll_interval_seconds=0)

    assert result.status == "filled"
    assert result.price == 27977.9166666667


def test_wait_for_entry_fill_returns_rejected_when_nt8_rejects(isolated_state, monkeypatch):
    monkeypatch.setattr(executor.time, "sleep", lambda s: None)
    monkeypatch.setattr(
        executor.oif_writer, "tail_outgoing",
        lambda d, seen: ([{"raw_fields": ["REJECTED", "0", "0"]}], {"a"}),
    )

    result = executor.wait_for_entry_fill(seen_files=set(), timeout_seconds=1.0, poll_interval_seconds=0)

    assert result.status == "rejected"
    assert result.price is None


def test_wait_for_entry_fill_returns_timeout_when_no_confirmation_arrives(isolated_state, monkeypatch):
    monkeypatch.setattr(executor.time, "sleep", lambda s: None)
    monkeypatch.setattr(executor.oif_writer, "tail_outgoing", lambda d, seen: ([], seen))

    result = executor.wait_for_entry_fill(seen_files=set(), timeout_seconds=0.05, poll_interval_seconds=0)

    assert result.status == "timeout"
    assert result.price is None


def test_wait_for_entry_fill_rereads_same_filename_when_nt8_rewrites_it_in_place(isolated_state, monkeypatch):
    # Regression for the 2026-11-10 incident: NT8 rewrites the SAME
    # outgoing filename in place as an order's status progresses
    # (SUBMITTED -> FILLED) rather than writing a new file per status.
    # This exercises the REAL oif_writer.tail_outgoing (not mocked) so
    # it actually reproduces the filename-permanently-excluded bug.
    outgoing = isolated_state / "outgoing"
    outgoing.mkdir()
    fill_file = outgoing / "Sim101_order123.txt"
    fill_file.write_text("SUBMITTED;0;0", encoding="utf-8")

    def _rewrite_to_filled_between_polls(seconds):
        fill_file.write_text("FILLED;3;27700.0", encoding="utf-8")

    monkeypatch.setattr(executor.time, "sleep", _rewrite_to_filled_between_polls)

    result = executor.wait_for_entry_fill(seen_files=set(), timeout_seconds=5.0, poll_interval_seconds=0)

    assert result.status == "filled"
    assert result.price == 27700.0


# ---------------------------------------------------------------- process_order, fill-driven bracket

def test_process_order_waits_for_fill_then_places_bracket_from_real_fill_price(isolated_state, monkeypatch):
    _write_telemetry(isolated_state, monkeypatch)
    write_command = mock.Mock(return_value=str(isolated_state / "incoming" / "oif_fake.txt"))
    monkeypatch.setattr(executor.oif_writer, "write_command", write_command)
    monkeypatch.setattr(
        executor, "wait_for_entry_fill",
        lambda seen_files, **kw: executor.FillResult("filled", 27800.0),
    )
    raw_order = {
        "symbol": "MNQ", "side": "sell", "qty": 3, "type": "market",
        "stop_loss_points": 33.25, "take_profit_points": 16.75,
        "bars": CLEAN_SIGNAL_BARS,
    }

    decision = executor.process_order(raw_order)

    assert decision.allow is True
    assert write_command.call_count == 3  # entry + stop + target
    sent_commands = [call.args[1] for call in write_command.call_args_list]
    assert sent_commands[0].split(";")[0:6] == ["PLACE", "SIM101", "MNQ 12-26", "SELL", "3", "MARKET"]
    stop_fields = sent_commands[1].split(";")
    target_fields = sent_commands[2].split(";")
    assert stop_fields[0:6] == ["PLACE", "SIM101", "MNQ 12-26", "BUY", "3", "STOPMARKET"]
    assert stop_fields[7] == "27833.25"  # fill (27800) + 33.25, above fill for a short
    assert target_fields[0:6] == ["PLACE", "SIM101", "MNQ 12-26", "BUY", "3", "LIMIT"]
    assert target_fields[6] == "27783.25"  # fill (27800) - 16.75, below fill for a short


def test_process_order_flattens_defensively_on_fill_confirmation_timeout(isolated_state, monkeypatch):
    _write_telemetry(isolated_state, monkeypatch)
    write_command = mock.Mock(return_value=str(isolated_state / "incoming" / "oif_fake.txt"))
    monkeypatch.setattr(executor.oif_writer, "write_command", write_command)
    monkeypatch.setattr(
        executor, "wait_for_entry_fill",
        lambda seen_files, **kw: executor.FillResult("timeout", None),
    )
    raw_order = {
        "symbol": "MNQ", "side": "buy", "qty": 3, "type": "market",
        "stop_loss_points": 33.25, "take_profit_points": 16.75,
        "bars": CLEAN_SIGNAL_BARS,
    }

    decision = executor.process_order(raw_order)

    assert decision.allow is True  # risk_guard passed; the flatten is a post-fill safety action
    assert write_command.call_count == 2  # entry, then a defensive CLOSEPOSITION
    sent_commands = [call.args[1] for call in write_command.call_args_list]
    assert sent_commands[1].split(";")[0:3] == ["CLOSEPOSITION", "SIM101", "MNQ 12-26"]
    fills = _read_jsonl(executor.FILLS_LOG)
    assert any(f.get("status") == "defensive_flatten_fill_confirmation_timeout" for f in fills)


def test_process_order_skips_bracket_when_entry_rejected_by_nt8(isolated_state, monkeypatch):
    _write_telemetry(isolated_state, monkeypatch)
    write_command = mock.Mock(return_value=str(isolated_state / "incoming" / "oif_fake.txt"))
    monkeypatch.setattr(executor.oif_writer, "write_command", write_command)
    monkeypatch.setattr(
        executor, "wait_for_entry_fill",
        lambda seen_files, **kw: executor.FillResult("rejected", None),
    )
    raw_order = {
        "symbol": "MNQ", "side": "buy", "qty": 3, "type": "market",
        "stop_loss_points": 33.25, "take_profit_points": 16.75,
        "bars": CLEAN_SIGNAL_BARS,
    }

    decision = executor.process_order(raw_order)

    assert decision.allow is True
    assert write_command.call_count == 1  # entry only, no bracket, no flatten
    fills = _read_jsonl(executor.FILLS_LOG)
    assert any(f.get("status") == "entry_rejected_by_nt8" for f in fills)


def test_process_order_refuses_bracket_when_outgoing_dir_not_configured(isolated_state, monkeypatch):
    _write_telemetry(isolated_state, monkeypatch)
    monkeypatch.setattr(executor, "NT8_OUTGOING_DIR", "")
    write_command = mock.Mock(return_value=str(isolated_state / "incoming" / "oif_fake.txt"))
    monkeypatch.setattr(executor.oif_writer, "write_command", write_command)
    raw_order = {
        "symbol": "MNQ", "side": "buy", "qty": 3, "type": "market",
        "stop_loss_points": 33.25, "take_profit_points": 16.75,
        "bars": CLEAN_SIGNAL_BARS,
    }

    decision = executor.process_order(raw_order)

    assert decision.allow is False
    write_command.assert_not_called()


# ---------------------------------------------------------------- cycle log

def test_log_cycle_appends_record_with_timestamp(isolated_state):
    executor.log_cycle({"position": "flat", "signal": "skip", "signal_reason": "chop"})

    rows = _read_jsonl(executor.CYCLE_LOG)
    assert len(rows) == 1
    assert rows[0]["position"] == "flat"
    assert rows[0]["signal"] == "skip"
    assert rows[0]["signal_reason"] == "chop"
    assert "at" in rows[0]


def test_log_cycle_appends_one_line_per_call(isolated_state):
    executor.log_cycle({"position": "flat", "signal": "skip"})
    executor.log_cycle({"position": "flat", "signal": "buy", "outcome": "filled", "fill_price": 27784.0})

    rows = _read_jsonl(executor.CYCLE_LOG)
    assert len(rows) == 2
    assert rows[1]["signal"] == "buy"
    assert rows[1]["fill_price"] == 27784.0


# ---------------------------------------------------------------- one-position-at-a-time

def test_process_order_rejects_when_a_long_position_is_already_open(isolated_state, monkeypatch):
    _write_telemetry(isolated_state, monkeypatch)
    write_command = mock.Mock()
    monkeypatch.setattr(executor.oif_writer, "write_command", write_command)
    monkeypatch.setattr(executor, "current_position", lambda: position_state.PositionState("Long", 2))
    raw_order = {"symbol": "MNQ", "side": "buy", "qty": 1, "type": "market", "bars": CLEAN_SIGNAL_BARS}

    decision = executor.process_order(raw_order)

    assert decision.allow is False
    assert "position" in decision.reason.lower()
    write_command.assert_not_called()
    rows = _read_jsonl(executor.DECISIONS_LOG)
    assert rows[-1]["allow"] is False


def test_process_order_rejects_when_a_short_position_is_already_open(isolated_state, monkeypatch):
    _write_telemetry(isolated_state, monkeypatch)
    write_command = mock.Mock()
    monkeypatch.setattr(executor.oif_writer, "write_command", write_command)
    monkeypatch.setattr(executor, "current_position", lambda: position_state.PositionState("Short", 1))
    raw_order = {"symbol": "MNQ", "side": "buy", "qty": 1, "type": "market", "bars": CLEAN_SIGNAL_BARS}

    decision = executor.process_order(raw_order)

    assert decision.allow is False
    write_command.assert_not_called()


def test_process_order_rejects_when_position_is_unknown(isolated_state, monkeypatch):
    _write_telemetry(isolated_state, monkeypatch)
    write_command = mock.Mock()
    monkeypatch.setattr(executor.oif_writer, "write_command", write_command)
    monkeypatch.setattr(executor, "current_position", lambda: position_state.PositionState("unknown", 0))
    raw_order = {"symbol": "MNQ", "side": "buy", "qty": 1, "type": "market", "bars": CLEAN_SIGNAL_BARS}

    decision = executor.process_order(raw_order)

    assert decision.allow is False
    write_command.assert_not_called()


def test_process_order_allows_when_position_is_flat(isolated_state, monkeypatch):
    _write_telemetry(isolated_state, monkeypatch)
    write_command = mock.Mock(return_value=str(isolated_state / "incoming" / "oif_fake.txt"))
    monkeypatch.setattr(executor.oif_writer, "write_command", write_command)
    monkeypatch.setattr(executor, "current_position", lambda: position_state.PositionState("Flat", 0))
    raw_order = {"symbol": "MNQ", "side": "buy", "qty": 1, "type": "market", "bars": CLEAN_SIGNAL_BARS}

    decision = executor.process_order(raw_order)

    assert decision.allow is True
    write_command.assert_called_once()


# ---------------------------------------------------------------- auto-flatten on market close

CLOSED_HOURS = datetime(2026, 11, 9, 17, 0, tzinfo=time_utils.EASTERN)  # Monday, within the 16:00-18:00 ET closed window


def test_run_once_auto_flattens_when_closed_and_position_open(isolated_state, monkeypatch, tmp_path):
    monkeypatch.setattr(time_utils, "now_et", lambda: CLOSED_HOURS)
    monkeypatch.setattr(executor, "current_position", lambda: position_state.PositionState("Long", 2))
    write_command = mock.Mock(return_value=str(isolated_state / "incoming" / "oif_fake.txt"))
    monkeypatch.setattr(executor.oif_writer, "write_command", write_command)
    orders_path = tmp_path / "orders.json"
    orders_path.write_text(json.dumps({"orders": [{"symbol": "MNQ", "side": "sell", "qty": 1, "type": "market"}]}))

    decisions = executor.run_once(orders_path=str(orders_path))

    assert decisions == []  # normal order processing skipped this cycle
    write_command.assert_called_once()
    command_arg = write_command.call_args[0][1]
    assert command_arg.split(";")[0:3] == ["CLOSEPOSITION", "SIM101", "MNQ 12-26"]
    fills = _read_jsonl(executor.FILLS_LOG)
    assert any(f.get("status") == "auto_flatten_market_closed" for f in fills)


def test_run_once_auto_flattens_when_closed_and_position_unknown(isolated_state, monkeypatch, tmp_path):
    monkeypatch.setattr(time_utils, "now_et", lambda: CLOSED_HOURS)
    monkeypatch.setattr(executor, "current_position", lambda: position_state.PositionState("unknown", 0))
    write_command = mock.Mock(return_value=str(isolated_state / "incoming" / "oif_fake.txt"))
    monkeypatch.setattr(executor.oif_writer, "write_command", write_command)
    orders_path = tmp_path / "orders.json"
    orders_path.write_text(json.dumps({"orders": []}))

    decisions = executor.run_once(orders_path=str(orders_path))

    assert decisions == []
    write_command.assert_called_once()
    command_arg = write_command.call_args[0][1]
    assert command_arg.split(";")[0] == "CLOSEPOSITION"
    fills = _read_jsonl(executor.FILLS_LOG)
    assert any(f.get("status") == "auto_flatten_market_closed" for f in fills)


def test_run_once_does_nothing_when_closed_and_position_flat(isolated_state, monkeypatch, tmp_path):
    monkeypatch.setattr(time_utils, "now_et", lambda: CLOSED_HOURS)
    monkeypatch.setattr(executor, "current_position", lambda: position_state.PositionState("Flat", 0))
    write_command = mock.Mock()
    monkeypatch.setattr(executor.oif_writer, "write_command", write_command)
    orders_path = tmp_path / "orders.json"
    orders_path.write_text(json.dumps({"orders": [{"symbol": "MNQ", "side": "sell", "qty": 1, "type": "market"}]}))

    decisions = executor.run_once(orders_path=str(orders_path))

    assert decisions == []
    write_command.assert_not_called()


def test_run_once_processes_orders_normally_when_within_hours(isolated_state, monkeypatch, tmp_path):
    # Reuses the autouse isolated_state fixture's WITHIN_HOURS clock.
    monkeypatch.setattr(executor, "current_position", lambda: position_state.PositionState("Flat", 0))
    _write_telemetry(isolated_state, monkeypatch)
    write_command = mock.Mock(return_value=str(isolated_state / "incoming" / "oif_fake.txt"))
    monkeypatch.setattr(executor.oif_writer, "write_command", write_command)
    orders_path = tmp_path / "orders.json"
    orders_path.write_text(json.dumps({"orders": [
        {"symbol": "MNQ", "side": "buy", "qty": 1, "type": "market", "bars": CLEAN_SIGNAL_BARS},
    ]}))

    decisions = executor.run_once(orders_path=str(orders_path))

    assert len(decisions) == 1
    assert decisions[0].allow is True
    write_command.assert_called_once()


# ---------------------------------------------------------------- optional explicit BotConfig
# (odd/tasks/tradovate-architecture-scaling.md task 3: purely additive --
# every test above calls with no config argument at all and must keep
# behaving exactly as it always has, reading the same module globals the
# isolated_state fixture monkeypatches.)

def _make_config(isolated_state, **overrides):
    defaults = dict(
        account_id="ALT_ACCOUNT",
        instrument="MNQ 03-27",
        allowed_symbol="MNQ",
        max_contracts=3,
        incoming_dir=str(isolated_state / "incoming"),
        outgoing_dir=str(isolated_state / "outgoing"),
        log_dir=str(isolated_state / "log"),
        telemetry_json_path="",
        telemetry_max_staleness_seconds=30.0,
        fill_timeout_seconds=5.0,
        fill_poll_interval_seconds=0.5,
    )
    defaults.update(overrides)
    return config_module.BotConfig(**defaults)


def test_build_place_command_uses_config_account_and_instrument_override(isolated_state):
    order = executor.normalize_order({"symbol": "MNQ", "side": "buy", "qty": 2, "type": "market"})
    cfg = _make_config(isolated_state)

    command = executor.build_place_command(order, config=cfg)

    assert command == "PLACE;ALT_ACCOUNT;MNQ 03-27;BUY;2;MARKET;;;DAY;;;;"


def test_build_place_command_config_none_behaves_exactly_like_before(isolated_state):
    order = executor.normalize_order({"symbol": "MNQ", "side": "buy", "qty": 2, "type": "market"})

    command_omitted = executor.build_place_command(order)
    command_explicit_none = executor.build_place_command(order, config=None)

    assert command_omitted == command_explicit_none == "PLACE;SIM101;MNQ 12-26;BUY;2;MARKET;;;DAY;;;;"


def test_build_place_command_explicit_account_param_still_wins_over_config(isolated_state):
    # account= is the more specific, already-existing override -- config
    # fields replace module globals, not an explicitly passed account.
    order = executor.normalize_order({"symbol": "MNQ", "side": "buy", "qty": 2, "type": "market"})
    cfg = _make_config(isolated_state)

    command = executor.build_place_command(order, account="EXPLICIT_ACCOUNT", config=cfg)

    assert command == "PLACE;EXPLICIT_ACCOUNT;MNQ 03-27;BUY;2;MARKET;;;DAY;;;;"


def test_process_order_with_config_uses_overridden_account_and_instrument(isolated_state, monkeypatch):
    telemetry_path = isolated_state / "telemetry.json"
    telemetry_path.write_text(json.dumps({
        "updated_at": time_utils.now_et_iso(),
        "daily_realized_pnl": 0.0,
        "eod_balance_last_close": 25000.0,
        "current_balance": 25000.0,
        "daily_profit_today": 0.0,
        "total_realized_profit": 0.0,
    }), encoding="utf-8")
    cfg = _make_config(isolated_state, telemetry_json_path=str(telemetry_path))
    # current_position is monkeypatched here (not relying on a real NT8
    # log file under cfg.log_dir) to isolate this test to the
    # account/instrument override -- position_state's own config-aware
    # behavior isn't this task's concern.
    monkeypatch.setattr(executor, "current_position", lambda config=None: position_state.PositionState("Flat", 0))
    write_command = mock.Mock(return_value=str(isolated_state / "incoming" / "oif_fake.txt"))
    monkeypatch.setattr(executor.oif_writer, "write_command", write_command)
    raw_order = {"symbol": "MNQ", "side": "buy", "qty": 2, "type": "market", "bars": CLEAN_SIGNAL_BARS}

    decision = executor.process_order(raw_order, config=cfg)

    assert decision.allow is True
    incoming_dir_arg, command_arg = write_command.call_args[0]
    assert incoming_dir_arg == cfg.incoming_dir
    assert command_arg == "PLACE;ALT_ACCOUNT;MNQ 03-27;BUY;2;MARKET;;;DAY;;;;"


def test_process_order_config_none_behaves_exactly_like_omitting_it(isolated_state, monkeypatch):
    _write_telemetry(isolated_state, monkeypatch)
    write_command = mock.Mock(return_value=str(isolated_state / "incoming" / "oif_fake.txt"))
    monkeypatch.setattr(executor.oif_writer, "write_command", write_command)
    raw_order = {"symbol": "MNQ", "side": "buy", "qty": 2, "type": "market", "bars": CLEAN_SIGNAL_BARS}

    decision_explicit_none = executor.process_order(raw_order, config=None)

    assert decision_explicit_none.allow is True
    incoming_dir_arg, command_arg = write_command.call_args[0]
    assert incoming_dir_arg == executor.NT8_INCOMING_DIR
    assert command_arg == "PLACE;SIM101;MNQ 12-26;BUY;2;MARKET;;;DAY;;;;"


def test_log_cycle_accepts_a_minimal_partial_record(isolated_state):
    # log_cycle makes no assumption about shape -- the caller is an LLM-
    # driven cron prompt, not code, so it must accept whatever a given
    # cycle actually has (e.g. no order/signal at all for an
    # outside-hours cycle).
    executor.log_cycle({"position": "flat", "within_hours": False, "outcome": "outside_hours"})

    rows = _read_jsonl(executor.CYCLE_LOG)
    assert rows[0]["outcome"] == "outside_hours"
