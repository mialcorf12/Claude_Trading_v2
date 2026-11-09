"""Main loop: reads an approved order, checks it against risk_guard using
account state from the NinjaScript AddOn's telemetry file, places it only
if every check passes via NT8's ATI (OIF) transport, and logs every
decision and fill to local JSON state files.

Input contract: an approved order record with the same shape trader-broker
(day-trader-skill) writes to trader/orders.json -- a dict (or {"orders": [...]})
with at least symbol, side, qty, order type, and optionally stop/target
prices. This bot does not decide entries/exits; it only gates and sends
what it is handed.

Transport: NinjaTrader 8's Automated Trading Interface (ATI) -- see
oif_writer.py for the Order Instruction File grammar and the outgoing-folder
tailer. Account state (balance / realized P&L for the Lucid kill switches)
comes from a separate, purpose-built channel: account_state.py reads a JSON
file exported by the read-only NinjaScript AddOn
(ninjatrader_addon/AccountTelemetryAddOn.cs).

No LLM call exists anywhere in this module or anything it calls.
"""
import json
import os
import sys
import time
from datetime import datetime, timezone

from tradovate_bot import account_state, oif_writer, risk_guard

STATE_DIR = os.path.join(os.path.dirname(__file__), "state")
DECISIONS_LOG = os.path.join(STATE_DIR, "decisions.log.jsonl")
FILLS_LOG = os.path.join(STATE_DIR, "fills.jsonl")
DEFAULT_ORDERS_PATH = os.environ.get("TRADOVATE_ORDERS_PATH", "trader/orders.json")
POLL_INTERVAL_SECONDS = float(os.environ.get("TRADOVATE_POLL_INTERVAL_SECONDS", "5"))

# NT8 ATI transport configuration. NT8_INCOMING_DIR/NT8_OUTGOING_DIR are the
# ATI "incoming"/"outgoing" folders NT8 is configured to watch (typically
# under My Documents\<NT Folder>\incoming and \outgoing). NT8_ACCOUNT_ID is
# the NT8 account *name* string (e.g. "Sim101" or the live Tradovate-backed
# account name as NT8 displays it) -- not a Tradovate numeric account id,
# since ATI never talks to Tradovate's API directly.
NT8_INCOMING_DIR = os.environ.get("NT8_INCOMING_DIR", "")
NT8_OUTGOING_DIR = os.environ.get("NT8_OUTGOING_DIR", "")
NT8_ACCOUNT_ID = os.environ.get("NT8_ACCOUNT_ID", "")

# Path to the JSON telemetry file written by the read-only NinjaScript
# AddOn. See account_state.py for the required field contract.
ACCOUNT_TELEMETRY_JSON_PATH = os.environ.get("ACCOUNT_TELEMETRY_JSON_PATH", "")
ACCOUNT_TELEMETRY_MAX_STALENESS_SECONDS = float(
    os.environ.get("ACCOUNT_TELEMETRY_MAX_STALENESS_SECONDS", "30")
)


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
    and the OIF writer need: symbol, side, qty, type, plus optional
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


def fetch_account_state():
    """Reads current account state from the NinjaScript AddOn's telemetry
    file. Returns {} (triggering risk_guard's own fail-closed check) on any
    missing file, stale data, or malformed payload -- account_state.py
    itself never raises for those cases."""
    if not ACCOUNT_TELEMETRY_JSON_PATH:
        return {}
    return account_state.read_account_state(
        ACCOUNT_TELEMETRY_JSON_PATH,
        max_staleness_seconds=ACCOUNT_TELEMETRY_MAX_STALENESS_SECONDS,
    )


# NT8 ATI action strings for futures are BUY/SELL (per NT8 ATI usage
# examples in the task doc's confirmed grammar); this mapping is the one
# place that assumption lives, isolated from oif_writer's pure grammar
# functions.
_SIDE_TO_ATI_ACTION = {"buy": "BUY", "sell": "SELL"}
_TYPE_TO_ATI_ORDER_TYPE = {"market": "MARKET", "limit": "LIMIT", "stop": "STOP"}


