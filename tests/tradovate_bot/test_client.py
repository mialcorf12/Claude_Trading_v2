"""Unit tests for tradovate_bot.client with mocked HTTP responses.

No real network access and no real Tradovate demo credentials were
available in this environment, so authentication, order placement, and the
WebSocket listener are exercised here only against mocked requests.Session
responses -- never against the live Tradovate API. Real end-to-end demo
connectivity remains unverified.
"""
import os
from unittest import mock

import pytest

from tradovate_bot import client as tv


ENV_VARS = {
    "TRADOVATE_USERNAME": "testuser",
    "TRADOVATE_PASSWORD": "testpass",
    "TRADOVATE_API_KEY": "testkey",
    "TRADOVATE_API_SECRET": "testsecret",
    "TRADOVATE_ACCOUNT_ID": "12345",
    "TRADOVATE_DEVICE_ID": "test-device",
}


@pytest.fixture
def env(monkeypatch):
    for k, v in ENV_VARS.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("TRADOVATE_ENV", raising=False)
    yield


def test_credentials_default_to_demo(env):
    creds = tv.TradovateCredentials()
    assert creds.live is False
    assert creds.rest_url == tv.DEMO_REST_URL
    assert creds.ws_url == tv.DEMO_WS_URL


def test_env_live_requires_explicit_flag(env, monkeypatch):
    monkeypatch.setenv("TRADOVATE_ENV", "live")
    creds = tv.TradovateCredentials()
    assert creds.live is True
    assert creds.rest_url == tv.LIVE_REST_URL


def test_missing_env_var_raises_config_error(monkeypatch):
    for k in ENV_VARS:
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(tv.TradovateConfigError):
        tv.TradovateCredentials()


def _mock_response(json_data, status=200):
    resp = mock.Mock()
    resp.json.return_value = json_data
    resp.raise_for_status.side_effect = None
    return resp


def test_authenticate_stores_token(env):
    creds = tv.TradovateCredentials()
    session = mock.Mock()
    session.post.return_value = _mock_response({"accessToken": "abc123", "expirationTime": 3600})
    client = tv.TradovateClient(creds, session=session)

    token = client.authenticate(now=1000.0)

    assert token == "abc123"
    assert client._access_token == "abc123"
    assert client._token_expires_at == 1000.0 + 3600
    session.post.assert_called_once()
    called_url = session.post.call_args[0][0]
    assert called_url == tv.DEMO_REST_URL + "/auth/accesstokenrequest"


def test_authenticate_raises_on_error_text(env):
    creds = tv.TradovateCredentials()
    session = mock.Mock()
    session.post.return_value = _mock_response({"errorText": "bad credentials"})
    client = tv.TradovateClient(creds, session=session)

    with pytest.raises(tv.TradovateAPIError):
        client.authenticate()


def test_ensure_authenticated_refreshes_expired_token(env):
    creds = tv.TradovateCredentials()
    session = mock.Mock()
    session.post.return_value = _mock_response({"accessToken": "tok1", "expirationTime": 10})
    client = tv.TradovateClient(creds, session=session)
    client.authenticate(now=1000.0)

    # Token "expires" almost immediately; ensure_authenticated must re-auth.
    with mock.patch("time.time", return_value=1000.0 + 20):
        session.post.return_value = _mock_response({"accessToken": "tok2", "expirationTime": 3600})
        token = client.ensure_authenticated()
    assert token == "tok2"


def test_place_order_includes_account_id(env):
    creds = tv.TradovateCredentials()
    session = mock.Mock()
    session.post.side_effect = [
        _mock_response({"accessToken": "tok", "expirationTime": 3600}),
        _mock_response({"orderId": 999}),
    ]
    client = tv.TradovateClient(creds, session=session)

    result = client.place_order({"symbol": "MNQZ6", "action": "Buy", "orderQty": 1, "orderType": "Market"})

    assert result == {"orderId": 999}
    second_call_payload = session.post.call_args_list[1][1]["json"]
    assert second_call_payload["accountId"] == 12345
    assert second_call_payload["symbol"] == "MNQZ6"


def test_cancel_order(env):
    creds = tv.TradovateCredentials()
    session = mock.Mock()
    session.post.side_effect = [
        _mock_response({"accessToken": "tok", "expirationTime": 3600}),
        _mock_response({"orderId": 999, "cancelRequested": True}),
    ]
    client = tv.TradovateClient(creds, session=session)
    result = client.cancel_order(999)
    assert result["cancelRequested"] is True


def test_flatten_position(env):
    creds = tv.TradovateCredentials()
    session = mock.Mock()
    session.post.side_effect = [
        _mock_response({"accessToken": "tok", "expirationTime": 3600}),
        _mock_response({"status": "ok"}),
    ]
    client = tv.TradovateClient(creds, session=session)
    result = client.flatten_position("MNQZ6")
    assert result == {"status": "ok"}


def test_split_frames_handles_heartbeat_and_array():
    assert tv._split_frames("h") == []
    assert tv._split_frames('a[{"e":"fill"}]') == [{"e": "fill"}]
    assert tv._split_frames('a["{\\"e\\":\\"fill\\"}"]') == [{"e": "fill"}]


def test_split_frames_handles_malformed_input():
    assert tv._split_frames("a[not json") == []
    assert tv._split_frames("") == []
