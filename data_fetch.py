"""
data_fetch.py
=============
Single source of truth for "what the world looks like right now":

  * Today's BTC and ETH option-surface row (skew, atm_iv, butterfly, ts_ratio)
  * Today's BTC perpetual funding figure (8h period sum mapped to 1-day)
  * Latest spot for both indices
  * BTC trend gate state (close > 50d sma)

We compute the surface ourselves from Deribit's `get_book_summary_by_currency`
output rather than relying on Deribit's own DVOL, so the columns match
the BTC/ETH_surface.csv files exactly.

The output of `snapshot()` is appended to BTC_surface_live.csv /
ETH_surface_live.csv / btc_funding_live.csv (idempotent: if today's date
already exists it gets overwritten).
"""
from __future__ import annotations

import datetime as dt
import math
import os
from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

import config
from deribit_api import DeribitClient, DeribitError


# ============================================================
# Surface row computation
# ============================================================
@dataclass
class SurfaceRow:
    date:        pd.Timestamp
    spot:        float
    dte1:        int
    atm_iv:      float
    atm_iv_w2:   Optional[float]
    ts_ratio:    Optional[float]
    iv_25p:      float
    iv_25c:      float
    K_25p:       float
    K_25c:       float
    skew:        float
    butterfly:   float


def _interp_iv_at_delta(rows: pd.DataFrame, target_delta: float
                        ) -> tuple[float, float]:
    """Linear-interp IV at target delta along (delta, iv) for one expiry/side."""
    if len(rows) < 2:
        return float("nan"), float("nan")
    rows = rows.sort_values("delta")
    deltas = rows["delta"].values
    ivs    = rows["iv"].values
    Ks     = rows["strike"].values
    if target_delta < deltas.min() or target_delta > deltas.max():
        return float("nan"), float("nan")
    return (float(np.interp(target_delta, deltas, ivs)),
            float(np.interp(target_delta, deltas, Ks)))


def _parse_instrument(name: str) -> dict | None:
    """Decode 'BTC-13MAY26-95000-C' style into {expiry, strike, side}."""
    parts = name.split("-")
    if len(parts) != 4:
        return None
    _ccy, exp_str, strike_str, side = parts
    try:
        expiry = dt.datetime.strptime(exp_str, "%d%b%y").date()
        strike = float(strike_str)
    except ValueError:
        return None
    if side not in ("C", "P"):
        return None
    return {"expiry": expiry, "strike": strike, "side": side}