def build_place_command(order, account=None):
    """Maps our normalized order onto an OIF PLACE command string. account
    defaults to the module-level NT8_ACCOUNT_ID, read at call time (not at
    import time) so tests/callers can reconfigure it after import."""
    if account is None:
        account = NT8_ACCOUNT_ID
    action = _SIDE_TO_ATI_ACTION.get(order["side"], "BUY")
    order_type = _TYPE_TO_ATI_ORDER_TYPE.get(str(order.get("type", "market")).lower(), "MARKET")
    stop_price = order.get("stop_price", order.get("stop_loss_stop_price"))
    return oif_writer.build_place_command(
        account=account,
        instrument=order["symbol"],
        action=action,
        qty=int(order["qty"]),
        order_type=order_type,
        limit_price=order.get("limit_price"),
        stop_price=stop_price,
    )


def process_order(raw_order):
    """Normalizes, risk-checks, and -- only on a pass -- places one order
    via the NT8 ATI OIF writer. Always logs the decision, pass or reject,
    with its reason."""
    order = normalize_order(raw_order)
    if order is None:
        decision = risk_guard.Decision(False, "order record could not be normalized (missing/malformed fields)")
        log_decision(raw_order, decision)
        return decision

    account_state_dict = fetch_account_state()
    decision = risk_guard.evaluate_order(order, account_state_dict)
    log_decision(order, decision, extra={"account_state": account_state_dict})

    if not decision.allow:
        return decision

    command = build_place_command(order)
    try:
        path = oif_writer.write_command(NT8_INCOMING_DIR, command)
    except OSError as exc:
        log_decision(order, risk_guard.Decision(False, "order placement failed: %s" % exc))
        raise
    log_fill({"order": order, "command": command, "oif_path": path, "status": "sent"})
    return decision


def run_once(orders_path=DEFAULT_ORDERS_PATH):
    """Processes every order currently in the orders file. Returns the list
    of decisions made, in order."""
    decisions = []
    for raw_order in load_orders(orders_path):
        decisions.append(process_order(raw_order))
    return decisions


def poll_outgoing_once(seen_files):
    """Tails NT8's outgoing folder once for new order/position/connection
    confirmation files, logging each as a fill-log record for later review.
    These files are logging-only -- see oif_writer.py's module docstring on
    why their internal fields are not trusted for risk decisions. Returns
    the updated seen_files set."""
    records, updated_seen = oif_writer.tail_outgoing(NT8_OUTGOING_DIR, seen_files)
    for record in records:
        log_fill({"source": "nt8_outgoing", "record": record})
    return updated_seen


def main():
    _ensure_state_dir()
    if not NT8_INCOMING_DIR or not NT8_ACCOUNT_ID or not ACCOUNT_TELEMETRY_JSON_PATH:
        print(
            "tradovate_bot executor misconfigured: NT8_INCOMING_DIR, NT8_ACCOUNT_ID, and "
            "ACCOUNT_TELEMETRY_JSON_PATH must all be set. Refusing to start rather than run "
            "against an unconfigured/ambiguous transport.",
            file=sys.stderr,
        )
        sys.exit(1)
    print(
        "tradovate_bot executor running: account=%s incoming=%s outgoing=%s telemetry=%s"
        % (NT8_ACCOUNT_ID, NT8_INCOMING_DIR, NT8_OUTGOING_DIR or "(not configured)", ACCOUNT_TELEMETRY_JSON_PATH),
        file=sys.stderr,
    )
    seen_outgoing_files = set()
    try:
        while True:
            run_once()
            if NT8_OUTGOING_DIR:
                seen_outgoing_files = poll_outgoing_once(seen_outgoing_files)
            time.sleep(POLL_INTERVAL_SECONDS)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
