"""Unit tests for tradovate_bot.position_state.

These exercise the pure log-line parser (parse_position_state) and the
thin file-finding wrapper (read_position_state) against synthetic NT8
diagnostic-log text -- never a real NT8 instance. See
odd/tasks/tradovate-architecture-scaling.md task 1 for why this exists:
one-position-at-a-time and auto-flatten-on-close were previously enforced
only by an LLM grepping NT8's own log file each cycle in the cron prompt.
"""
import os

import pytest

from tradovate_bot import position_state

LONG_LINE = (
    "2026-11-10 18:52:06:109|1|64|Instrument='MNQ 12-26' Account='Sim101' "
    "Average price=27971.75 Quantity=2 Market position=Long Operation=Update"
)
SHORT_LINE = (
    "2026-11-10 18:53:06:109|1|64|Instrument='MNQ 12-26' Account='Sim101' "
    "Average price=27960.50 Quantity=1 Market position=Short Operation=Update"
)
FLAT_LINE = (
    "2026-11-10 18:52:06:143|1|64|Instrument='MNQ 12-26' Account='Sim101' "
    "Average price=0 Quantity=0 Market position=Flat Operation=Remove"
)
OTHER_ACCOUNT_LONG_LINE = (
    "2026-11-10 18:54:06:109|1|64|Instrument='MNQ 12-26' Account='Sim202' "
    "Average price=28000.0 Quantity=3 Market position=Long Operation=Update"
)
OTHER_ACCOUNT_FLAT_LINE = (
    "2026-11-10 18:55:06:109|1|64|Instrument='MNQ 12-26' Account='Sim202' "
    "Average price=0 Quantity=0 Market position=Flat Operation=Remove"
)


def test_parse_position_state_returns_long_with_qty():
    result = position_state.parse_position_state(LONG_LINE, "Sim101")
    assert result.side == "Long"
    assert result.qty == 2


def test_parse_position_state_returns_short_with_qty():
    result = position_state.parse_position_state(SHORT_LINE, "Sim101")
    assert result.side == "Short"
    assert result.qty == 1


def test_parse_position_state_returns_flat():
    result = position_state.parse_position_state(FLAT_LINE, "Sim101")
    assert result.side == "Flat"
    assert result.qty == 0


def test_parse_position_state_latest_matching_line_wins():
    text = "\n".join([LONG_LINE, FLAT_LINE, SHORT_LINE])
    result = position_state.parse_position_state(text, "Sim101")
    assert result.side == "Short"
    assert result.qty == 1


def test_parse_position_state_ignores_other_accounts():
    text = "\n".join([OTHER_ACCOUNT_LONG_LINE, OTHER_ACCOUNT_FLAT_LINE])
    result = position_state.parse_position_state(text, "Sim202")
    assert result.side == "Flat"  # latest Sim202 line wins
    # Sim101 has no matching line in this text -- must be unknown, never
    # guessed flat or guessed open from another account's lines.
    result_other = position_state.parse_position_state(text, "Sim101")
    assert result_other.side == "unknown"


def test_parse_position_state_empty_content_returns_unknown():
    result = position_state.parse_position_state("", "Sim101")
    assert result.side == "unknown"


def test_parse_position_state_no_matching_account_returns_unknown():
    result = position_state.parse_position_state(LONG_LINE, "NoSuchAccount")
    assert result.side == "unknown"


def test_parse_position_state_rejects_missing_account_argument():
    assert position_state.parse_position_state(LONG_LINE, "").side == "unknown"
    assert position_state.parse_position_state(LONG_LINE, None).side == "unknown"


def test_read_position_state_returns_unknown_when_log_dir_unconfigured():
    result = position_state.read_position_state("", "Sim101")
    assert result.side == "unknown"


def test_read_position_state_returns_unknown_when_dir_missing(tmp_path):
    result = position_state.read_position_state(str(tmp_path / "does_not_exist"), "Sim101")
    assert result.side == "unknown"


def test_read_position_state_returns_unknown_when_no_en_txt_files(tmp_path):
    (tmp_path / "NinjaTrader.txt").write_text(LONG_LINE, encoding="utf-8")
    result = position_state.read_position_state(str(tmp_path), "Sim101")
    assert result.side == "unknown"


def test_read_position_state_reads_matching_position_from_log_file(tmp_path):
    (tmp_path / "log.en.txt").write_text(LONG_LINE, encoding="utf-8")
    result = position_state.read_position_state(str(tmp_path), "Sim101")
    assert result.side == "Long"
    assert result.qty == 2


def test_read_position_state_picks_newest_en_txt_file_by_mtime(tmp_path):
    older = tmp_path / "old.en.txt"
    newer = tmp_path / "new.en.txt"
    older.write_text(LONG_LINE, encoding="utf-8")
    newer.write_text(FLAT_LINE, encoding="utf-8")
    older_time = os.path.getmtime(str(newer)) - 100
    os.utime(str(older), (older_time, older_time))

    result = position_state.read_position_state(str(tmp_path), "Sim101")

    assert result.side == "Flat"


def test_read_position_state_returns_unknown_on_unreadable_file(tmp_path, monkeypatch):
    log_file = tmp_path / "log.en.txt"
    log_file.write_text(LONG_LINE, encoding="utf-8")

    def _raise_open(*args, **kwargs):
        raise OSError("simulated unreadable file")

    monkeypatch.setattr(position_state, "open", _raise_open, raising=False)

    result = position_state.read_position_state(str(tmp_path), "Sim101")

    assert result.side == "unknown"


def test_position_state_is_frozen_dataclass():
    result = position_state.PositionState(side="Flat", qty=0)
    with pytest.raises(Exception):
        result.side = "Long"