def build_surface_row(book: list[dict], spot: float,
                      today: pd.Timestamp) -> SurfaceRow | None:
    """Compute one row of (skew, butterfly, ts_ratio, atm_iv, ...) from a
    Deribit book-summary response.  Mirrors the convention used in
    btc_option_skew.py compute_surface() so the live data is a drop-in
    replacement for the historical CSV."""
    today_d = today.date()
    rows = []
    for r in book:
        meta = _parse_instrument(r["instrument_name"])
        if meta is None:
            continue
        # Deribit returns mark_iv as percent (e.g. 65.5 = 65.5%).  We store
        # decimals (0.655) to match the historical CSVs.
        miv = r.get("mark_iv")
        if miv is None or miv <= 0:
            continue
        dte = (meta["expiry"] - today_d).days
        if dte <= 0:
            continue
        rows.append({
            "expiry":  meta["expiry"],
            "strike":  meta["strike"],
            "side":    meta["side"],   # 'C' or 'P'
            "iv":      float(miv) / 100.0,
            "dte":     dte,
        })
    if not rows:
        return None
    df = pd.DataFrame(rows)

    # Pick "front weekly" = nearest expiry with at least 3 strikes both sides.
    expiries = sorted(df["expiry"].unique())
    e1 = None
    for cand in expiries:
        sub = df[df["expiry"] == cand]
        n_call = (sub["side"] == "C").sum()
        n_put  = (sub["side"] == "P").sum()
        if n_call >= 3 and n_put >= 3:
            e1 = cand
            break
    if e1 is None:
        return None

    chain = df[df["expiry"] == e1].copy()
    T1 = (e1 - today_d).days / 365.0

    # Compute Black-Scholes delta from spot & IV (r=0, q=0).
    chain["delta"] = chain.apply(
        lambda row: _bs_delta(spot, row["strike"], T1, row["iv"], row["side"]),
        axis=1,
    )
    chain = chain[~chain["delta"].isna()].copy()

    # ATM IV: average call+put at strike closest to spot
    nearest_K = chain.iloc[(chain["strike"] - spot).abs().argsort()[:1]
                           ]["strike"].iloc[0]
    atm_chain = chain[chain["strike"] == nearest_K]
    if len(atm_chain) == 0:
        return None
    atm_iv = float(atm_chain["iv"].mean())

    # 25-delta wings (only OTM puts have delta < 0; calls have delta > 0)
    puts  = chain[chain["side"] == "P"]
    calls = chain[chain["side"] == "C"]
    iv_25p, K_25p = _interp_iv_at_delta(puts,  -0.25)
    iv_25c, K_25c = _interp_iv_at_delta(calls, +0.25)
    if math.isnan(iv_25p) or math.isnan(iv_25c):
        return None
    skew = iv_25p - iv_25c
    bfly = (iv_25p + iv_25c) / 2.0 - atm_iv

    # Term structure: next weekly ATM IV
    atm_iv_w2 = None
    ts_ratio  = None
    later = [e for e in expiries if e > e1]
    if later:
        e2 = later[0]
        c2 = df[df["expiry"] == e2]
        if len(c2):
            nearest_K2 = c2.iloc[(c2["strike"] - spot).abs().argsort()[:1]
                                 ]["strike"].iloc[0]
            atm2 = c2[c2["strike"] == nearest_K2]
            if len(atm2):
                atm_iv_w2 = float(atm2["iv"].mean())
                if atm_iv > 0:
                    ts_ratio = atm_iv_w2 / atm_iv

    return SurfaceRow(
        date=today.normalize(),
        spot=spot,
        dte1=(e1 - today_d).days,
        atm_iv=atm_iv,
        atm_iv_w2=atm_iv_w2,
        ts_ratio=ts_ratio,
        iv_25p=iv_25p,
        iv_25c=iv_25c,
        K_25p=K_25p,
        K_25c=K_25c,
        skew=skew,
        butterfly=bfly,
    )


def _bs_delta(S: float, K: float, T: float, sigma: float, side: str) -> float:
    """Black-Scholes delta with r=0, q=0.  side: 'C' or 'P'."""
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return float("nan")
    try:
        d1 = (math.log(S / K) + 0.5 * sigma * sigma * T) / (sigma * math.sqrt(T))
        from scipy.stats import norm
        cdf = norm.cdf(d1)
        return cdf if side == "C" else cdf - 1.0
    except Exception:
        return float("nan")


# ============================================================
# Funding fetch
# ============================================================
def fetch_btc_funding_today(client: DeribitClient,
                            today: pd.Timestamp) -> float:
    """Sum of 8h funding rates over the trailing 24h ending at `today`.
    Matches the daily aggregation in BTC/fetch_funding.py."""
    end_ms   = int(today.normalize().timestamp() * 1000) + 86_400_000 - 1
    start_ms = end_ms - 86_400_000 + 1
    try:
        # The PERPETUAL legacy contract is the canonical funding source
        rows = client.get_funding_rate_history("BTC-PERPETUAL", start_ms, end_ms)
    except DeribitError:
        return 0.0
    if not rows:
        return 0.0
    # Each row: {timestamp, interest_8h, ...}.  The 8h fields differ across
    # firmware versions; we just take the first numeric one.
    keys = ["interest_8h", "8h_interest", "funding_8h", "rate"]
    total = 0.0
    for r in rows:
        v = next((float(r[k]) for k in keys if k in r and r[k] is not None), 0.0)
        total += v
    return float(total)


