"""
paper_equity.py
===============
Mark-to-market accounting for the paper book.  Called once per main.py
run (after execute) when MODE=paper.

We track three streams:

  realized_cum   running total of net PnL on closed positions (USD)
  unrealized     mark-to-market PnL of currently open positions (USD)
  equity         CAPITAL_USD + realized_cum + unrealized (USD)

These are all in USD.  Per-position PnL accounting:

  size_qty       = pos.size_usd / pos.entry_price       (in BTC or ETH)
  pnl_gross      = pos.direction * (current_price - entry_price) * size_qty
  fee_open       = pos.size_usd * FEE_BPS  / 10_000
  fee_close      = pos.size_usd * FEE_BPS  / 10_000     (only on close)
  slippage_open  = pos.size_usd * SLIPPAGE_BPS / 10_000 (already baked into entry_price)
  slippage_close = ... (baked into exit_price at close time)

So while the position is OPEN we charge `fee_open` (the close fee will
be charged on close), and the price-impact slippage is already in
entry_price (set in execution._simulated_fill()).

CSV layout (`equity_paper.csv`):
  timestamp,btc_spot,eth_spot,n_open,realized_cum,unrealized,equity,
  open_btc_usd,open_eth_usd
"""
from __future__ import annotations

import csv
import datetime as dt
import os
from typing import Iterable

import pandas as pd

import config
import state as st


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _realized_cum_from_log() -> float:
    """Sum the net PnL from every CLOSE row in the trade log.

    `state.log_trade` writes the realized PnL into the `reason` field as
    ' pnl_net=<float>USD' on close rows; we parse it back out so we
    don't need a second source of truth."""
    if not os.path.exists(config.TRADE_LOG):
        return 0.0
    df = pd.read_csv(config.TRADE_LOG)
    closes = df[df["action"] == "CLOSE"]
    if len(closes) == 0:
        return 0.0
    pnl_total = 0.0
    for reason in closes["reason"].astype(str):
        # reason example: "hold_expiry pnl_net=+12.34USD"
        if "pnl_net=" not in reason:
            continue
        try:
            tail = reason.split("pnl_net=")[1]
            num  = tail.split("USD")[0]
            pnl_total += float(num)
        except (ValueError, IndexError):
            continue
    return pnl_total


def _unrealized_for_position(pos: dict, current_price: float) -> float:
    """MTM PnL for one open position, net of the entry-side fee."""
    if current_price <= 0 or pos["entry_price"] <= 0:
        return 0.0
    qty       = pos["size_usd"] / pos["entry_price"]
    pnl_gross = pos["direction"] * (current_price - pos["entry_price"]) * qty
    fee_open  = pos["size_usd"] * config.FEE_BPS / 10_000.0
    return pnl_gross - fee_open


def update_equity(state: dict, btc_spot: float, eth_spot: float) -> dict:
    """Compute MTM, append a row to equity_paper.csv, return the snapshot
    so main.py can print a one-line summary."""
    realized = _realized_cum_from_log()

    unreal = 0.0
    open_btc_usd = 0.0
    open_eth_usd = 0.0
    for pos in state.get("positions", {}).values():
        spot = btc_spot if pos["asset"] == "BTC" else eth_spot
        unreal += _unrealized_for_position(pos, spot)
        signed_usd = pos["direction"] * pos["size_usd"]
        if pos["asset"] == "BTC":
            open_btc_usd += signed_usd
        else:
            open_eth_usd += signed_usd

    equity = config.CAPITAL_USD + realized + unreal
    n_open = len(state.get("positions", {}))

    row = {
        "timestamp":     _now_iso(),
        "btc_spot":      round(btc_spot, 2),
        "eth_spot":      round(eth_spot, 2),
        "n_open":        n_open,
        "realized_cum":  round(realized, 4),
        "unrealized":    round(unreal,   4),
        "equity":        round(equity,   4),
        "open_btc_usd":  round(open_btc_usd, 2),
        "open_eth_usd":  round(open_eth_usd, 2),
    }
    cols = list(row.keys())
    new_file = not os.path.exists(config.EQUITY_CSV)
    with open(config.EQUITY_CSV, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        if new_file:
            w.writeheader()
        w.writerow(row)
    return row


def one_line_summary(snapshot: dict) -> str:
    pct = (snapshot["equity"] / config.CAPITAL_USD - 1) * 100
    return (f"  Paper book   equity=${snapshot['equity']:,.2f} "
            f"({pct:+.2f}%)   "
            f"realized=${snapshot['realized_cum']:+,.2f}   "
            f"unrealized=${snapshot['unrealized']:+,.2f}   "
            f"open={snapshot['n_open']}   "
            f"net_BTC=${snapshot['open_btc_usd']:+,.0f}   "
            f"net_ETH=${snapshot['open_eth_usd']:+,.0f}")
