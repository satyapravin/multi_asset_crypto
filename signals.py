"""
signals.py
==========
Reads the appended live history (surface + funding CSVs), computes the
SAME signals that multi_asset_combined.py uses, and evaluates each
fund's trigger.

Returns a `Signals` dataclass per asset with:
  * latest skew_z (and funding_z for BTC)
  * a `fired_funds` dict {fund_name -> +1/-1} for funds whose trigger
    is firing on the most recent row.

We DO NOT take the past row's signals -- we always look at the freshly
appended row (today).  Crypto is 24/7, so we enter immediately on a
fire (caller does that).
"""
from __future__ import annotations

import os
from dataclasses import dataclass

import numpy as np
import pandas as pd

import config


# ============================================================
# Winsorize  (copy of signal_transforms.winsorize_series so the
# deribit/ project has zero relative-import dependencies on ../BTC)
# ============================================================
def _winsorize(s: pd.Series, k: float = 6.0,
               window: int = 252, min_periods: int = 60) -> pd.Series:
    if k is None or k <= 0:
        return s.copy()
    med = s.rolling(window, min_periods=min_periods).median()
    abs_dev = (s - med).abs()
    mad = abs_dev.rolling(window, min_periods=min_periods).median()
    sigma = 1.4826 * mad
    return s.clip(lower=med - k * sigma, upper=med + k * sigma)


# ============================================================
# Signals dataclass
# ============================================================
@dataclass
class AssetSignals:
    asset:        str
    spot:         float
    skew_z:       float
    funding_z:    float | None
    btc_uptrend:  bool
    eth_uptrend:  bool
    near_hi:      bool
    fired:        dict[str, int]   # fund_name -> direction (+1/-1) if firing
    last_date:    pd.Timestamp


# ============================================================
# History loading + z-score
# ============================================================
def _load_surface(path: str) -> pd.DataFrame:
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"Missing {path}.  Run bootstrap.py first to seed warm-up data.")
    df = pd.read_csv(path, parse_dates=["date"])
    df["date"] = pd.to_datetime(df["date"]).dt.normalize()
    return df.sort_values("date").reset_index(drop=True)


def _load_funding(path: str) -> pd.DataFrame:
    if not os.path.exists(path):
        return pd.DataFrame(columns=["date", "funding_sum"])
    df = pd.read_csv(path, parse_dates=["date"])
    df["date"] = pd.to_datetime(df["date"]).dt.normalize()
    return df.sort_values("date").reset_index(drop=True)


def _compute_panel(surf: pd.DataFrame,
                   fund: pd.DataFrame | None,
                   btc_panel: pd.DataFrame | None) -> pd.DataFrame:
    """Build per-asset panel with z-scores + gates.  `btc_panel` is the
    BTC panel (used to attach the `btc_uptrend` cross-asset gate to ETH
    rows)."""
    df = surf[["date", "spot", "skew"]].copy()
    df["ret"] = df["spot"].pct_change()

    # ---- skew_z ----
    df["skew_w"] = _winsorize(df["skew"], k=config.WINSORIZE_K,
                              window=config.WINSORIZE_W,
                              min_periods=config.SKEW_MIN_OBS)
    mu = df["skew_w"].rolling(config.SKEW_LB,
                              min_periods=config.SKEW_MIN_OBS).mean()
    sd = df["skew_w"].rolling(config.SKEW_LB,
                              min_periods=config.SKEW_MIN_OBS).std()
    df["skew_z"] = (df["skew_w"] - mu) / sd

    # ---- funding_z (BTC only) ----
    if fund is not None and len(fund):
        df = df.merge(fund[["date", "funding_sum"]], on="date", how="left")
        df["funding_sum"] = df["funding_sum"].fillna(0.0)
        df["funding_w"] = _winsorize(df["funding_sum"], k=config.WINSORIZE_K,
                                     window=config.WINSORIZE_W,
                                     min_periods=config.FUND_MIN_OBS)
        mu = df["funding_w"].rolling(config.FUND_Z_LB,
                                     min_periods=config.FUND_MIN_OBS).mean()
        sd = df["funding_w"].rolling(config.FUND_Z_LB,
                                     min_periods=config.FUND_MIN_OBS).std()
        df["funding_z"] = (df["funding_w"] - mu) / sd
    else:
        df["funding_z"] = float("nan")

    # ---- gates ----
    df["sma50"]   = df["spot"].rolling(50).mean()
    df["uptrend"] = df["spot"] > df["sma50"]
    df["high20"]  = df["spot"].rolling(20).max()
    df["near_hi"] = df["spot"] >= df["high20"] * 0.99

    # ---- cross-asset btc_uptrend ----
    if btc_panel is not None and len(btc_panel):
        b = btc_panel[["date", "uptrend"]].rename(
            columns={"uptrend": "btc_uptrend"})
        df = df.merge(b, on="date", how="left")
        df["btc_uptrend"] = df["btc_uptrend"].fillna(False).astype(bool)
    else:
        df["btc_uptrend"] = df["uptrend"]
    return df


