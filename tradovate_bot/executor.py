"""Main loop: reads an approved order, checks it against risk_guard using
live account state from the Tradovate client, places it only if every check
passes, and logs every decision and fill to local JSON state files.

Input contract: an approved order record with the same shape trader-broker
(day-trader-skill) writes to trader/orders.json -- a dict (or {"orders": [...]})
with at least symbol, side, qty, order type, and optionally stop/target
prices. This bot does not decide entries/exits; it only gates and sends
what it is handed.

No LLM call exists anywhere in this module or anything it calls.
"""
import json
import os
import sys
import time
from datetime import datetime, timezone

from tradovate_bot import risk_guard
from tradovate_bot.client import TradovateClient, TradovateCredentials

STATE_DIR = os.path.join(os.path.dirname(__file__), "state")
DECISIONS_LOG = os.path.join(STATE_DIR, "decisions.log.jsonl")
FILLS_LOG = os.path.join(STATE_DIR, "fills.jsonl")
DEFAULT_ORDERS_PATH = os.environ.get("TRADOVATE_ORDERS_PATH", "trader/orders.json")
POLL_INTERVAL_SECONDS = float(os.environ.get("TRADOVATE_POLL_INTERVAL_SECONDS", "5"))


def _utc_now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ensure_state_dir():
    os.makedirs(STATE_DIR, exist_ok=True)


def _append_jsonl(path, record):
    """Append-only, one JSON object per line. Never silently drops a write:
    raises if the directory can't be created or the file can't be opened."""
    _ensure_state_dir()
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=True))
        fh.write("\n")


def log_decision(order, decision, extra=None):
    record = {
        "at": _utc_now_iso(),
        "order": order,
        "allow": bool(decision.allow),
        "reason": decision.reason,
    }
    if extra:
        record.update(extra)
    _append_jsonl(DECISIONS_LOG, record)


def log_fill(fill_record):
    record = {"at": _utc_now_iso()}
    record.update(fill_record)
    _append_jsonl(FILLS_LOG, record)


def load_orders(path):
    """Reads an orders file in the trader-broker shape: either a bare list
    of orders, or {"orders": [...]}. Returns [] if the file is absent so a
    polling loop can keep running without crashing on "nothing to do"."""
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    if isinstance(doc, dict):
        return doc.get("orders", [])
    if isinstance(doc, list):
        return doc
    return []


def normalize_order(raw_order):
    """Maps a trader-broker-shaped order record onto the fields risk_guard
    and the Tradovate client need: symbol, side, qty, type, plus optional
    stop/target prices. Returns None if the record can't be normalized --
    callers must treat that as a reject, never a pass."""
    if not isinstance(raw_order, dict):
        return None
    symbol = raw_order.get("symbol")
    side = raw_order.get("side")
    qty = raw_order.get("qty")
    order_type = raw_order.get("type", "market")
    if symbol is None or side is None or qty is None:
        return None
    try:
        qty_num = float(qty)
    except (TypeError, ValueError):
        return None
    normalized = {
        "symbol": str(symbol).upper(),
        "side": str(side).lower(),
        "qty": qty_num,
        "type": order_type,
    }
    for key in ("stop_price", "stop_loss_stop_price", "target_price", "take_profit_limit_price", "limit_price"):
        if raw_order.get(key) is not None:
            normalized[key] = raw_order[key]
    return normalized


def fetch_account_state(client):
    """Pulls current account state from Tradovate and maps it onto the
    fields risk_guard requires. On any missing/unexpected field, returns a
    state dict with that field simply absent -- risk_guard itself fails
    closed on missing fields, so no guessing happens here either."""
    state = {}
    try:
        account = client.get_account() or {}
        cash = client.get_cash_balance() or {}
    except Exception:
        # Any network/auth failure means we cannot trust account state:
        # return an empty dict so risk_guard's fail-closed check rejects.
        return {}
    def first_present(*keys):
        for key in keys:
            if cash.get(key) is not None:
                return cash.get(key)
        return None

    state["current_balance"] = first_present("netLiquidationValue", "cashBalance")
    state["eod_balance_last_close"] = first_present("endOfDayNetLiquidation", "priorDayBalance")
    state["daily_realized_pnl"] = first_present("realizedPnl", "totalPnL")
    state["daily_profit_today"] = cash.get("todayRealizedProfit")
    state["total_realized_profit"] = cash.get("totalRealizedProfit")
    return state


def build_tradovate_order(order):
    """Maps our normalized order onto Tradovate's placeorder payload shape."""
    side_map = {"buy": "Buy", "sell": "Sell"}
    type_map = {"market": "Market", "limit": "Limit", "stop": "Stop"}
    payload = {
        "symbol": order["symbol"],
        "action": side_map.get(order["side"], "Buy"),
        "orderQty": int(order["qty"]),
        "orderType": type_map.get(str(order.get("type", "market")).lower(), "Market"),
    }
    if order.get("limit_price") is not None:
        payload["price"] = order["limit_price"]
    if order.get("stop_price") is not None or order.get("stop_loss_stop_price") is not None:
        payload["stopPrice"] = order.get("stop_price", order.get("stop_loss_stop_price"))
    return payload


def process_order(client, raw_order):
    """Normalizes, risk-checks, and -- only on a pass -- places one order.
    Always logs the decision, pass or reject, with its reason."""
    order = normalize_order(raw_order)
    if order is None:
        decision = risk_guard.Decision(False, "order record could not be normalized (missing/malformed fields)")
        log_decision(raw_order, decision)
        return decision

    account_state = fetch_account_state(client)
    decision = risk_guard.evaluate_order(order, account_state)
    log_decision(order, decision, extra={"account_state": account_state})

    if not decision.allow:
        return decision

    try:
        result = client.place_order(build_tradovate_order(order))
    except Exception as exc:  # noqa: BLE001 - must be logged, never swallowed silently
        log_decision(order, risk_guard.Decision(False, "order placement failed: %s" % exc))
        raise
    log_fill({"order": order, "placement_result": result, "status": "sent"})
    return decision


def run_once(client, orders_path=DEFAULT_ORDERS_PATH):
    """Processes every order currently in the orders file. Returns the list
    of decisions made, in order."""
    decisions = []
    for raw_order in load_orders(orders_path):
        decisions.append(process_order(client, raw_order))
    return decisions


def on_ws_message(message):
    """WebSocket callback: logs fills and account-update frames as they
    arrive. Tradovate sends fill events inside an "fills" or "orderVersion"
    collection in its sync frames."""
    if not isinstance(message, dict):
        return
    for key in ("fills", "fill"):
        if key in message:
            payload = message[key]
            items = payload if isinstance(payload, list) else [payload]
            for item in items:
                log_fill({"source": "websocket", "event": item})


def main():
    _ensure_state_dir()
    credentials = TradovateCredentials()
    client = TradovateClient(credentials)
    client.authenticate()
    client.start_listener(on_message=on_ws_message)
    print("tradovate_bot executor running against %s environment, account %s" % (
        "LIVE" if credentials.live else "demo", credentials.account_id), file=sys.stderr)
    try:
        while True:
            run_once(client)
            time.sleep(POLL_INTERVAL_SECONDS)
    except KeyboardInterrupt:
        pass
    finally:
        client.stop_listener()


if __name__ == "__main__":
    main()
