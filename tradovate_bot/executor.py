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
import uuid
from dataclasses import dataclass

from tradovate_bot import account_state, oif_writer, risk_guard, time_utils

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

# How long to wait for NT8's own fill confirmation before giving up (see
# wait_for_entry_fill). A bracket is only ever sized from this CONFIRMED
# fill price, never from a quote fetched before submission -- see
# odd/tasks/tradovate-execution-bot.md, 2026-11-10: a bracket sized off a
# pre-submission quote repeatedly landed on the wrong side of a fast-moving
# market, causing premature exits and, once, a fully rejected/naked bracket.
FILL_CONFIRMATION_TIMEOUT_SECONDS = float(os.environ.get("TRADOVATE_FILL_TIMEOUT_SECONDS", "5"))
FILL_CONFIRMATION_POLL_INTERVAL_SECONDS = float(os.environ.get("TRADOVATE_FILL_POLL_INTERVAL_SECONDS", "0.5"))


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
        "at": time_utils.now_et_iso(),
        "order": order,
        "allow": bool(decision.allow),
        "reason": decision.reason,
    }
    if extra:
        record.update(extra)
    _append_jsonl(DECISIONS_LOG, record)


def log_fill(fill_record):
    record = {"at": time_utils.now_et_iso()}
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
    and the OIF writer need: symbol, side, qty, type, plus optional entry
    trigger prices and bracket point-distances. Returns None if the record
    can't be normalized -- callers must treat that as a reject, never a
    pass.

    stop_loss_points/take_profit_points are distances in price points, NOT
    absolute prices -- process_order computes the actual bracket prices
    from the CONFIRMED fill (see compute_bracket_prices_from_fill), since
    an absolute price supplied here would necessarily be based on a quote
    fetched before the order is even submitted."""
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
    for key in ("stop_price", "limit_price", "stop_loss_points", "take_profit_points"):
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
# functions. "stop" maps to "STOPMARKET", not "STOP" -- confirmed live
# against a real NT8 instance: it rejected "STOP" outright with "holds
# invalid order type parameter 'STOP'".
_SIDE_TO_ATI_ACTION = {"buy": "BUY", "sell": "SELL"}
_OPPOSITE_SIDE = {"buy": "sell", "sell": "buy"}
_TYPE_TO_ATI_ORDER_TYPE = {"market": "MARKET", "limit": "LIMIT", "stop": "STOPMARKET"}

# MNQ futures contract NT8 ATI needs for the OIF INSTRUMENT field -- NT8
# rejects the generic root symbol outright (confirmed live: subscribing to
# market data for bare "MNQ" failed with "Symbol is inaccessible" /
# "UnknownSymbol", which then self-rejected the order with "There is no
# market data available to drive the simulation engine."). risk_guard's
# own ALLOWED_SYMBOL stays the generic "MNQ" root (that's what "MNQ only,
# cap of 3 contracts" means for risk purposes) -- this is a separate
# constant, read only when building the OIF command actually sent to NT8.
# MNQ rolls quarterly (Mar/Jun/Sep/Dec); update this by hand each roll to
# match NT8's own display name for the new front-month contract (check
# NT8's instrument list or chart title -- confirmed format so far:
# "<ROOT> <MM-YY>", e.g. "MNQ 12-26" for the December 2026 contract).
NT8_INSTRUMENT = "MNQ 12-26"


def build_place_command(order, account=None):
    """Maps our normalized order onto an OIF PLACE command string for the
    order's own entry. account defaults to the module-level NT8_ACCOUNT_ID,
    read at call time (not at import time) so tests/callers can reconfigure
    it after import. The OIF INSTRUMENT field is always NT8_INSTRUMENT, the
    NT8-qualified contract name -- never order["symbol"], which risk_guard
    checks against the generic "MNQ" root and which NT8 itself rejects as
    an instrument. limit_price/stop_price here are the entry's own trigger
    (for a LIMIT or STOP entry), never the position's protective exit
    prices -- those are bracket legs, built separately by
    build_bracket_exit_commands."""
    if account is None:
        account = NT8_ACCOUNT_ID
    action = _SIDE_TO_ATI_ACTION.get(order["side"], "BUY")
    order_type = _TYPE_TO_ATI_ORDER_TYPE.get(str(order.get("type", "market")).lower(), "MARKET")
    return oif_writer.build_place_command(
        account=account,
        instrument=NT8_INSTRUMENT,
        action=action,
        qty=int(order["qty"]),
        order_type=order_type,
        limit_price=order.get("limit_price"),
        stop_price=order.get("stop_price"),
    )


def build_bracket_exit_commands(order, stop_price, target_price, account=None):
    """Builds the OIF PLACE commands for an order's protective exit legs:
    a stop-loss (STOPMARKET) at stop_price and/or a take-profit (LIMIT) at
    target_price, both on the side opposite the entry, same qty,
    instrument NT8_INSTRUMENT (see build_place_command). When both are
    present they share one OCO group id so a fill on either leg cancels
    the other -- NT8 enforces that linkage; this code only assigns
    matching group ids. Returns [] when neither price is given (no
    bracket requested).

    stop_price/target_price must already be absolute, tick-rounded prices
    derived from the CONFIRMED fill -- see compute_bracket_prices_from_fill.
    This function does no price derivation of its own."""
    if account is None:
        account = NT8_ACCOUNT_ID
    if stop_price is None and target_price is None:
        return []
    exit_side = _OPPOSITE_SIDE.get(order["side"])
    if exit_side is None:
        return []
    exit_action = _SIDE_TO_ATI_ACTION[exit_side]
    qty = int(order["qty"])
    oco = "OCO-%s" % uuid.uuid4().hex[:8] if stop_price is not None and target_price is not None else None
    commands = []
    if stop_price is not None:
        commands.append(oif_writer.build_place_command(
            account=account, instrument=NT8_INSTRUMENT, action=exit_action,
            qty=qty, order_type="STOPMARKET", stop_price=stop_price, oco=oco,
        ))
    if target_price is not None:
        commands.append(oif_writer.build_place_command(
            account=account, instrument=NT8_INSTRUMENT, action=exit_action,
            qty=qty, order_type="LIMIT", limit_price=target_price, oco=oco,
        ))
    return commands


_TICK_SIZE = 0.25


def _round_to_tick(price):
    return round(price / _TICK_SIZE) * _TICK_SIZE


def compute_bracket_prices_from_fill(order, fill_price):
    """Computes tick-rounded absolute stop/target prices from the order's
    requested point distances and the CONFIRMED fill price -- never from a
    quote fetched before submission. Returns (stop_price, target_price);
    either is None if its point distance wasn't requested on the order."""
    stop_points = order.get("stop_loss_points")
    target_points = order.get("take_profit_points")
    sign = {"buy": 1, "sell": -1}.get(order.get("side"))
    if sign is None:
        return None, None
    stop_price = _round_to_tick(fill_price - sign * stop_points) if stop_points is not None else None
    target_price = _round_to_tick(fill_price + sign * target_points) if target_points is not None else None
    return stop_price, target_price


