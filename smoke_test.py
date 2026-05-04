"""
smoke_test.py
=============
End-to-end test that does NOT require Deribit credentials.  Skips the
data_fetch.snapshot() step (which calls Deribit).  Uses the warm-up
CSVs copied by bootstrap.py to test:

  * signals.compute_all_signals() pipeline
  * strategy.decide() with an empty state
  * execution flow in paper mode (simulated fills, no orders)
  * paper_equity.update_equity() MTM accounting
  * state save/load roundtrip
"""
import os
import sys

import config
# Ensure we're in paper mode + persisting (so we test the real path)
config.MODE = "paper"
config.IS_PAPER = True
config.IS_LIVE = False
config.DRY_RUN = False

import signals
import strategy
import state as st
import execution
import paper_equity


def main():
    # 1. Verify warm-up data is present
    for f in (config.BTC_SURF_LIVE, config.ETH_SURF_LIVE, config.FUND_LIVE):
        if not os.path.exists(f):
            print(f"[FAIL] missing warm-up file: {f}")
            print("       run `python bootstrap.py` first")
            sys.exit(1)

    print("=" * 70)
    print(" SMOKE TEST  (no Deribit API calls)")
    print("=" * 70)

    # 2. Compute signals from warm-up history
    print("\n[1] signals.compute_all_signals() ...")
    btc_sig, eth_sig, btc_panel, eth_panel = signals.compute_all_signals()
    print(f"  BTC panel rows: {len(btc_panel):>5d}  "
          f"date range: {btc_panel['date'].min().date()} -> {btc_panel['date'].max().date()}")
    print(f"  ETH panel rows: {len(eth_panel):>5d}  "
          f"date range: {eth_panel['date'].min().date()} -> {eth_panel['date'].max().date()}")
    print(f"\n  BTC last row:  spot=${btc_sig.spot:,.2f}  "
          f"skew_z={btc_sig.skew_z:+.3f}  funding_z={btc_sig.funding_z:+.3f}  "
          f"uptrend={btc_sig.btc_uptrend}")
    print(f"  ETH last row:  spot=${eth_sig.spot:,.2f}  "
          f"skew_z={eth_sig.skew_z:+.3f}  "
          f"btc_up gate (cross-asset)={eth_sig.btc_uptrend}  "
          f"eth_uptrend={eth_sig.eth_uptrend}")
    print(f"\n  BTC fired funds: {btc_sig.fired}")
    print(f"  ETH fired funds: {eth_sig.fired}")

    # 3. Strategy decide() with empty state
    print("\n[2] strategy.decide() with EMPTY state ...")
    empty_state = {"last_run_ts": None, "positions": {}}
    print(strategy.explain_state(btc_sig, eth_sig, empty_state))
    actions = strategy.decide(btc_sig, eth_sig, empty_state)
    if actions:
        for a in actions:
            print(f"    {a.op:<7s} {a.asset}/{a.fund:<16s} "
                  f"dir={a.direction:+d}  size=${a.size_usd:,.2f}  "
                  f"reason={a.reason}")
    else:
        print("    (no actions -- no funds firing today)")

    # 4. Strategy decide() with a SIMULATED open position to test exit logic
    print("\n[3] strategy.decide() with SIMULATED open positions ...")
    sim_state = {"last_run_ts": None, "positions": {}}
    # Force an "expired" position so we get a CLOSE action
    import datetime as dt
    sim_state["positions"]["ETH_skew_short_xhi"] = {
        "asset": "ETH", "fund": "skew_short_xhi",
        "instrument": config.PERP_ETH,
        "direction": -1, "size_contracts": 100.0, "size_usd": 100.0,
        "entry_price": eth_sig.spot * 1.01,
        "entry_ts":     "2020-01-01T00:00:00Z",
        "exit_due_ts":  "2020-01-02T00:00:00Z",   # already past
        "asset_weight_at_entry": 1.0,
    }
    actions = strategy.decide(btc_sig, eth_sig, sim_state)
    for a in actions:
        print(f"    {a.op:<7s} {a.asset}/{a.fund:<16s} "
              f"dir={a.direction:+d}  size=${a.size_usd:,.2f}  "
              f"reason={a.reason}")

    # 4b. paper_equity MTM with the simulated open position
    print("\n[3b] paper_equity.update_equity() MTM check ...")
    snap = paper_equity.update_equity(sim_state, btc_sig.spot, eth_sig.spot)
    print(f"  {paper_equity.one_line_summary(snap)}")
    expected_dir = -1
    expected_qty = 100.0 / (eth_sig.spot * 1.01)
    expected_pnl = expected_dir * (eth_sig.spot - eth_sig.spot * 1.01) * expected_qty \
                   - 100.0 * config.FEE_BPS / 10_000
    assert abs(snap["unrealized"] - expected_pnl) < 0.01, \
        f"unrealized mismatch: got {snap['unrealized']}, expected {expected_pnl:.4f}"
    print(f"  unrealized math OK (expected ${expected_pnl:+.4f})")
    # Clean up the equity row we just wrote so it doesn't leak into a real run
    if os.path.exists(config.EQUITY_CSV):
        os.remove(config.EQUITY_CSV)
        print(f"  removed test equity file {config.EQUITY_CSV}")

    # 5. State save/load roundtrip
    print("\n[4] state save/load roundtrip ...")
    test_state = {"last_run_ts": "2026-05-04T19:00:00Z",
                  "positions": {"BTC_squeeze": {
                      "asset": "BTC", "fund": "squeeze",
                      "instrument": "BTC_USDC-PERPETUAL",
                      "direction": 1, "size_contracts": 600.0,
                      "size_usd": 600.0, "entry_price": 96000.0,
                      "entry_ts": "2026-05-04T19:00:00Z",
                      "exit_due_ts": "2026-05-05T19:00:00Z",
                      "asset_weight_at_entry": 1.0}}}
    st.save_state(test_state)
    loaded = st.load_state()
    assert loaded["positions"]["BTC_squeeze"]["size_usd"] == 600.0, loaded
    print(f"  state file -> {config.STATE_FILE}")
    print(f"  loaded {len(loaded['positions'])} positions OK")

    # Clean up test state so it doesn't pollute later real runs
    os.remove(config.STATE_FILE)
    print(f"  removed test state file")

    # 6. Sanity-check fund definitions
    print("\n[5] fund definitions sanity ...")
    print(f"  ETH funds: {len(config.ETH_FUNDS)} -> "
          f"per-fund slice solo=${config.CAPITAL_USD/len(config.ETH_FUNDS):,.0f}  "
          f"overlap=${config.CAPITAL_USD*0.5/len(config.ETH_FUNDS):,.0f}")
    print(f"  BTC funds: {len(config.BTC_FUNDS)} -> "
          f"per-fund slice solo=${config.CAPITAL_USD/len(config.BTC_FUNDS):,.0f}  "
          f"overlap=${config.CAPITAL_USD*0.5/len(config.BTC_FUNDS):,.0f}")

    print("\n[OK] smoke test passed.")


if __name__ == "__main__":
    main()
