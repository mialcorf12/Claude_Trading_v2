"""Tradovate REST + WebSocket client.

Auth, order placement/cancel/flatten, account/position queries, and a
reconnecting WebSocket listener for fills and account updates. No trading
logic lives here -- this module only talks to Tradovate's API.

Credentials and environment selection come exclusively from environment
variables, never from source:
  TRADOVATE_USERNAME       Tradovate account username
  TRADOVATE_PASSWORD       Tradovate account password
  TRADOVATE_API_KEY        app "name"/client id issued by Tradovate
  TRADOVATE_API_SECRET     app client secret
  TRADOVATE_ACCOUNT_ID     numeric Tradovate account id to trade
  TRADOVATE_DEVICE_ID      a stable per-machine id Tradovate asks for (any
                            stable string is fine, e.g. a hostname-derived id)
  TRADOVATE_ENV            "demo" (default) or "live" -- live requires this
                            to be set explicitly, there is no code default to
                            live.

Tradovate's actual OAuth-style flow is an access-token exchange against
/auth/accesstokenrequest, followed by REST calls carrying that bearer token,
and a WebSocket at wss://.../v1/websocket (demo) or wss://live.tradovateapi.com
(live) authorized by sending the token as the first frame. This module
models that shape; exact Tradovate-side payload field names should be
confirmed against the live API docs before pointing this at a funded
account -- the author did not have real Tradovate demo credentials
available to exercise this end-to-end.
"""
import json
import os
import threading
import time

import requests

try:
    import websockets
    from websockets.sync.client import connect as ws_connect
except ImportError:  # pragma: no cover - exercised only if websockets missing
    websockets = None
    ws_connect = None


DEMO_REST_URL = "https://demo.tradovateapi.com/v1"
LIVE_REST_URL = "https://live.tradovateapi.com/v1"
DEMO_WS_URL = "wss://demo.tradovateapi.com/v1/websocket"
LIVE_WS_URL = "wss://live.tradovateapi.com/v1/websocket"

TOKEN_REFRESH_MARGIN_SECONDS = 60
WS_RECONNECT_DELAY_SECONDS = 3
WS_RECONNECT_MAX_DELAY_SECONDS = 30


class TradovateConfigError(Exception):
    """Raised when required environment configuration is missing."""


class TradovateAPIError(Exception):
    """Raised when Tradovate's REST API returns an error response."""


def env_is_live():
    """True only if TRADOVATE_ENV is explicitly set to "live". Any other
    value, including unset, means demo -- live is never the default."""
    return os.environ.get("TRADOVATE_ENV", "demo").strip().lower() == "live"


def _require_env(name):
    value = os.environ.get(name)
    if not value:
        raise TradovateConfigError("missing required environment variable: %s" % name)
    return value


class TradovateCredentials:
    """Reads credentials from environment variables only. Never hardcode
    these and never log them."""

    def __init__(self):
        self.username = _require_env("TRADOVATE_USERNAME")
        self.password = _require_env("TRADOVATE_PASSWORD")
        self.api_key = _require_env("TRADOVATE_API_KEY")
        self.api_secret = _require_env("TRADOVATE_API_SECRET")
        self.account_id = _require_env("TRADOVATE_ACCOUNT_ID")
        self.device_id = _require_env("TRADOVATE_DEVICE_ID")
        self.live = env_is_live()

    @property
    def rest_url(self):
        return LIVE_REST_URL if self.live else DEMO_REST_URL

    @property
    def ws_url(self):
        return LIVE_WS_URL if self.live else DEMO_WS_URL