@dataclass(frozen=True)
class FillResult:
    status: str  # "filled", "rejected", or "timeout"
    price: float = None


def wait_for_entry_fill(seen_files, timeout_seconds=None, poll_interval_seconds=None):
    """Polls NT8's outgoing folder for the entry order's own confirmation,
    logging every new record seen (same as poll_outgoing_once). Returns a
    FillResult as soon as a FILLED or REJECTED record appears, or "timeout"
    once timeout_seconds has elapsed with neither. seen_files should be a
    snapshot of the outgoing folder taken immediately before the entry
    command was written, so only records that appear afterward are seen."""
    if timeout_seconds is None:
        timeout_seconds = FILL_CONFIRMATION_TIMEOUT_SECONDS
    if poll_interval_seconds is None:
        poll_interval_seconds = FILL_CONFIRMATION_POLL_INTERVAL_SECONDS
    seen = set(seen_files)
    deadline = time.monotonic() + timeout_seconds
    while True:
        records, seen = oif_writer.tail_outgoing(NT8_OUTGOING_DIR, seen)
        for record in records:
            log_fill({"source": "nt8_outgoing", "record": record})
            raw_fields = record.get("raw_fields") or []
            status = raw_fields[0] if raw_fields else None
            if status == "FILLED":
                try:
                    price = float(raw_fields[2])
                except (IndexError, ValueError):
                    price = None
                if price is not None:
                    return FillResult("filled", price)
            elif status == "REJECTED":
                return FillResult("rejected")
        if time.monotonic() >= deadline:
            return FillResult("timeout")
        time.sleep(poll_interval_seconds)


