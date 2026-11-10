"""Unit tests for tradovate_bot.executor against the NT8 ATI (OIF) transport.

These exercise the executor's order normalization, risk-gating, OIF command
building, and logging logic. No real NT8 instance is touched: the OIF
writer's filesystem call is monkeypatched to capture its arguments, and
account state comes from a real temp JSON fixture file exercised through
the actual account_state.read_account_state function.
"""
import json
import os
from unittest import mock

import pytest

from tradovate_bot import executor, time_utils


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(executor, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(executor, "DECISIONS_LOG", str(tmp_path / "decisions.log.jsonl"))
    monkeypatch.setattr(executor, "FILLS_LOG", str(tmp_path / "fills.jsonl"))
    monkeypatch.setattr(executor, "NT8_INCOMING_DIR", str(tmp_path / "incoming"))
    monkeypatch.setattr(executor, "NT8_OUTGOING_DIR", str(tmp_path / "outgoing"))
    monkeypatch.setattr(executor, "NT8_ACCOUNT_ID", "SIM101")
    monkeypatch.setattr(executor, "NT8_INSTRUMENT", "MNQ 12-26")
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
           "stop_loss_stop_price": 20000.0, "take_profit_limit_price": 20200.0}
    order = executor.normalize_order(raw)
    assert order["symbol"] == "MNQ"
    assert order["side"] == "buy"
    assert order["qty"] == 2.0
    assert order["stop_loss_stop_price"] == 20000.0


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
    raw_order = {"symbol": "MNQ", "side": "buy", "qty": 2, "type": "market"}

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
        {"symbol": "MNQ", "side": "buy", "qty": 1, "type": "market"},
        {"symbol": "MNQ", "side": "sell", "qty": 1, "type": "market"},
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
    assert executor.build_bracket_exit_commands(order, account="SIM101") == []


def test_build_bracket_exit_commands_builds_stop_and_target_with_shared_oco():
    order = executor.normalize_order({
        "symbol": "MNQ", "side": "sell", "qty": 3, "type": "market",
        "stop_loss_stop_price": 27990.0, "take_profit_limit_price": 27850.0,
    })
    commands = executor.build_bracket_exit_commands(order, account="SIM101")

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
    order = executor.normalize_order({
        "symbol": "MNQ", "side": "buy", "qty": 2, "type": "market",
        "stop_loss_stop_price": 27500.0,
    })
    commands = executor.build_bracket_exit_commands(order, account="SIM101")

    assert len(commands) == 1
    fields = commands[0].split(";")
    assert fields[0:6] == ["PLACE", "SIM101", "MNQ 12-26", "SELL", "2", "STOPMARKET"]
    assert fields[9] == ""  # no OCO when there's only one exit leg


def test_process_order_places_entry_and_bracket_legs_when_prices_present(isolated_state, monkeypatch):
    _write_telemetry(isolated_state, monkeypatch)
    write_command = mock.Mock(return_value=str(isolated_state / "incoming" / "oif_fake.txt"))
    monkeypatch.setattr(executor.oif_writer, "write_command", write_command)
    raw_order = {
        "symbol": "MNQ", "side": "sell", "qty": 3, "type": "market",
        "stop_loss_stop_price": 27990.0, "take_profit_limit_price": 27850.0,
    }

    decision = executor.process_order(raw_order)

    assert decision.allow is True
    assert write_command.call_count == 3
    sent_commands = [call.args[1] for call in write_command.call_args_list]
    assert sent_commands[0].split(";")[0:6] == ["PLACE", "SIM101", "MNQ 12-26", "SELL", "3", "MARKET"]
    assert sent_commands[1].split(";")[0:6] == ["PLACE", "SIM101", "MNQ 12-26", "BUY", "3", "STOPMARKET"]
    assert sent_commands[2].split(";")[0:6] == ["PLACE", "SIM101", "MNQ 12-26", "BUY", "3", "LIMIT"]

    fills = _read_jsonl(executor.FILLS_LOG)
    assert len(fills) == 3
    assert all(f["status"] == "sent" for f in fills)
