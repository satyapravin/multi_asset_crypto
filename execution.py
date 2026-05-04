"""
execution.py
============
Translate `Action` objects from strategy.py into either Deribit REST
calls (live mode) or simulated fills (paper mode).

Sizing on Deribit USDC perps
----------------------------
BTC_USDC-PERPETUAL and ETH_USDC-PERPETUAL are LINEAR futures, so per
Deribit docs the `amount` parameter is in BASE CURRENCY (BTC, ETH),
NOT USD or "contracts".  Convert size_usd -> size_native via the
current spot price, then quantize down to `min_trade_amount`
(0.0001 BTC, 0.001 ETH).

Reduce-only on close orders
---------------------------
We always send `reduce_only=true` on CLOSE actions so we cannot
accidentally flip the position.

Modes
-----
  paper + DRY_RUN=False : print + simulate fill at spot * (1 +- slip)
                          + persist to state + log trade
  paper + DRY_RUN=True  : print only, no persist
  live  + DRY_RUN=False : real market order on Deribit
  live  + DRY_RUN=True  : print would-be order, no send, no persist
"""
from __future__ import annotations

import math
from typing import Optional

import config
import state as st
from deribit_api import DeribitClient
from strategy import Action


# Cache instrument specs per run to avoid duplicate REST calls.
_INSTRUMENT_CACHE: dict[str, dict] = {}


def _instrument_spec(client: DeribitClient, name: str) -> dict:
    if name not in _INSTRUMENT_CACHE:
        _INSTRUMENT_CACHE[name] = client.get_instrument(name)
    return _INSTRUMENT_CACHE[name]


def _quantize_amount(client: DeribitClient, instrument: str,
                     size_usd: float, spot_price: float) -> float:
    """Convert `size_usd` to a base-currency amount (BTC, ETH) and round
    DOWN to the instrument's `min_trade_amount`.

    Returns 0.0 if the rounded amount is below the exchange minimum --
    caller should skip the order in that case.

    For LINEAR perps (which both BTC_USDC and ETH_USDC are), Deribit
    expects the `amount` parameter in BASE CURRENCY units, not USD and
    not "contracts".
    """
    if spot_price <= 0:
        return 0.0
    try:
        spec = _instrument_spec(client, instrument)
        step = float(spec.get("min_trade_amount", 0.0001))
    except Exception as e:
        print(f"  [warn] get_instrument({instrument}) failed: {e}; "
              f"using conservative min_trade_amount=0.0001")
        step = 0.0001
    amount_native = size_usd / spot_price
    rounded = math.floor(amount_native / step) * step
    return rounded


def _simulated_fill(spot: float, side: str) -> float:
    """Apply slippage to spot for paper-mode fills.  Buys pay up,
    sells get hit down."""
    bps = config.SLIPPAGE_BPS / 10_000.0
    return spot * (1 + bps) if side == "buy" else spot * (1 - bps)


def _hold_days_for(asset: str, fund: str) -> int:
    funds = config.BTC_FUNDS if asset == "BTC" else config.ETH_FUNDS
    return int(funds[fund]["hold_days"])


# ============================================================
# Per-action handlers
# ============================================================
def _do_open(client: DeribitClient, act: Action, state: dict,
             skew_z: float | None, funding_z: float | None) -> None:
    side = "buy" if act.direction == +1 else "sell"
    spot = act.price_hint or 0.0
    qty_native = _quantize_amount(client, act.instrument, act.size_usd, spot)
    if qty_native <= 0:
        print(f"  [skip] OPEN {act.asset}/{act.fund}: size {act.size_usd:.2f} "
              f"USD @ ${spot:,.2f} rounds to 0 (below min_trade_amount)")
        return
    label = f"{act.asset}_{act.fund}"[:64]
    sym = "BTC" if act.asset == "BTC" else "ETH"
    print(f"  OPEN  {act.asset}/{act.fund:<16s}  {side.upper()} "
          f"{qty_native:.4f} {sym}  ~${act.size_usd:,.0f}  @~${spot:,.2f}  "
          f"({act.reason})")

    # ---- determine fill price by mode ----
    if config.IS_LIVE and not config.DRY_RUN:
        resp = client.market_order(act.instrument, side, qty_native,
                                   label=label, reduce_only=False)
        fill_price = float(
            resp.get("order", {}).get("average_price")
            or (resp.get("trades") or [{}])[0].get("price")
            or spot
        )
    else:
        # Paper (or live --dry-run): simulate a fill at spot +/- slippage.
        fill_price = _simulated_fill(spot, side)

    if config.DRY_RUN:
        print(f"        [dry-run] no state update, no order sent")
        return

    n_funds = (len(config.BTC_FUNDS) if act.asset == "BTC"
               else len(config.ETH_FUNDS))
    asset_w = act.size_usd / (config.CAPITAL_USD / n_funds) if config.CAPITAL_USD else 1.0
    st.add_position(state,
                    asset=act.asset, fund=act.fund,
                    instrument=act.instrument,
                    direction=act.direction,
                    size_contracts=qty_native,
                    size_usd=act.size_usd,
                    entry_price=fill_price,
                    hold_days=_hold_days_for(act.asset, act.fund),
                    asset_weight_at_entry=asset_w)
    st.log_trade("OPEN", act.asset, act.fund, act.instrument,
                 act.direction, qty_native, act.size_usd, fill_price,
                 act.reason, config.DRY_RUN, skew_z, funding_z)


