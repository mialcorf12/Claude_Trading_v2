"""Unit tests for tradovate_bot.executor, with a mocked Tradovate client.

These exercise the executor's order normalization, risk-gating, and logging
logic. They do not touch any real Tradovate endpoint.
"""
import json
import os
from unittest import mock

import pytest

from tradovate_bot import executor


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(executor, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(executor, "DECISIONS_LOG", str(tmp_path / "decisions.log.jsonl"))
    monkeypatch.setattr(executor, "FILLS_LOG", str(tmp_path / "fills.jsonl"))
    yield tmp_path


def _read_jsonl(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def good_account_state():
    return {
        "daily_realized_pnl": 0.0,
        "eod_balance_last_close": 25000.0,
        "current_balance": 25000.0,
        "daily_profit_today": 0.0,
        "total_realized_profit": 0.0,
    }


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


def test_process_order_rejects_when_normalization_fails(isolated_state):
    client = mock.Mock()
    decision = executor.process_order(client, {"symbol": "MNQ"})
    assert decision.allow is False
    client.place_order.assert_not_called()
    rows = _read_jsonl(executor.DECISIONS_LOG)
    assert len(rows) == 1
    assert rows[0]["allow"] is False


def test_process_order_rejects_when_risk_guard_fails(isolated_state):
    client = mock.Mock()
    client.get_account.return_value = {}
    client.get_cash_balance.return_value = {
        "netLiquidationValue": 24000.0,
        "endOfDayNetLiquidation": 25000.0,  # floor 24000, balance at floor is ok
        "realizedPnl": -650.0,  # breaches $600 daily loss limit
    }
    raw_order = {"symbol": "MNQ", "side": "buy", "qty": 1, "type": "market"}

    decision = executor.process_order(client, raw_order)

    assert decision.allow is False
    client.place_order.assert_not_called()
    rows = _read_jsonl(executor.DECISIONS_LOG)
    assert len(rows) == 1
    assert "daily loss" in rows[0]["reason"].lower()


def test_process_order_places_order_when_all_checks_pass(isolated_state):
    client = mock.Mock()
    client.get_account.return_value = {}
    client.get_cash_balance.return_value = {
        "netLiquidationValue": 25000.0,
        "endOfDayNetLiquidation": 25000.0,
        "realizedPnl": 0.0,
        "todayRealizedProfit": 0.0,
        "totalRealizedProfit": 0.0,
    }
    client.place_order.return_value = {"orderId": 42}
    raw_order = {"symbol": "MNQ", "side": "buy", "qty": 2, "type": "market"}

    decision = executor.process_order(client, raw_order)

    assert decision.allow is True
    client.place_order.assert_called_once()
    sent_payload = client.place_order.call_args[0][0]
    assert sent_payload["symbol"] == "MNQ"
    assert sent_payload["orderQty"] == 2

    decisions = _read_jsonl(executor.DECISIONS_LOG)
    fills = _read_jsonl(executor.FILLS_LOG)
    assert decisions[0]["allow"] is True
    assert fills[0]["status"] == "sent"


def test_process_order_rejects_oversized_mnq_position(isolated_state):
    client = mock.Mock()
    client.get_account.return_value = {}
    client.get_cash_balance.return_value = {
        "netLiquidationValue": 25000.0, "endOfDayNetLiquidation": 25000.0, "realizedPnl": 0.0,
        "todayRealizedProfit": 0.0, "totalRealizedProfit": 0.0,
    }
    raw_order = {"symbol": "MNQ", "side": "buy", "qty": 5, "type": "market"}
    decision = executor.process_order(client, raw_order)
    assert decision.allow is False
    client.place_order.assert_not_called()


def test_fetch_account_state_returns_empty_dict_on_client_failure():
    client = mock.Mock()
    client.get_account.side_effect = RuntimeError("network down")
    state = executor.fetch_account_state(client)
    assert state == {}


def test_load_orders_handles_missing_file(tmp_path):
    assert executor.load_orders(str(tmp_path / "nope.json")) == []


def test_load_orders_handles_bare_list_and_wrapped_dict(tmp_path):
    list_path = tmp_path / "list.json"
    list_path.write_text(json.dumps([{"symbol": "MNQ"}]))
    assert executor.load_orders(str(list_path)) == [{"symbol": "MNQ"}]

    dict_path = tmp_path / "wrapped.json"
    dict_path.write_text(json.dumps({"orders": [{"symbol": "MNQ"}]}))
    assert executor.load_orders(str(dict_path)) == [{"symbol": "MNQ"}]


def test_run_once_processes_every_order(isolated_state, tmp_path):
    orders_path = tmp_path / "orders.json"
    orders_path.write_text(json.dumps({"orders": [
        {"symbol": "MNQ", "side": "buy", "qty": 1, "type": "market"},
        {"symbol": "MNQ", "side": "sell", "qty": 1, "type": "market"},
    ]}))
    client = mock.Mock()
    client.get_account.return_value = {}
    client.get_cash_balance.return_value = {
        "netLiquidationValue": 25000.0, "endOfDayNetLiquidation": 25000.0, "realizedPnl": 0.0,
        "todayRealizedProfit": 0.0, "totalRealizedProfit": 0.0,
    }
    client.place_order.return_value = {"orderId": 1}

    decisions = executor.run_once(client, orders_path=str(orders_path))

    assert len(decisions) == 2
    assert all(d.allow for d in decisions)
    assert client.place_order.call_count == 2
