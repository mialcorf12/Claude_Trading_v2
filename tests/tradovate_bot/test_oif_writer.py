"""Unit tests for tradovate_bot.oif_writer -- the NT8 ATI (OIF) transport.

No real NT8 instance is touched; these only exercise the command-string
grammar and the incoming/outgoing filesystem contracts.
"""
import os

from tradovate_bot import oif_writer


def test_build_place_command_full_fields():
    cmd = oif_writer.build_place_command(
        account="SIM101", instrument="MNQ", action="BUY", qty=2, order_type="MARKET",
        limit_price=20100.25, stop_price=20050.0, tif="DAY", oco="OCO1",
        order_id="ORD1", strategy="MyStrat", strategy_id="STRAT1",
    )
    assert cmd == "PLACE;SIM101;MNQ;BUY;2;MARKET;20100.25;20050.0;DAY;OCO1;ORD1;MyStrat;STRAT1"


def test_build_place_command_minimal_fields_blank_optionals():
    cmd = oif_writer.build_place_command(
        account="SIM101", instrument="MNQ", action="SELL", qty=1, order_type="MARKET",
    )
    assert cmd == "PLACE;SIM101;MNQ;SELL;1;MARKET;;;DAY;;;;"
    assert cmd.count(";") == 12


def test_build_cancel_command():
    cmd = oif_writer.build_cancel_command(order_id="ORD1", strategy_id="STRAT1")
    assert cmd == "CANCEL;;;;;;;;;;ORD1;;STRAT1"
    assert cmd.count(";") == 12


def test_build_cancel_command_no_strategy_id():
    cmd = oif_writer.build_cancel_command(order_id="ORD1")
    assert cmd == "CANCEL;;;;;;;;;;ORD1;;"


def test_build_closeposition_command():
    cmd = oif_writer.build_closeposition_command(account="SIM101", instrument="MNQ")
    assert cmd == "CLOSEPOSITION;SIM101;MNQ;;;;;;;;;;"
    assert cmd.count(";") == 12


def test_write_command_writes_readable_file_with_no_temp_leftover(tmp_path):
    incoming = tmp_path / "incoming"
    command = oif_writer.build_closeposition_command("SIM101", "MNQ")

    path = oif_writer.write_command(str(incoming), command)

    assert os.path.exists(path)
    assert os.path.basename(path).startswith("oif_")
    assert os.path.basename(path).endswith(".txt")
    with open(path, encoding="utf-8") as fh:
        content = fh.read()
    assert content.strip() == command

    leftovers = [f for f in os.listdir(str(incoming)) if f != os.path.basename(path)]
    assert leftovers == []


def test_write_command_creates_incoming_dir_if_missing(tmp_path):
    incoming = tmp_path / "does_not_exist_yet"
    oif_writer.write_command(str(incoming), "PLACE;A;B;BUY;1;MARKET;;;DAY;;;;")
    assert os.path.isdir(str(incoming))


def test_write_command_writes_directly_no_rename(tmp_path, monkeypatch):
    """Regression test for a second bug, also confirmed against a live NT8
    instance: staging the temp file outside incoming_dir and renaming the
    final file in (the previous fix) meant NT8's ATI watcher never reacted
    at all -- confirmed live by zero log/trace activity for over a minute
    with ATI enabled (Config.xml IsAtiEnabled=true) and the trace log
    otherwise live. NT8's watcher reacts to a file created directly inside
    incoming_dir, not to one that arrives via a rename from outside it.
    Content is always one short line, so writing directly to the final
    path is safe here. This locks in that write_command never calls
    os.replace/os.rename at all."""
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    replace_calls = []
    monkeypatch.setattr(oif_writer.os, "replace", lambda *a, **k: replace_calls.append((a, k)))

    path = oif_writer.write_command(str(incoming), "PLACE;A;B;BUY;1;MARKET;;;DAY;;;;")

    assert replace_calls == []
    assert os.path.dirname(path) == str(incoming)
    with open(path, encoding="utf-8") as fh:
        assert fh.read().strip() == "PLACE;A;B;BUY;1;MARKET;;;DAY;;;;"


def test_tail_outgoing_returns_empty_for_missing_dir(tmp_path):
    records, seen = oif_writer.tail_outgoing(str(tmp_path / "nope"), set())
    assert records == []
    assert seen == set()


def test_tail_outgoing_picks_up_new_files_only_once(tmp_path):
    outgoing = tmp_path / "outgoing"
    outgoing.mkdir()
    (outgoing / "SIM101_MNQ_ord.txt").write_text("ORD1;FILLED;2;20100.25\n", encoding="utf-8")

    records, seen = oif_writer.tail_outgoing(str(outgoing), set())
    assert len(records) == 1
    assert records[0]["filename"] == "SIM101_MNQ_ord.txt"
    assert records[0]["kind"] == "ord"
    assert records[0]["raw_fields"] == ["ORD1", "FILLED", "2", "20100.25"]
    assert "error" not in records[0]

    records2, seen2 = oif_writer.tail_outgoing(str(outgoing), seen)
    assert records2 == []

    (outgoing / "SIM101_MNQ_pos.txt").write_text("LONG;2;20100.25\n", encoding="utf-8")
    records3, seen3 = oif_writer.tail_outgoing(str(outgoing), seen2)
    assert len(records3) == 1
    assert records3[0]["filename"] == "SIM101_MNQ_pos.txt"
    assert records3[0]["kind"] == "pos"


def test_tail_outgoing_returns_error_record_instead_of_raising(tmp_path, monkeypatch):
    outgoing = tmp_path / "outgoing"
    outgoing.mkdir()
    (outgoing / "SIM101_MNQ_ord.txt").write_text("ORD1;FILLED\n", encoding="utf-8")

    real_open = open

    def flaky_open(path, *args, **kwargs):
        if "SIM101_MNQ_ord.txt" in str(path):
            raise OSError("simulated unreadable file")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", flaky_open)

    records, seen = oif_writer.tail_outgoing(str(outgoing), set())

    assert len(records) == 1
    assert "error" in records[0]
    assert records[0]["filename"] == "SIM101_MNQ_ord.txt"
    assert "SIM101_MNQ_ord.txt" in seen
