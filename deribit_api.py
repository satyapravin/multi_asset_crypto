"""
deribit_api.py
==============
Thin wrapper around the Deribit REST API v2.

Public endpoints (no auth) -- routed to config.DATA_BASE_URL:
  - get_book_summary_by_currency  (option chain snapshot)
  - get_index_price               (spot)
  - get_funding_rate_history      (BTC perp funding history)
  - get_instrument                (contract specs for a symbol)

Private endpoints (OAuth2 client_credentials) -- routed to
config.TRADE_BASE_URL:
  - get_positions                 (current open positions)
  - buy / sell                    (market orders)
  - get_account_summary           (USDC balance)

This split lets you trade on testnet (TRADE_ENV=test, default) while
still pulling real, liquid market data from production (DATA_ENV=live,
default).  Testnet option chains are too thin to compute trustworthy
skew_z signals on.

All prices are floats; for LINEAR perps `amount` is in BASE CURRENCY
(BTC, ETH).  `client_credentials` token is cached and refreshed
automatically when expired.

We intentionally avoid websockets/threading for clarity -- the live
deployment runs once per N hours from cron, not as a long-lived daemon.
"""
from __future__ import annotations

import time
from typing import Any
import requests

import config


class DeribitError(RuntimeError):
    pass


class DeribitClient:
    def __init__(self,
                 client_id:        str | None = None,
                 client_secret:    str | None = None,
                 data_base_url:    str | None = None,
                 trade_base_url:   str | None = None):
        self.client_id      = client_id      or config.CLIENT_ID
        self.client_secret  = client_secret  or config.CLIENT_SECRET
        self.data_base_url  = data_base_url  or config.DATA_BASE_URL
        self.trade_base_url = trade_base_url or config.TRADE_BASE_URL
        self._token: str | None = None
        self._token_expiry: float = 0.0
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": "deribit-live-strategy/0.1"})

    # ----------------------------------------------------------
    # Low-level transport
    # ----------------------------------------------------------
    def _call(self, method: str, params: dict | None = None,
              auth: bool = False, timeout: int = 20) -> Any:
        """Issue a JSON-RPC-style GET request to Deribit REST.

        Public endpoints go to DATA_BASE_URL (real liquidity by default).
        Private endpoints + the auth call go to TRADE_BASE_URL (testnet
        by default).
        """
        base = self.trade_base_url if auth else self.data_base_url
        url = f"{base}/{method}"
        headers = {}
        if auth:
            headers["Authorization"] = f"Bearer {self._get_token()}"
        try:
            r = self.s.get(url, params=params or {}, headers=headers,
                           timeout=timeout)
        except requests.RequestException as e:
            raise DeribitError(f"network error calling {method}: {e}") from e
        if r.status_code != 200:
            raise DeribitError(
                f"{method} HTTP {r.status_code} ({base}): {r.text[:300]}")
        body = r.json()
        if "error" in body:
            raise DeribitError(
                f"{method} error ({base}): {body['error']}")
        return body.get("result", body)

    # ----------------------------------------------------------
    # OAuth2 token (client_credentials grant)
    # ----------------------------------------------------------
    def _get_token(self) -> str:
        if self._token and time.time() < self._token_expiry - 30:
            return self._token
        if not self.client_id or not self.client_secret:
            raise DeribitError(
                "Missing DERIBIT_CLIENT_ID / DERIBIT_CLIENT_SECRET in .env")
        params = {
            "grant_type":    "client_credentials",
            "client_id":     self.client_id,
            "client_secret": self.client_secret,
        }
        # The auth endpoint is technically `public/auth` but credentials
        # are venue-specific so we MUST hit the trading venue, not the
        # data venue.  Using the trade base URL via the get-with-auth
        # codepath (auth=False because no bearer token yet -- but force
        # the trade host explicitly).
        url = f"{self.trade_base_url}/public/auth"
        try:
            r = self.s.get(url, params=params, timeout=20)
        except requests.RequestException as e:
            raise DeribitError(f"auth network error: {e}") from e
        if r.status_code != 200:
            raise DeribitError(
                f"auth HTTP {r.status_code} ({self.trade_base_url}): "
                f"{r.text[:300]}")
        body = r.json()
        if "error" in body:
            raise DeribitError(f"auth error: {body['error']}")
        result = body["result"]
        self._token = result["access_token"]
        self._token_expiry = time.time() + float(result.get("expires_in", 900))
        return self._token

    # ----------------------------------------------------------
    # Public endpoints
    # ----------------------------------------------------------
    def get_index_price(self, index_name: str) -> float:
        """index_name e.g. 'btc_usd', 'eth_usd'."""
        r = self._call("public/get_index_price", {"index_name": index_name})
        return float(r["index_price"])

    def get_book_summary_by_currency(self, currency: str,
                                     kind: str = "option") -> list[dict]:
        """One row per instrument: mark_iv, mark_price, bid_price, ask_price,
        underlying_price, etc."""
        return self._call("public/get_book_summary_by_currency",
                          {"currency": currency, "kind": kind})

    def get_funding_rate_history(self, instrument_name: str,
                                 start_ts_ms: int, end_ts_ms: int) -> list[dict]:
        """Funding observations between two epoch-ms timestamps.  For
        BTC-PERPETUAL you get 8h granularity by default."""
        return self._call("public/get_funding_rate_history", {
            "instrument_name": instrument_name,
            "start_timestamp": start_ts_ms,
            "end_timestamp":   end_ts_ms,
        })

    def get_instrument(self, instrument_name: str) -> dict:
        return self._call("public/get_instrument",
                          {"instrument_name": instrument_name})

    def get_ticker(self, instrument_name: str) -> dict:
        return self._call("public/ticker",
                          {"instrument_name": instrument_name})

    # ----------------------------------------------------------
    # Private endpoints
    # ----------------------------------------------------------
    def get_positions(self, currency: str = "USDC",
                      kind: str | None = None) -> list[dict]:
        params = {"currency": currency}
        if kind:
            params["kind"] = kind
        return self._call("private/get_positions", params, auth=True)

    def get_account_summary(self, currency: str = "USDC") -> dict:
        return self._call("private/get_account_summary",
                          {"currency": currency, "extended": True}, auth=True)

    def market_order(self, instrument_name: str, side: str,
                     amount: float, label: str | None = None,
                     reduce_only: bool = False) -> dict:
        """Place a market order.

        side : "buy" | "sell"
        amount : units in the instrument's native size (contracts).
                 For Deribit USDC perps `amount` is contracts where 1
                 contract = $1 of underlying notional.  Caller is
                 responsible for quantizing to `min_trade_amount`.
        """
        side = side.lower()
        assert side in ("buy", "sell"), side
        params = {
            "instrument_name": instrument_name,
            "amount":          amount,
            "type":            "market",
        }
        if label:
            params["label"] = label[:64]
        if reduce_only:
            params["reduce_only"] = "true"
        return self._call(f"private/{side}", params, auth=True)