def _snapshot_outgoing_files():
    if not NT8_OUTGOING_DIR or not os.path.isdir(NT8_OUTGOING_DIR):
        return set()
    return set(os.listdir(NT8_OUTGOING_DIR))


def process_order(raw_order):
    """Normalizes, risk-checks, and -- only on a pass -- places one order
    via the NT8 ATI OIF writer. Always logs the decision, pass or reject,
    with its reason.

    When the order requests a bracket (stop_loss_points/take_profit_points),
    the entry is placed alone first, then this waits for NT8's own fill
    confirmation and sizes the stop/target from that CONFIRMED price --
    never from a quote fetched before submission (see
    compute_bracket_prices_from_fill's docstring for why). If NT8 rejects
    the entry, no bracket is placed (there is nothing to protect). If fill
    confirmation doesn't arrive within FILL_CONFIRMATION_TIMEOUT_SECONDS,
    this defensively flattens via CLOSEPOSITION rather than risk leaving a
    possibly-open position with no stop and no target."""
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

    wants_bracket = order.get("stop_loss_points") is not None or order.get("take_profit_points") is not None

    if wants_bracket and not NT8_OUTGOING_DIR:
        refusal = risk_guard.Decision(
            False,
            "bracket requested (stop_loss_points/take_profit_points) but NT8_OUTGOING_DIR is not "
            "configured, so the real fill price can never be confirmed; refusing to place an entry "
            "this bot could not safely protect",
        )
        log_decision(order, refusal)
        return refusal

    seen_before = _snapshot_outgoing_files() if wants_bracket else None
    entry_command = build_place_command(order)
    try:
        path = oif_writer.write_command(NT8_INCOMING_DIR, entry_command)
    except OSError as exc:
        log_decision(order, risk_guard.Decision(False, "order placement failed: %s" % exc))
        raise
    log_fill({"order": order, "command": entry_command, "oif_path": path, "status": "sent"})

    if not wants_bracket:
        return decision

    fill = wait_for_entry_fill(seen_before)

    if fill.status == "rejected":
        log_fill({"order": order, "status": "entry_rejected_by_nt8"})
        return decision

    if fill.status != "filled":
        close_command = oif_writer.build_closeposition_command(account=NT8_ACCOUNT_ID, instrument=NT8_INSTRUMENT)
        close_path = oif_writer.write_command(NT8_INCOMING_DIR, close_command)
        log_fill({
            "order": order, "command": close_command, "oif_path": close_path,
            "status": "defensive_flatten_fill_confirmation_timeout",
        })
        return decision

    stop_price, target_price = compute_bracket_prices_from_fill(order, fill.price)
    for command in build_bracket_exit_commands(order, stop_price, target_price):
        try:
            exit_path = oif_writer.write_command(NT8_INCOMING_DIR, command)
        except OSError as exc:
            log_decision(order, risk_guard.Decision(False, "bracket placement failed: %s" % exc))
            raise
        log_fill({"order": order, "command": command, "oif_path": exit_path, "status": "sent", "fill_price": fill.price})
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
            "against an unconfigured/ambiguous transport.\n"
            "If you just set these with [Environment]::SetEnvironmentVariable(...,\"Machine\"), "
            "this terminal (and NT8, if already running) started before that write and is still "
            "holding its old, empty environment -- Windows does not refresh an already-running "
            "process's variables. Close this terminal and NT8 entirely, open a brand-new terminal "
            "and relaunch NT8, then retry. If it still doesn't pick them up, log off and back into "
            "this RDP session once.",
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
