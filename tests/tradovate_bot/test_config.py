"""Test-first unit tests for tradovate_bot.config.BotConfig.

BotConfig.from_env() must reproduce the exact same env vars and defaults
executor.py reads at import time (see executor.py's module-level globals
and odd/tasks/archive/tradovate-architecture-scaling.md task 3), so it's a drop-in
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


# ---------------------------------------------------------------- single source of defaults
# (odd/tasks/config-single-source.md) config.BotConfig.from_env() is the ONE place
# that reads the env vars and owns the defaults and the MNQ contract name; the
# executor's module-level names are its values.

import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
EXECUTOR_SOURCE = (REPO_ROOT / "tradovate_bot" / "executor.py").read_text(encoding="utf-8")


def test_the_mnq_contract_name_is_defined_once_in_config():
    from tradovate_bot import executor
    assert config_module.DEFAULT_INSTRUMENT == "MNQ 12-26"
    assert config_module.BotConfig.from_env().instrument == config_module.DEFAULT_INSTRUMENT
    assert executor.NT8_INSTRUMENT == config_module.DEFAULT_INSTRUMENT
    assert not re.search(r'^NT8_INSTRUMENT\s*=\s*"', EXECUTOR_SOURCE, re.MULTILINE)


def test_executor_does_not_keep_a_second_copy_of_the_env_defaults():
    for needle in ('os.environ.get("NT8_', 'os.environ.get("ACCOUNT_TELEMETRY', 'os.environ.get("TRADOVATE_FILL'):
        assert needle not in EXECUTOR_SOURCE, "executor.py reads %s itself instead of using BotConfig.from_env()" % needle


def test_executor_module_globals_follow_the_env_through_botconfig():
    env = dict(os.environ,
               NT8_ACCOUNT_ID="ACC1", NT8_INCOMING_DIR="in_dir", NT8_OUTGOING_DIR="out_dir", NT8_LOG_DIR="log_dir",
               ACCOUNT_TELEMETRY_JSON_PATH="tele.json", ACCOUNT_TELEMETRY_MAX_STALENESS_SECONDS="12",
               TRADOVATE_FILL_TIMEOUT_SECONDS="7", TRADOVATE_FILL_POLL_INTERVAL_SECONDS="0.25")
    code = ("import json, tradovate_bot.executor as e; print(json.dumps([e.NT8_ACCOUNT_ID, e.NT8_INCOMING_DIR, "
            "e.NT8_OUTGOING_DIR, e.NT8_LOG_DIR, e.ACCOUNT_TELEMETRY_JSON_PATH, "
            "e.ACCOUNT_TELEMETRY_MAX_STALENESS_SECONDS, e.FILL_CONFIRMATION_TIMEOUT_SECONDS, "
            "e.FILL_CONFIRMATION_POLL_INTERVAL_SECONDS, e.NT8_INSTRUMENT]))")
    out = subprocess.run([sys.executable, "-c", code], cwd=str(REPO_ROOT), env=env,
                         capture_output=True, text=True, check=True).stdout
    assert json.loads(out) == ["ACC1", "in_dir", "out_dir", "log_dir", "tele.json", 12.0, 7.0, 0.25, "MNQ 12-26"]