class TradovateClient:
    """REST + WebSocket client for one Tradovate account.

    Usage:
        creds = TradovateCredentials()
        client = TradovateClient(creds)
        client.authenticate()
        client.place_order({...})
        client.start_listener(on_message=handle)
        ...
        client.stop_listener()
    """

    def __init__(self, credentials, session=None):
        self.credentials = credentials
        self.session = session or requests.Session()
        self._access_token = None
        self._token_expires_at = 0.0
        self._listener_thread = None
        self._listener_stop = threading.Event()

    # ------------------------------------------------------------ auth

    def authenticate(self, now=None):
        """Exchange credentials for an access token. Tradovate's
        accesstokenrequest endpoint returns {"accessToken": ..., "expirationTime": ...}
        (or an "errorText" on failure)."""
        now = now if now is not None else time.time()
        payload = {
            "name": self.credentials.username,
            "password": self.credentials.password,
            "appId": self.credentials.api_key,
            "appVersion": "1.0",
            "cid": self.credentials.api_key,
            "sec": self.credentials.api_secret,
            "deviceId": self.credentials.device_id,
        }
        data = self._post("/auth/accesstokenrequest", payload, authed=False)
        if "errorText" in data and data.get("errorText"):
            raise TradovateAPIError("authentication failed: %s" % data["errorText"])
        token = data.get("accessToken")
        if not token:
            raise TradovateAPIError("authentication response had no accessToken")
        self._access_token = token
        # Tradovate returns an expirationTime; fall back to a conservative
        # default if that field is absent so we never treat a token as
        # eternally valid.
        expires_in = data.get("expirationTime")
        self._token_expires_at = now + (float(expires_in) if expires_in else 3600.0)
        return token

    def ensure_authenticated(self):
        if not self._access_token or time.time() > self._token_expires_at - TOKEN_REFRESH_MARGIN_SECONDS:
            self.authenticate()
        return self._access_token

    # ------------------------------------------------------------ REST helpers

    def _post(self, path, payload, authed=True):
        headers = {"Content-Type": "application/json"}
        if authed:
            headers["Authorization"] = "Bearer %s" % self.ensure_authenticated()
        resp = self.session.post(self.credentials.rest_url + path, json=payload, headers=headers, timeout=10)
        resp.raise_for_status()
        return resp.json()

    def _get(self, path, params=None, authed=True):
        headers = {}
        if authed:
            headers["Authorization"] = "Bearer %s" % self.ensure_authenticated()
        resp = self.session.get(self.credentials.rest_url + path, params=params, headers=headers, timeout=10)
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------ orders

    def place_order(self, order):
        """order: dict with at least symbol, action (Buy/Sell), orderQty,
        orderType. Returns Tradovate's placeorder response."""
        payload = {
            "accountSpec": self.credentials.username,
            "accountId": int(self.credentials.account_id),
            **order,
        }
        return self._post("/order/placeorder", payload)

    def cancel_order(self, order_id):
        return self._post("/order/cancelorder", {"orderId": order_id})

    def flatten_position(self, symbol):
        """Closes any open position for symbol at market via
        /order/liquidateposition, Tradovate's dedicated flatten endpoint."""
        payload = {"accountId": int(self.credentials.account_id), "symbol": symbol}
        return self._post("/order/liquidateposition", payload)

    # ------------------------------------------------------------ account/position queries

    def get_account(self):
        return self._get("/account/item", params={"id": self.credentials.account_id})

    def get_cash_balance(self):
        return self._get("/cashBalance/getcashbalancesnapshot",
                          params={"accountId": self.credentials.account_id})

    def get_positions(self):
        return self._get("/position/deps", params={"accountId": self.credentials.account_id})

    # ------------------------------------------------------------ websocket listener

    def start_listener(self, on_message, on_error=None):
        """Starts a background thread that connects to Tradovate's WebSocket,
        authorizes with the current access token, and calls on_message(dict)
        for every frame. Reconnects automatically with a short, bounded
        backoff if the connection drops -- no external retry framework, just
        the minimum loop a long-lived listener needs."""
        if ws_connect is None:
            raise RuntimeError("the 'websockets' package is required for the WebSocket listener")
        self._listener_stop.clear()
        self._listener_thread = threading.Thread(
            target=self._listen_forever, args=(on_message, on_error), daemon=True
        )
        self._listener_thread.start()
        return self._listener_thread

    def stop_listener(self):
        self._listener_stop.set()
        if self._listener_thread:
            self._listener_thread.join(timeout=5)

    def _listen_forever(self, on_message, on_error):
        delay = WS_RECONNECT_DELAY_SECONDS
        while not self._listener_stop.is_set():
            try:
                self._listen_once(on_message)
                delay = WS_RECONNECT_DELAY_SECONDS  # reset backoff after a clean session
            except Exception as exc:  # noqa: BLE001 - must never kill the listener thread
                if on_error:
                    on_error(exc)
                if self._listener_stop.is_set():
                    return
                time.sleep(delay)
                delay = min(delay * 2, WS_RECONNECT_MAX_DELAY_SECONDS)

    def _listen_once(self, on_message):
        token = self.ensure_authenticated()
        with ws_connect(self.credentials.ws_url) as ws:
            # Tradovate's WS protocol: send "authorize\n0\n\n<token>" as the
            # first frame after the initial "o" open frame.
            ws.recv()  # the "o" open frame
            ws.send("authorize\n0\n\n%s" % token)
            while not self._listener_stop.is_set():
                raw = ws.recv(timeout=5) if hasattr(ws, "recv") else None
                if raw is None:
                    continue
                for frame in _split_frames(raw):
                    on_message(frame)


def _split_frames(raw):
    """Tradovate multiplexes multiple JSON frames per WS message behind a
    one-character type prefix ('a' = array of frames, 'h' = heartbeat,
    'o'/'c' = open/close control frames). Returns a list of parsed dicts,
    possibly empty."""
    if not raw:
        return []
    kind, _, body = raw[0], None, raw[1:]
    if kind == "h":
        return []
    if kind == "a":
        try:
            items = json.loads(body)
        except json.JSONDecodeError:
            return []
        out = []
        for item in items:
            if isinstance(item, str):
                try:
                    out.append(json.loads(item))
                except json.JSONDecodeError:
                    continue
            elif isinstance(item, dict):
                out.append(item)
        return out
    return []
