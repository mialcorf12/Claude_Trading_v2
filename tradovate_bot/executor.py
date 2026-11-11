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

from tradovate_bot import account_state, oif_writer, position_state, risk_guard, signals, time_utils

STATE_DIR = os.path.join(os.path.dirname(__file__), "state")
DECISIONS_LOG = os.path.join(STATE_DIR, "decisions.log.jsonl")
FILLS_LOG = os.path.join(STATE_DIR, "fills.jsonl")
CYCLE_LOG = os.path.join(STATE_DIR, "cycles.log.jsonl")
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

# Directory holding NT8's own windowed diagnostic log (*.en.txt), NOT the
# ATI incoming/outgoing folders above -- see position_state.py. Used to
# read the account's current market position for the one-position-at-a-
# time and auto-flatten-on-close safeguards (see
# odd/tasks/tradovate-architecture-scaling.md task 1).
NT8_LOG_DIR = os.environ.get("NT8_LOG_DIR", "")

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


def log_cycle(cycle_record):
    """Appends one structured record per overnight trading cycle to
    CYCLE_LOG, so cycle history can be summarized (win/loss counts, P&L,
    skip reasons, signal distribution) with jq/pandas instead of parsing
    `odd/tasks/tradovate-execution-bot.md`'s prose. The cron-driven cycle
    itself lives in a prompt, not in this module, so this function makes
    no assumption about the record's shape beyond adding a timestamp --
    the caller supplies whatever fields apply to that cycle. Recommended
    fields (all optional, omit what doesn't apply to a given cycle):
      position: "flat" | "open"
      within_hours: bool
      data_fresh: bool
      signal: "buy" | "sell" | "skip" | "flatten"
      signal_reason: short free-text summary of the read
      order: {"side", "qty", "stop_loss_points", "take_profit_points"}
      risk_guard_allow: bool
      risk_guard_reason: str
      outcome: "filled" | "rejected" | "timeout_flatten" | "tp_hit" |
               "sl_hit" | "skipped" | "outside_hours"
      fill_price: float
      pnl: float
      daily_profit_today: float (from account_telemetry.json)
      notes: short free-text for anything not covered above
    """
    record = {"at": time_utils.now_et_iso()}
    record.update(cycle_record)
    _append_jsonl(CYCLE_LOG, record)


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
    for key in ("stop_price", "limit_price", "stop_loss_points", "take_profit_points", "bars"):
        if raw_order.get(key) is not None:
            normalized[key] = raw_order[key]
    return normalized


