"""
state.py
========
JSON-backed persistent state for the live deployment.

State shape (positions_state.json):
{
  "last_run_ts":  "2026-05-04T19:00:00Z",
  "positions": {
     "<asset>_<fund>":  {
        "asset":            "BTC" | "ETH",
        "fund":             "skew_short_xhi",
        "instrument":       "BTC_USDC-PERPETUAL",
        "direction":        +1 | -1,
        "size_contracts":   24.0,
        "size_usd":         24.0,
        "entry_price":      95234.5,
        "entry_ts":         "2026-05-04T19:00:00Z",
        "exit_due_ts":      "2026-05-05T19:00:00Z",
        "asset_weight_at_entry": 0.5
     }
  }
}

We key on f"{asset}_{fund}" so each fund-leg is independent and the
multi-fund book aggregates by summing position sizes when placing
orders (handled in execution.py).
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import os
from typing import Any

import config


def _utc_iso(ts: dt.datetime | None = None) -> str:
    if ts is None:
        ts = dt.datetime.now(dt.timezone.utc)
    elif ts.tzinfo is None:
        ts = ts.replace(tzinfo=dt.timezone.utc)
    return ts.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s: str) -> dt.datetime:
    return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=dt.timezone.utc)


# ============================================================
# Read / write
# ============================================================
def load_state() -> dict[str, Any]:
    if not os.path.exists(config.STATE_FILE):
        return {"last_run_ts": None, "positions": {}}
    with open(config.STATE_FILE, "r") as f:
        return json.load(f)


def save_state(state: dict[str, Any]) -> None:
    state["last_run_ts"] = _utc_iso()
    tmp = config.STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)
    os.replace(tmp, config.STATE_FILE)


# ============================================================
# Position helpers
# ============================================================
def position_key(asset: str, fund: str) -> str:
    return f"{asset}_{fund}"


def add_position(state: dict, asset: str, fund: str, instrument: str,
                 direction: int, size_contracts: float, size_usd: float,
                 entry_price: float, hold_days: int,
                 asset_weight_at_entry: float) -> dict:
    now = dt.datetime.now(dt.timezone.utc)
    exit_due = now + dt.timedelta(seconds=hold_days * config.HOLD_DAY_SECONDS)
    pos = {
        "asset":                    asset,
        "fund":                     fund,
        "instrument":               instrument,
        "direction":                int(direction),
        "size_contracts":           float(size_contracts),
        "size_usd":                 float(size_usd),
        "entry_price":              float(entry_price),
        "entry_ts":                 _utc_iso(now),
        "exit_due_ts":              _utc_iso(exit_due),
        "asset_weight_at_entry":    float(asset_weight_at_entry),
    }
    state.setdefault("positions", {})[position_key(asset, fund)] = pos
    return pos


def remove_position(state: dict, asset: str, fund: str) -> dict | None:
    return state.get("positions", {}).pop(position_key(asset, fund), None)


def refresh_hold(state: dict, asset: str, fund: str, hold_days: int) -> None:
    """Extend `exit_due_ts` to now + hold_days (used when a fund's signal
    fires again while the position is still held -- backtest convention)."""
    pos = state.get("positions", {}).get(position_key(asset, fund))
    if pos is None:
        return
    new_exit = dt.datetime.now(dt.timezone.utc) + dt.timedelta(
        seconds=hold_days * config.HOLD_DAY_SECONDS)
    pos["exit_due_ts"] = _utc_iso(new_exit)


def is_due_for_exit(pos: dict) -> bool:
    return dt.datetime.now(dt.timezone.utc) >= parse_iso(pos["exit_due_ts"])


# ============================================================
# Trade log (CSV append-only)
# ============================================================
TRADE_LOG_COLS = [
    "timestamp", "action", "asset", "fund", "instrument", "direction",
    "size_contracts", "size_usd", "price", "reason", "dry_run",
    "skew_z", "funding_z",
]


def log_trade(action: str, asset: str, fund: str, instrument: str,
              direction: int, size_contracts: float, size_usd: float,
              price: float, reason: str, dry_run: bool,
              skew_z: float | None = None,
              funding_z: float | None = None) -> None:
    row = {
        "timestamp":      _utc_iso(),
        "action":         action,           # "OPEN" | "CLOSE" | "REFRESH"
        "asset":          asset,
        "fund":           fund,
        "instrument":     instrument,
        "direction":      int(direction),
        "size_contracts": float(size_contracts),
        "size_usd":       float(size_usd),
        "price":          float(price),
        "reason":         reason,           # "signal_fire" | "hold_expiry" | "stop_loss" | ...
        "dry_run":        bool(dry_run),
        "skew_z":         "" if skew_z    is None else f"{skew_z:.4f}",
        "funding_z":      "" if funding_z is None else f"{funding_z:.4f}",
    }
    new_file = not os.path.exists(config.TRADE_LOG)
    with open(config.TRADE_LOG, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=TRADE_LOG_COLS)
        if new_file:
            w.writeheader()
        w.writerow(row)