# ============================================================
# Top-level snapshot
# ============================================================
@dataclass
class Snapshot:
    timestamp:    pd.Timestamp
    btc_spot:     float
    eth_spot:     float
    btc_surface:  SurfaceRow | None
    eth_surface:  SurfaceRow | None
    btc_funding_sum: float

    def summary(self) -> str:
        bs = self.btc_surface
        es = self.eth_surface
        return (f"BTC ${self.btc_spot:,.0f}  ETH ${self.eth_spot:,.0f}  "
                f"BTC skew={bs.skew:+.4f} atm_iv={bs.atm_iv:.2%}  "
                f"ETH skew={es.skew:+.4f} atm_iv={es.atm_iv:.2%}  "
                f"BTC funding24h={self.btc_funding_sum*1e4:+.2f}bp"
                if (bs and es) else
                f"BTC ${self.btc_spot:,.0f}  ETH ${self.eth_spot:,.0f}  "
                f"(surface incomplete)")


def take_snapshot(client: DeribitClient,
                  today: pd.Timestamp | None = None) -> Snapshot:
    """End-to-end: fetch spot + option chains + funding, build surfaces."""
    if today is None:
        today = pd.Timestamp.utcnow().tz_localize(None)

    btc_spot = client.get_index_price(config.INDEX_BTC)
    eth_spot = client.get_index_price(config.INDEX_ETH)

    btc_book = client.get_book_summary_by_currency(config.OPT_CCY_BTC)
    eth_book = client.get_book_summary_by_currency(config.OPT_CCY_ETH)

    btc_surf = build_surface_row(btc_book, btc_spot, today)
    eth_surf = build_surface_row(eth_book, eth_spot, today)
    btc_fund = fetch_btc_funding_today(client, today)

    return Snapshot(
        timestamp=today,
        btc_spot=btc_spot,
        eth_spot=eth_spot,
        btc_surface=btc_surf,
        eth_surface=eth_surf,
        btc_funding_sum=btc_fund,
    )


# ============================================================
# Persistence -- append / overwrite-by-date
# ============================================================
SURF_COLS = ["date", "spot", "dte1", "atm_iv", "atm_iv_w2", "ts_ratio",
             "iv_25p", "iv_25c", "K_25p", "K_25c", "skew", "butterfly"]


def _surface_row_to_dict(s: SurfaceRow) -> dict:
    return {c: getattr(s, c) for c in SURF_COLS}


def append_surface(path: str, surface: SurfaceRow) -> None:
    """Append (or overwrite by date) one row to a surface CSV."""
    new = pd.DataFrame([_surface_row_to_dict(surface)])
    if os.path.exists(path):
        cur = pd.read_csv(path, parse_dates=["date"])
        cur["date"] = pd.to_datetime(cur["date"]).dt.normalize()
        cur = cur[cur["date"] != surface.date]
        out = pd.concat([cur, new], ignore_index=True)
    else:
        out = new
    out = out[SURF_COLS].sort_values("date")
    out.to_csv(path, index=False)


def append_funding(path: str, date: pd.Timestamp,
                   funding_sum: float) -> None:
    new = pd.DataFrame([{
        "date":            date.normalize(),
        "funding_sum":     funding_sum,
        "funding_mean":    funding_sum / 3.0,   # 3 periods/day
        "funding_periods": 3,
    }])
    if os.path.exists(path):
        cur = pd.read_csv(path, parse_dates=["date"])
        cur["date"] = pd.to_datetime(cur["date"]).dt.normalize()
        cur = cur[cur["date"] != date.normalize()]
        out = pd.concat([cur, new], ignore_index=True)
    else:
        out = new
    out = out.sort_values("date")
    out.to_csv(path, index=False)


def persist_snapshot(snap: Snapshot) -> None:
    if snap.btc_surface is not None:
        append_surface(config.BTC_SURF_LIVE, snap.btc_surface)
    if snap.eth_surface is not None:
        append_surface(config.ETH_SURF_LIVE, snap.eth_surface)
    append_funding(config.FUND_LIVE, snap.timestamp, snap.btc_funding_sum)