def fetch_account_state(config=None):
    """Reads current account state from the NinjaScript AddOn's telemetry
    file. Returns {} (triggering risk_guard's own fail-closed check) on any
    missing file, stale data, or malformed payload -- account_state.py
    itself never raises for those cases.

    `config`, when given, supplies telemetry_json_path/
    telemetry_max_staleness_seconds instead of the module-level
    ACCOUNT_TELEMETRY_JSON_PATH/ACCOUNT_TELEMETRY_MAX_STALENESS_SECONDS for
    this call only; None (the default) changes nothing (see
    odd/tasks/tradovate-architecture-scaling.md task 3)."""
    telemetry_path = config.telemetry_json_path if config is not None else ACCOUNT_TELEMETRY_JSON_PATH
    max_staleness = (
        config.telemetry_max_staleness_seconds
        if config is not None
        else ACCOUNT_TELEMETRY_MAX_STALENESS_SECONDS
    )
    if not telemetry_path:
        return {}
    return account_state.read_account_state(
        telemetry_path,
        max_staleness_seconds=max_staleness,
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


def build_place_command(order, account=None, config=None):
    """Maps our normalized order onto an OIF PLACE command string for the
    order's own entry. account defaults to the module-level NT8_ACCOUNT_ID
    (or config.account_id when an explicit config is given and account is
    not), read at call time (not at import time) so tests/callers can
    reconfigure it after import. The OIF INSTRUMENT field is always
    NT8_INSTRUMENT (or config.instrument), the NT8-qualified contract name
    -- never order["symbol"], which risk_guard checks against the generic
    "MNQ" root and which NT8 itself rejects as an instrument. limit_price/
    stop_price here are the entry's own trigger (for a LIMIT or STOP
    entry), never the position's protective exit prices -- those are
    bracket legs, built separately by build_bracket_exit_commands.

    `config` is an optional explicit identity/transport bundle (see
    tradovate_bot/config.py and
    odd/tasks/tradovate-architecture-scaling.md task 3): when given, its
    fields take precedence over the module globals for this call, except
    an explicitly passed `account` still wins over config.account_id --
    `account` is the already-existing, more specific override. When
    `config` is None (the default), nothing changes from before."""
    if account is None:
        account = config.account_id if config is not None else NT8_ACCOUNT_ID
    instrument = config.instrument if config is not None else NT8_INSTRUMENT
    action = _SIDE_TO_ATI_ACTION.get(order["side"], "BUY")
    order_type = _TYPE_TO_ATI_ORDER_TYPE.get(str(order.get("type", "market")).lower(), "MARKET")
    return oif_writer.build_place_command(
        account=account,
        instrument=instrument,
        action=action,
        qty=int(order["qty"]),
        order_type=order_type,
        limit_price=order.get("limit_price"),
        stop_price=order.get("stop_price"),
    )


def build_bracket_exit_commands(order, stop_price, target_price, account=None, config=None):
    """Builds the OIF PLACE commands for an order's protective exit legs:
    a stop-loss (STOPMARKET) at stop_price and/or a take-profit (LIMIT) at
    target_price, both on the side opposite the entry, same qty,
    instrument NT8_INSTRUMENT/config.instrument (see build_place_command).
    When both are present they share one OCO group id so a fill on either
    leg cancels the other -- NT8 enforces that linkage; this code only
    assigns matching group ids. Returns [] when neither price is given (no
    bracket requested).

    stop_price/target_price must already be absolute, tick-rounded prices
    derived from the CONFIRMED fill -- see compute_bracket_prices_from_fill.
    This function does no price derivation of its own.

    `config` behaves exactly as in build_place_command: optional, its
    fields override the module globals for this call, an explicit
    `account` still wins over config.account_id, and None changes nothing
    (see odd/tasks/tradovate-architecture-scaling.md task 3)."""
    if account is None:
        account = config.account_id if config is not None else NT8_ACCOUNT_ID
    instrument = config.instrument if config is not None else NT8_INSTRUMENT
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
            account=account, instrument=instrument, action=exit_action,
            qty=qty, order_type="STOPMARKET", stop_price=stop_price, oco=oco,
        ))
    if target_price is not None:
        commands.append(oif_writer.build_place_command(
            account=account, instrument=instrument, action=exit_action,
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


_TERMINAL_OUTGOING_STATUSES = {"FILLED", "REJECTED", "CANCELLED"}


def wait_for_entry_fill(seen_files, timeout_seconds=None, poll_interval_seconds=None, config=None):
    """Polls NT8's outgoing folder for the entry order's own confirmation,
    logging every new record seen (same as poll_outgoing_once). Returns a
    FillResult as soon as a FILLED or REJECTED record appears, or "timeout"
    once timeout_seconds has elapsed with neither. seen_files should be a
    snapshot of the outgoing folder taken immediately before the entry
    command was written, so only records that appear afterward are seen.

    NT8 rewrites the SAME outgoing filename in place as an order's status
    progresses (e.g. SUBMITTED -> FILLED) rather than writing a new file
    per status (confirmed live, 2026-11-10: a poll that caught a file
    while still SUBMITTED, with oif_writer.tail_outgoing's normal
    "new filenames only" exclusion, then never saw that same file's later
    rewrite to FILLED -- timing out and defensively flattening a position
    that had, in fact, already filled). So a filename is only treated as
    permanently seen once it reaches a terminal status; anything else is
    re-read on the next poll.

    `config`, when given, supplies outgoing_dir/fill_timeout_seconds/
    fill_poll_interval_seconds instead of NT8_OUTGOING_DIR/
    FILL_CONFIRMATION_TIMEOUT_SECONDS/FILL_CONFIRMATION_POLL_INTERVAL_SECONDS
    -- only for whichever of timeout_seconds/poll_interval_seconds was left
    None, same precedence as every other config-aware function here. None
    (the default) changes nothing (see
    odd/tasks/tradovate-architecture-scaling.md task 3)."""
    if timeout_seconds is None:
        timeout_seconds = config.fill_timeout_seconds if config is not None else FILL_CONFIRMATION_TIMEOUT_SECONDS
    if poll_interval_seconds is None:
        poll_interval_seconds = (
            config.fill_poll_interval_seconds if config is not None else FILL_CONFIRMATION_POLL_INTERVAL_SECONDS
        )
    outgoing_dir = config.outgoing_dir if config is not None else NT8_OUTGOING_DIR
    seen = set(seen_files)
    deadline = time.monotonic() + timeout_seconds
    while True:
        records, polled_seen = oif_writer.tail_outgoing(outgoing_dir, seen)
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
            if status not in _TERMINAL_OUTGOING_STATUSES:
                polled_seen.discard(record.get("filename"))
        seen = polled_seen
        if time.monotonic() >= deadline:
            return FillResult("timeout")
        time.sleep(poll_interval_seconds)


def _snapshot_outgoing_files(config=None):
    outgoing_dir = config.outgoing_dir if config is not None else NT8_OUTGOING_DIR
    if not outgoing_dir or not os.path.isdir(outgoing_dir):
        return set()
    return set(os.listdir(outgoing_dir))


def current_position(config=None):
    """Reads the current NT8 market position for NT8_ACCOUNT_ID from NT8's
    own diagnostic log (see position_state.py), using NT8_LOG_DIR. Returns
    a position_state.PositionState; "unknown" (unconfigured/missing/
    unreadable log, or no matching account line) is never treated as Flat
    by any caller here -- see process_order and run_once.

    `config`, when given, supplies log_dir/account_id instead of the
    module globals for this call only; None (the default) changes nothing
    (see odd/tasks/tradovate-architecture-scaling.md task 3)."""
    log_dir = config.log_dir if config is not None else NT8_LOG_DIR
    account_id = config.account_id if config is not None else NT8_ACCOUNT_ID
    return position_state.read_position_state(log_dir, account_id)


def process_order(raw_order, config=None):
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
    possibly-open position with no stop and no target.

    After risk_guard passes but before the one-position-at-a-time check,
    this also runs the signals.py guardrails: signals.evaluate_signal(
    order["bars"], order["side"]) must come back clear, or the entry is
    refused -- same fail-closed pattern as risk_guard and the position
    check, not an optional extra. An order with no `bars` field (or
    malformed/insufficient bars) is refused here too: signals.py's own
    guards already fail closed (flagged=True) on missing/malformed/too-few
    bars, so this gate never special-cases a missing `bars` field itself --
    it just trusts evaluate_signal's own fail-closed verdict and refuses
    whenever `clear` isn't True. This formalizes the pullback-over-chase
    and repeated-reversal-is-chop rules in code instead of leaving them as
    prose the cron prompt has to re-derive every cycle. See
    odd/tasks/tradovate-architecture-scaling.md task 4.

    After risk_guard and the signals guard both pass but before placing
    the entry, this also enforces one-position-at-a-time: if
    current_position() reports anything other than Flat (an open
    Long/Short, or "unknown" because the log couldn't be read) the entry
    is refused -- this bot is single-order/single-position by design, so
    any non-Flat read means a new entry must not be placed. See
    odd/tasks/tradovate-architecture-scaling.md task 1.

    `config` is an optional explicit identity/transport bundle, threaded
    through to every call below that accepts one (fetch_account_state,
    risk_guard.evaluate_order, current_position, build_place_command,
    wait_for_entry_fill, build_bracket_exit_commands). `config=None` (the
    default, or simply omitting it) reads every module global exactly as
    before -- see odd/tasks/tradovate-architecture-scaling.md task 3.
    current_position is only ever called with the config kwarg when a
    config was actually given, so a caller that monkeypatches
    current_position with a plain zero-argument stub (as every pre-
    existing test here does) keeps working unchanged."""
    order = normalize_order(raw_order)
    if order is None:
        decision = risk_guard.Decision(False, "order record could not be normalized (missing/malformed fields)")
        log_decision(raw_order, decision)
        return decision

    account_state_dict = fetch_account_state(config=config)
    decision = risk_guard.evaluate_order(order, account_state_dict, time_utils.now_et(), config=config)
    log_decision(order, decision, extra={"account_state": account_state_dict})

    if not decision.allow:
        return decision

    verdict = signals.evaluate_signal(order.get("bars"), order.get("side"))
    if not verdict.clear:
        refusal = risk_guard.Decision(
            False,
            "refusing new entry: signals guard flagged this setup -- %s" % "; ".join(verdict.reasons),
        )
        log_decision(order, refusal, extra={"signal_reasons": list(verdict.reasons)})
        return refusal

    position = current_position(config=config) if config is not None else current_position()
    account_id = config.account_id if config is not None else NT8_ACCOUNT_ID
    if position.side != "Flat":
        refusal = risk_guard.Decision(
            False,
            "refusing new entry: one-position-at-a-time -- current position on account %s is "
            "%s (qty=%s), not Flat" % (account_id, position.side, position.qty),
        )
        log_decision(order, refusal, extra={"position_side": position.side, "position_qty": position.qty})
        return refusal

    wants_bracket = order.get("stop_loss_points") is not None or order.get("take_profit_points") is not None

    outgoing_dir = config.outgoing_dir if config is not None else NT8_OUTGOING_DIR
    incoming_dir = config.incoming_dir if config is not None else NT8_INCOMING_DIR
    instrument = config.instrument if config is not None else NT8_INSTRUMENT

    if wants_bracket and not outgoing_dir:
        refusal = risk_guard.Decision(
            False,
            "bracket requested (stop_loss_points/take_profit_points) but NT8_OUTGOING_DIR is not "
            "configured, so the real fill price can never be confirmed; refusing to place an entry "
            "this bot could not safely protect",
        )
        log_decision(order, refusal)
        return refusal

    seen_before = _snapshot_outgoing_files(config=config) if wants_bracket else None
    entry_command = build_place_command(order, config=config)
    try:
        path = oif_writer.write_command(incoming_dir, entry_command)
    except OSError as exc:
        log_decision(order, risk_guard.Decision(False, "order placement failed: %s" % exc))
        raise
    log_fill({"order": order, "command": entry_command, "oif_path": path, "status": "sent"})

    if not wants_bracket:
        return decision

    fill = wait_for_entry_fill(seen_before, config=config)

    if fill.status == "rejected":
        log_fill({"order": order, "status": "entry_rejected_by_nt8"})
        return decision

    if fill.status != "filled":
        close_command = oif_writer.build_closeposition_command(account=account_id, instrument=instrument)
        close_path = oif_writer.write_command(incoming_dir, close_command)
        log_fill({
            "order": order, "command": close_command, "oif_path": close_path,
            "status": "defensive_flatten_fill_confirmation_timeout",
        })
        return decision

    stop_price, target_price = compute_bracket_prices_from_fill(order, fill.price)
    for command in build_bracket_exit_commands(order, stop_price, target_price, config=config):
        try:
            exit_path = oif_writer.write_command(incoming_dir, command)
        except OSError as exc:
            log_decision(order, risk_guard.Decision(False, "bracket placement failed: %s" % exc))
            raise
        log_fill({"order": order, "command": command, "oif_path": exit_path, "status": "sent", "fill_price": fill.price})
    return decision


def run_once(orders_path=DEFAULT_ORDERS_PATH, config=None):
    """Runs one poll cycle. Before processing any new orders, checks
    risk_guard.check_trading_hours: if the market is closed AND a position
    is currently open (or "unknown" -- treated the same as open, never
    assumed safe, per this module's fail-closed contract), this
    auto-flattens via a CLOSEPOSITION OIF command (the same mechanism
    wait_for_entry_fill's timeout path already uses in process_order) and
    skips normal order processing for this cycle -- the mandatory
    16:00-18:00 ET flatten no longer depends on the cron prompt's manual
    step (see odd/tasks/tradovate-architecture-scaling.md task 1). If the
    market is closed and the position is Flat, there's nothing to flatten
    and no new entries are possible anyway (risk_guard would reject them),
    so order processing is simply skipped for the cycle. Returns the list
    of decisions made for whatever orders were actually processed (empty
    when the cycle was closed-market and skipped).

    `config`, when given, is threaded through to current_position and
    every process_order call this cycle makes; None (the default) reads
    every module global exactly as before (see
    odd/tasks/tradovate-architecture-scaling.md task 3). Like
    process_order, current_position is only called with the config kwarg
    when a config was actually given, so existing zero-argument
    monkeypatch stubs of current_position keep working unchanged."""
    now = time_utils.now_et()
    hours_ok, _hours_reason = risk_guard.check_trading_hours(now)
    if not hours_ok:
        position = current_position(config=config) if config is not None else current_position()
        if position.side != "Flat":
            account_id = config.account_id if config is not None else NT8_ACCOUNT_ID
            instrument = config.instrument if config is not None else NT8_INSTRUMENT
            incoming_dir = config.incoming_dir if config is not None else NT8_INCOMING_DIR
            close_command = oif_writer.build_closeposition_command(account=account_id, instrument=instrument)
            close_path = oif_writer.write_command(incoming_dir, close_command)
            log_fill({
                "command": close_command,
                "oif_path": close_path,
                "status": "auto_flatten_market_closed",
                "position_side": position.side,
                "position_qty": position.qty,
            })
        return []

    decisions = []
    for raw_order in load_orders(orders_path):
        decisions.append(process_order(raw_order, config=config))
    return decisions


def poll_outgoing_once(seen_files, config=None):
    """Tails NT8's outgoing folder once for new order/position/connection
    confirmation files, logging each as a fill-log record for later review.
    These files are logging-only -- see oif_writer.py's module docstring on
    why their internal fields are not trusted for risk decisions. Returns
    the updated seen_files set.

    `config`, when given, supplies outgoing_dir instead of the module-level
    NT8_OUTGOING_DIR for this call only; None (the default) changes
    nothing (see odd/tasks/tradovate-architecture-scaling.md task 3)."""
    outgoing_dir = config.outgoing_dir if config is not None else NT8_OUTGOING_DIR
    records, updated_seen = oif_writer.tail_outgoing(outgoing_dir, seen_files)
    for record in records:
        log_fill({"source": "nt8_outgoing", "record": record})
    return updated_seen


def main(config=None):
    """Starts the always-on poll loop. `config`, when given (e.g.
    tradovate_bot.config.BotConfig.from_env(), or a different instance's
    config for a second account/instrument in the same process), is
    threaded through to run_once/poll_outgoing_once and used for this
    function's own startup misconfiguration check and status print
    instead of the module globals. None (the default -- main() keeps
    working exactly as before when called with no arguments) reads
    NT8_INCOMING_DIR/NT8_ACCOUNT_ID/NT8_OUTGOING_DIR/
    ACCOUNT_TELEMETRY_JSON_PATH exactly as before. See
    odd/tasks/tradovate-architecture-scaling.md task 3."""
    _ensure_state_dir()
    incoming_dir = config.incoming_dir if config is not None else NT8_INCOMING_DIR
    account_id = config.account_id if config is not None else NT8_ACCOUNT_ID
    outgoing_dir = config.outgoing_dir if config is not None else NT8_OUTGOING_DIR
    telemetry_path = config.telemetry_json_path if config is not None else ACCOUNT_TELEMETRY_JSON_PATH
    if not incoming_dir or not account_id or not telemetry_path:
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
        % (account_id, incoming_dir, outgoing_dir or "(not configured)", telemetry_path),
        file=sys.stderr,
    )
    seen_outgoing_files = set()
    try:
        while True:
            run_once(config=config)
            if outgoing_dir:
                seen_outgoing_files = poll_outgoing_once(seen_outgoing_files, config=config)
            time.sleep(POLL_INTERVAL_SECONDS)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
