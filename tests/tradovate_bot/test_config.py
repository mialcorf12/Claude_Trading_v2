"""Test-first unit tests for tradovate_bot.config.BotConfig.

BotConfig.from_env() must reproduce the exact same env vars and defaults
executor.py reads at import time (see executor.py's module-level globals
and odd/tasks/tradovate-architecture-scaling.md task 3), so it's a drop-in
stand-in for "the bot's current real configuration" wherever a caller
wants to start threading an explicit config through instead of relying on
executor.py's/risk_guard.py's module globals.
"""
import dataclasses

import pytest

from tradovate_bot import config as config_module
from tradovate_bot import risk_guard

_ENV_VARS = (
    "NT8_INCOMING_DIR",
    "NT8_OUTGOING_DIR",
    "NT8_ACCOUNT_ID",
    "NT8_LOG_DIR",
    "ACCOUNT_TELEMETRY_JSON_PATH",
    "ACCOUNT_TELEMETRY_MAX_STALENESS_SECONDS",
    "TRADOVATE_FILL_TIMEOUT_SECONDS",
    "TRADOVATE_FILL_POLL_INTERVAL_SECONDS",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    yield


def test_from_env_defaults_match_executors_current_defaults():
    cfg = config_module.BotConfig.from_env()
    assert cfg.account_id == ""
    assert cfg.instrument == "MNQ 12-26"  # matches executor.NT8_INSTRUMENT, not env-backed
    assert cfg.allowed_symbol == risk_guard.ALLOWED_SYMBOL
    assert cfg.max_contracts == risk_guard.MAX_CONTRACTS
    assert cfg.incoming_dir == ""
    assert cfg.outgoing_dir == ""
    assert cfg.log_dir == ""
    assert cfg.telemetry_json_path == ""
    assert cfg.telemetry_max_staleness_seconds == 30.0
    assert cfg.fill_timeout_seconds == 5.0
    assert cfg.fill_poll_interval_seconds == 0.5


def test_from_env_reads_overridden_env_vars(monkeypatch):
    monkeypatch.setenv("NT8_INCOMING_DIR", "/tmp/incoming")
    monkeypatch.setenv("NT8_OUTGOING_DIR", "/tmp/outgoing")
    monkeypatch.setenv("NT8_ACCOUNT_ID", "Sim202")
    monkeypatch.setenv("NT8_LOG_DIR", "/tmp/log")
    monkeypatch.setenv("ACCOUNT_TELEMETRY_JSON_PATH", "/tmp/telemetry.json")
    monkeypatch.setenv("ACCOUNT_TELEMETRY_MAX_STALENESS_SECONDS", "45")
    monkeypatch.setenv("TRADOVATE_FILL_TIMEOUT_SECONDS", "10")
    monkeypatch.setenv("TRADOVATE_FILL_POLL_INTERVAL_SECONDS", "1.5")

    cfg = config_module.BotConfig.from_env()

    assert cfg.incoming_dir == "/tmp/incoming"
    assert cfg.outgoing_dir == "/tmp/outgoing"
    assert cfg.account_id == "Sim202"
    assert cfg.log_dir == "/tmp/log"
    assert cfg.telemetry_json_path == "/tmp/telemetry.json"
    assert cfg.telemetry_max_staleness_seconds == 45.0
    assert cfg.fill_timeout_seconds == 10.0
    assert cfg.fill_poll_interval_seconds == 1.5
    # instrument/allowed_symbol/max_contracts are not env-backed
    assert cfg.instrument == "MNQ 12-26"
    assert cfg.allowed_symbol == risk_guard.ALLOWED_SYMBOL
    assert cfg.max_contracts == risk_guard.MAX_CONTRACTS


def test_bot_config_is_frozen():
    cfg = config_module.BotConfig.from_env()
    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.account_id = "changed"