def _eval_trigger(row: pd.Series, trigger: tuple) -> bool:
    col, op, thresh = trigger
    val = row.get(col)
    if val is None or pd.isna(val):
        return False
    if   op == ">":  return bool(val >  thresh)
    elif op == "<":  return bool(val <  thresh)
    elif op == ">=": return bool(val >= thresh)
    elif op == "<=": return bool(val <= thresh)
    raise ValueError(op)


def _gate_passes(row: pd.Series, gate: str) -> bool:
    if gate == "any":     return True
    if gate == "uptrend": return bool(row.get("uptrend", False))
    if gate == "btc_up":  return bool(row.get("btc_uptrend", False))
    if gate == "near_hi": return bool(row.get("near_hi", False))
    raise ValueError(gate)


def _fired_funds(row: pd.Series, funds: dict[str, dict]) -> dict[str, int]:
    out = {}
    for name, cfg in funds.items():
        if (_eval_trigger(row, cfg["trigger"])
                and _gate_passes(row, cfg["gate"])):
            out[name] = int(cfg["direction"])
    return out


# ============================================================
# Public entry point
# ============================================================
def compute_all_signals() -> tuple[AssetSignals, AssetSignals,
                                   pd.DataFrame, pd.DataFrame]:
    """Read live histories, compute signals, return latest-row signals
    for both assets plus the full panels (for diagnostics / logging)."""
    btc_surf = _load_surface(config.BTC_SURF_LIVE)
    eth_surf = _load_surface(config.ETH_SURF_LIVE)
    fund     = _load_funding(config.FUND_LIVE)

    btc_panel = _compute_panel(btc_surf, fund, btc_panel=None)
    eth_panel = _compute_panel(eth_surf, fund=None, btc_panel=btc_panel)

    if btc_panel.empty or eth_panel.empty:
        raise RuntimeError("Empty panel after compute -- check warm-up data.")

    btc_row = btc_panel.iloc[-1]
    eth_row = eth_panel.iloc[-1]

    btc_sig = AssetSignals(
        asset="BTC",
        spot=float(btc_row["spot"]),
        skew_z=float(btc_row.get("skew_z", float("nan"))),
        funding_z=float(btc_row.get("funding_z", float("nan"))),
        btc_uptrend=bool(btc_row.get("btc_uptrend", False)),
        eth_uptrend=bool(eth_row.get("uptrend", False)),
        near_hi=bool(btc_row.get("near_hi", False)),
        fired=_fired_funds(btc_row, config.BTC_FUNDS),
        last_date=pd.Timestamp(btc_row["date"]),
    )
    eth_sig = AssetSignals(
        asset="ETH",
        spot=float(eth_row["spot"]),
        skew_z=float(eth_row.get("skew_z", float("nan"))),
        funding_z=None,
        btc_uptrend=bool(eth_row.get("btc_uptrend", False)),
        eth_uptrend=bool(eth_row.get("uptrend", False)),
        near_hi=bool(eth_row.get("near_hi", False)),
        fired=_fired_funds(eth_row, config.ETH_FUNDS),
        last_date=pd.Timestamp(eth_row["date"]),
    )
    return btc_sig, eth_sig, btc_panel, eth_panel
