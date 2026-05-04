"""
strategy.py
===========
Given today's signals + current open positions, decide what the *target*
state should be, and what actions are required to get there.

Per multi_asset_combined.py logic:

  * Each fund within an asset gets weight = 1 / N_funds_for_that_asset
    (4 for ETH, 5 for BTC).

  * Asset weight is determined per-snapshot:
        both assets fire     -> 0.5 / 0.5
        only one asset fires -> 1.0 for that asset, 0.0 for the other
        neither fires        -> nothing new opens

  * `asset_weight_at_entry` is captured on each position so it does NOT
    change for already-open positions when the other asset starts/stops
    firing later.  This matches the backtest semantics (each fund's hold
    is independent).

Three classes of action:
  OPEN    -- new fund just fired and we have no open position for it
  CLOSE   -- fund's hold has expired OR an exit signal triggered
  REFRESH -- fund's signal still firing while position is open (extend hold)
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import config
from signals import AssetSignals
import state as st


@dataclass
class Action:
    op:           str           # "OPEN" | "CLOSE" | "REFRESH"
    asset:        str           # "BTC" | "ETH"
    fund:         str
    instrument:   str
    direction:    int
    size_usd:     float
    reason:       str
    # Filled in by execution.py based on live spot:
    price_hint:   float | None  = None
    size_contracts: float | None = None


# ============================================================
# Allocation logic
# ============================================================
def asset_weights(btc_sig: AssetSignals,
                  eth_sig: AssetSignals) -> tuple[float, float]:
    """Returns (btc_weight, eth_weight) in [0, 1], summing to 0 or 1
    if neither fires, 1 if exactly one, 1 if both (split 50/50)."""
    btc_active = len(btc_sig.fired) > 0
    eth_active = len(eth_sig.fired) > 0
    if btc_active and eth_active:
        return 0.5, 0.5
    if btc_active:
        return 1.0, 0.0
    if eth_active:
        return 0.0, 1.0
    return 0.0, 0.0


def per_fund_size_usd(asset: str, asset_weight: float) -> float:
    """Per-fund USD slice for this asset given its asset-weight."""
    n_funds = (len(config.BTC_FUNDS) if asset == "BTC"
               else len(config.ETH_FUNDS))
    return config.CAPITAL_USD * asset_weight / n_funds


# ============================================================
# Exit signal helpers
# ============================================================
def _stop_loss_breached(pos: dict, current_price: float) -> bool:
    if not config.USE_STOP_LOSS:
        return False
    direction = pos["direction"]
    entry     = pos["entry_price"]
    pnl_pct   = direction * (current_price - entry) / entry
    return pnl_pct < -config.STOP_LOSS_PCT


def _signal_reversed(pos: dict, sig: AssetSignals) -> bool:
    """For a position that was opened on a z-score trigger, return True
    if the z has crossed back through 0 by more than REVERSAL_BUFFER.

    Only applied to skew_z signals (funding_z trades on BTC squeeze are
    short-window mean-reversion and exit naturally on hold expiry)."""
    if not config.USE_SIGNAL_REVERSAL:
        return False
    fund_cfg = (config.BTC_FUNDS if pos["asset"] == "BTC"
                else config.ETH_FUNDS).get(pos["fund"])
    if fund_cfg is None:
        return False
    col, op, _ = fund_cfg["trigger"]
    if col != "skew_z":
        return False
    z = sig.skew_z
    if z is None or z != z:   # NaN
        return False
    direction = pos["direction"]
    if direction < 0:        # short on high z -- exit when z drops
        return z < -config.REVERSAL_BUFFER
    else:                    # long on low z -- exit when z rises
        return z > +config.REVERSAL_BUFFER


# ============================================================
# Decision
# ============================================================
def decide(btc_sig: AssetSignals, eth_sig: AssetSignals,
           state: dict) -> list[Action]:
    """Return ordered list of actions to take.

    Order matters: we close before we open so that gross exposure is
    correctly re-sized when the asset-weight regime changes.
    """
    actions: list[Action] = []
    btc_w, eth_w = asset_weights(btc_sig, eth_sig)

    # ---------- 1. CLOSES -----------
    positions = state.get("positions", {})
    for key, pos in list(positions.items()):
        asset = pos["asset"]
        fund  = pos["fund"]
        sig   = btc_sig if asset == "BTC" else eth_sig
        cur_price = sig.spot
        reason = None
        if st.is_due_for_exit(pos):
            reason = "hold_expiry"
        elif _stop_loss_breached(pos, cur_price):
            reason = "stop_loss"
        elif _signal_reversed(pos, sig):
            reason = "signal_reversal"
        if reason:
            actions.append(Action(
                op="CLOSE",
                asset=asset,
                fund=fund,
                instrument=pos["instrument"],
                direction=pos["direction"],
                size_usd=pos["size_usd"],
                reason=reason,
                price_hint=cur_price,
            ))

    # Funds that we've just queued to close should NOT then be re-opened
    # in the same run (avoids open-close churn within seconds when the
    # same fund's signal is still firing).  We let the next run re-fire.
    closing_now = {(a.asset, a.fund) for a in actions if a.op == "CLOSE"}

    # ---------- 2. OPENS -----------
    for sig, funds, weight in [
        (btc_sig, config.BTC_FUNDS, btc_w),
        (eth_sig, config.ETH_FUNDS, eth_w),
    ]:
        if weight <= 0:
            continue
        slice_usd = per_fund_size_usd(sig.asset, weight)
        instrument = (config.PERP_BTC if sig.asset == "BTC"
                      else config.PERP_ETH)
        for fund, direction in sig.fired.items():
            if (sig.asset, fund) in closing_now:
                continue
            key = st.position_key(sig.asset, fund)
            if key in positions:
                # Already in position for this fund.  Refresh hold so the
                # exit clock resets to now + hold_days.
                actions.append(Action(
                    op="REFRESH",
                    asset=sig.asset,
                    fund=fund,
                    instrument=instrument,
                    direction=direction,
                    size_usd=positions[key]["size_usd"],
                    reason="signal_still_firing",
                    price_hint=sig.spot,
                ))
                continue
            actions.append(Action(
                op="OPEN",
                asset=sig.asset,
                fund=fund,
                instrument=instrument,
                direction=direction,
                size_usd=slice_usd,
                reason="signal_fire",
                price_hint=sig.spot,
            ))

    return actions


def explain_state(btc_sig: AssetSignals, eth_sig: AssetSignals,
                  state: dict) -> str:
    """Pretty-print summary for the run log."""
    btc_w, eth_w = asset_weights(btc_sig, eth_sig)
    open_pos = state.get("positions", {})
    lines = [
        f"  Signals  BTC skew_z={btc_sig.skew_z:+.2f}  "
        f"funding_z={btc_sig.funding_z:+.2f}  "
        f"ETH skew_z={eth_sig.skew_z:+.2f}",
        f"  Gates    btc_uptrend={btc_sig.btc_uptrend}  "
        f"eth_uptrend={eth_sig.eth_uptrend}",
        f"  Fires    BTC={list(btc_sig.fired)}  ETH={list(eth_sig.fired)}",
        f"  Weights  BTC={btc_w:.2f}  ETH={eth_w:.2f}  "
        f"(capital=${config.CAPITAL_USD:,.0f})",
        f"  Open     {len(open_pos)} positions in state file",
    ]
    return "\n".join(lines)