def _do_close(client: DeribitClient, act: Action, state: dict,
              skew_z: float | None, funding_z: float | None) -> None:
    pos = state.get("positions", {}).get(st.position_key(act.asset, act.fund))
    if pos is None:
        print(f"  [warn] CLOSE for {act.asset}/{act.fund}: no state entry")
        return
    qty_native = float(pos["size_contracts"])   # base-currency units
    side = "sell" if pos["direction"] == +1 else "buy"   # opposite of entry
    label = f"{act.asset}_{act.fund}_close"[:64]
    spot = act.price_hint or 0.0
    sym = "BTC" if act.asset == "BTC" else "ETH"
    print(f"  CLOSE {act.asset}/{act.fund:<16s}  {side.upper()} "
          f"{qty_native:.4f} {sym}  ~${pos['size_usd']:,.0f}  @~${spot:,.2f}  "
          f"({act.reason})")

    if config.IS_LIVE and not config.DRY_RUN:
        resp = client.market_order(act.instrument, side, qty_native,
                                   label=label, reduce_only=True)
        fill_price = float(
            resp.get("order", {}).get("average_price")
            or (resp.get("trades") or [{}])[0].get("price")
            or spot
        )
    else:
        fill_price = _simulated_fill(spot, side)

    if config.DRY_RUN:
        print(f"        [dry-run] no state update, no order sent")
        return

    # Realize PnL on close (for the trade log) -- accounting fees/slip
    pnl_gross = pos["direction"] * (fill_price - pos["entry_price"]) \
                * pos["size_usd"] / pos["entry_price"]
    fee_total = 2 * pos["size_usd"] * config.FEE_BPS / 10_000.0
    pnl_net   = pnl_gross - fee_total

    st.remove_position(state, act.asset, act.fund)
    st.log_trade("CLOSE", act.asset, act.fund, act.instrument,
                 -pos["direction"], qty_native, pos["size_usd"], fill_price,
                 f"{act.reason} pnl_net={pnl_net:+.2f}USD",
                 config.DRY_RUN, skew_z, funding_z)


def _do_refresh(client: DeribitClient, act: Action, state: dict,
                skew_z: float | None, funding_z: float | None) -> None:
    print(f"  REFR  {act.asset}/{act.fund:<16s}  hold extended "
          f"by {_hold_days_for(act.asset, act.fund)}d  ({act.reason})")
    if config.DRY_RUN:
        return
    st.refresh_hold(state, act.asset, act.fund,
                    _hold_days_for(act.asset, act.fund))
    st.log_trade("REFRESH", act.asset, act.fund, act.instrument,
                 act.direction, 0.0, 0.0, act.price_hint or 0.0,
                 act.reason, config.DRY_RUN, skew_z, funding_z)


# ============================================================
# Public entry point
# ============================================================
def execute(client: DeribitClient, actions: list[Action], state: dict,
            skew_z_btc: float | None = None,
            skew_z_eth: float | None = None,
            funding_z: float | None = None) -> None:
    """Run all actions; CLOSE first, REFRESH next, OPEN last (so gross is
    only briefly elevated)."""
    if not actions:
        print("  (no actions)")
        return
    order = {"CLOSE": 0, "REFRESH": 1, "OPEN": 2}
    actions = sorted(actions, key=lambda a: order.get(a.op, 99))

    mode_label = "DRY-RUN" if config.DRY_RUN else (
        "PAPER" if config.IS_PAPER else "LIVE")
    print(f"  [{mode_label}] processing {len(actions)} actions:")
    for act in actions:
        skew_z = skew_z_btc if act.asset == "BTC" else skew_z_eth
        try:
            if act.op == "OPEN":
                _do_open(client, act, state, skew_z, funding_z)
            elif act.op == "CLOSE":
                _do_close(client, act, state, skew_z, funding_z)
            elif act.op == "REFRESH":
                _do_refresh(client, act, state, skew_z, funding_z)
        except Exception as e:
            print(f"  [ERROR] {act.op} {act.asset}/{act.fund}: {e}")


# ============================================================
# Sanity check: state vs Deribit
# ============================================================
def reconcile_with_exchange(client: DeribitClient, state: dict) -> None:
    """Print warnings if our state file disagrees with what Deribit shows
    as actually held.  We never auto-correct -- safer to surface the
    delta and let the operator decide.  In paper mode there are no real
    positions so we skip entirely."""
    if config.IS_PAPER:
        return
    try:
        positions = client.get_positions(currency=config.PERP_CCY)
    except Exception as e:
        print(f"  [warn] Could not fetch positions for reconcile: {e}")
        return
    on_exchange: dict[str, float] = {}
    for p in positions:
        instr = p.get("instrument_name")
        if instr in (config.PERP_BTC, config.PERP_ETH):
            on_exchange[instr] = on_exchange.get(instr, 0.0) + float(
                p.get("size", 0.0))
    expected: dict[str, float] = {}
    for pos in state.get("positions", {}).values():
        instr = pos["instrument"]
        signed = pos["direction"] * float(pos["size_contracts"])
        expected[instr] = expected.get(instr, 0.0) + signed
    for instr in {*on_exchange, *expected}:
        actual = on_exchange.get(instr, 0.0)
        want   = expected.get(instr, 0.0)
        if abs(actual - want) > 0.5:
            print(f"  [reconcile] {instr}: state expects {want:+.1f} ctr, "
                  f"Deribit shows {actual:+.1f} ctr  (Δ={actual-want:+.1f})")
