"""NinjaTrader 8 Automated Trading Interface (ATI) transport.

Writes Order Instruction Files (OIF) -- plain semicolon-delimited text
command files -- into NT8's `incoming` folder, and tails NT8's `outgoing`
folder for the confirmation files it writes back.

Confirmed OIF grammar (from NT8 ATI docs, see
odd/tasks/tradovate-execution-bot.md "Pivot history" #3). Every command is
exactly 13 semicolon-separated fields, unused fields left empty rather than
omitted:

    PLACE;<ACCOUNT>;<INSTRUMENT>;<ACTION>;<QTY>;<ORDER TYPE>;[LIMIT];[STOP];<TIF>;[OCO];[ORDER ID];[STRATEGY];[STRATEGY ID]
    CANCEL;;;;;;;;;;<ORDER ID>;;[STRATEGY ID]
    CLOSEPOSITION;<ACCOUNT>;<INSTRUMENT>;;;;;;;;;;

NOT confirmed: the exact internal field layout of the confirmation files
NT8 writes to `outgoing`. The task doc only confirms that no account
balance / realized P&L file exists there (which is why the separate
NinjaScript AddOn exists for that -- see account_state.py). `tail_outgoing`
therefore does not claim to decode those files' semantics; it returns the
raw semicolon-split fields plus whatever is safely inferable from the
filename convention. Verify the real field semantics against the user's
actual installed NT8 version once ATI is enabled, before trusting any
specific field from these records for anything beyond logging.
"""
import os
import time
import uuid

OIF_FIELD_COUNT = 13


def _fmt(value):
    """None -> empty field. Everything else -> str(value)."""
    if value is None:
        return ""
    return str(value)


def _join_fields(fields):
    if len(fields) != OIF_FIELD_COUNT:
        raise ValueError("OIF commands must have exactly %d fields, got %d" % (OIF_FIELD_COUNT, len(fields)))
    return ";".join(_fmt(f) for f in fields)


def build_place_command(account, instrument, action, qty, order_type,
                         limit_price=None, stop_price=None, tif="DAY",
                         oco=None, order_id=None, strategy=None, strategy_id=None):
    """Builds a PLACE command string. Required: account, instrument, action,
    qty, order_type. Everything else is optional and left blank when absent."""
    fields = [
        "PLACE", account, instrument, action, qty, order_type,
        limit_price, stop_price, tif, oco, order_id, strategy, strategy_id,
    ]
    return _join_fields(fields)


def build_cancel_command(order_id, strategy_id=None):
    """Builds a CANCEL command string. order_id is required; every other
    field is blank per the confirmed grammar."""
    fields = ["CANCEL", None, None, None, None, None, None, None, None, None, order_id, None, strategy_id]
    return _join_fields(fields)


def build_closeposition_command(account, instrument):
    """Builds a CLOSEPOSITION command string. account/instrument required;
    every other field is blank per the confirmed grammar."""
    fields = ["CLOSEPOSITION", account, instrument, None, None, None, None, None, None, None, None, None, None]
    return _join_fields(fields)


def write_command(incoming_dir, command_text):
    """Writes command_text into incoming_dir as a new oif*.txt file, using a
    temp-write-then-atomic-rename so NT8 never observes a partially written
    file. Returns the final path written."""
    os.makedirs(incoming_dir, exist_ok=True)
    unique = "%d_%s" % (time.time_ns(), uuid.uuid4().hex[:8])
    final_path = os.path.join(incoming_dir, "oif_%s.txt" % unique)
    temp_path = os.path.join(incoming_dir, ".oif_%s.txt.tmp" % unique)
    with open(temp_path, "w", encoding="utf-8", newline="") as fh:
        fh.write(command_text)
        if not command_text.endswith("\n"):
            fh.write("\n")
    os.replace(temp_path, final_path)
    return final_path


def _classify_filename(filename):
    """Best-effort, filename-convention-only classification. NT8's outgoing
    files are documented as named <account>_<instrument>_<suffix>.txt for
    order/position/connection state; this does not decode file contents."""
    stem = filename
    for ext in (".txt",):
        if stem.endswith(ext):
            stem = stem[: -len(ext)]
    parts = stem.split("_")
    kind = None
    if parts and parts[-1].lower() in ("ord", "pos", "cnt", "con"):
        kind = parts[-1].lower()
    account = parts[0] if len(parts) >= 1 else None
    instrument = parts[1] if len(parts) >= 3 else None
    return {"account": account, "instrument": instrument, "kind": kind}


def _read_outgoing_file(outgoing_dir, filename):
    path = os.path.join(outgoing_dir, filename)
    record = {"filename": filename}
    record.update(_classify_filename(filename))
    try:
        with open(path, encoding="utf-8") as fh:
            raw_line = fh.read().strip()
    except OSError as exc:
        record["error"] = "could not read outgoing file: %s" % exc
        return record
    record["raw_line"] = raw_line
    record["raw_fields"] = raw_line.split(";") if raw_line else []
    return record


def tail_outgoing(outgoing_dir, seen_files):
    """Lists files in outgoing_dir not already present in seen_files,
    parses each as a generic semicolon-delimited record (never raising --
    an unreadable file becomes a record with an "error" key instead), and
    returns (new_records, updated_seen_files). seen_files is never mutated
    in place; callers get a new set back."""
    updated_seen = set(seen_files)
    if not os.path.isdir(outgoing_dir):
        return [], updated_seen

    records = []
    for filename in sorted(os.listdir(outgoing_dir)):
        if filename in updated_seen:
            continue
        full_path = os.path.join(outgoing_dir, filename)
        if not os.path.isfile(full_path):
            continue
        records.append(_read_outgoing_file(outgoing_dir, filename))
        updated_seen.add(filename)
    return records, updated_seen
