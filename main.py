"""
main.py
=======
Single-pass orchestrator: fetch -> signal -> decide -> execute.
Designed to be run from cron / Task Scheduler every N hours, or
manually any time.

Run modes
---------
  Two orthogonal switches:
    MODE     paper | live   (env: MODE)
    DRY_RUN  true  | false  (env: DRY_RUN, --dry-run flag)

  python main.py                 # use MODE+DRY_RUN from .env (default paper)
  python main.py --dry-run       # force DRY_RUN=true regardless of .env
  python main.py --execute       # force DRY_RUN=false regardless of .env
  python main.py --no-trade      # snapshot + signal only, no decisions

  paper + DRY_RUN=False : simulated fills, paper equity tracked
  paper + DRY_RUN=True  : print plan, no persist (one-shot inspection)
  live  + DRY_RUN=False : REAL ORDERS on Deribit. Requires creds.
  live  + DRY_RUN=True  : print would-be orders, no send

What it does
------------
  1. Authenticates with Deribit (token cached in memory).
  2. Snapshots BTC + ETH option chains, computes today's surface row.
  3. Pulls 24h BTC perp funding sum.
  4. Appends today's row to BTC_surface_live.csv / ETH_surface_live.csv /
     btc_funding_live.csv (overwrites if today already in file).
  5. Loads warm-up history, computes skew_z + funding_z + gates.
  6. Evaluates each fund's trigger -> "fired" set per asset.
  7. Reads positions_state.json + reconciles with Deribit positions.
  8. Decides actions (CLOSE / REFRESH / OPEN).
  9. Executes (or prints) actions.
 10. Writes updated state + appends to trade_log.csv.

The whole run is logged to logs/run.log AND mirrored to stdout.
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
import traceback

import pandas as pd

import config
from deribit_api import DeribitClient, DeribitError
import data_fetch
import signals
import strategy
import state as st
import execution
import paper_equity


def _log(msg: str = "", file=None) -> None:
    print(msg)
    if file is not None:
        file.write(msg + "\n")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true",
                   help="Force DRY_RUN=true (print plan, no persist/send)")
    p.add_argument("--execute", action="store_true",
                   help="Force DRY_RUN=false (persist state, send orders if live)")
    p.add_argument("--no-trade", action="store_true",
                   help="Snapshot + signal only -- skip decide/execute")
    return p.parse_args()


def run(args: argparse.Namespace) -> int:
    if args.dry_run and args.execute:
        print("Cannot pass both --dry-run and --execute")
        return 4
    if args.dry_run:
        config.DRY_RUN = True
    if args.execute:
        config.DRY_RUN = False

    log_f = open(config.RUN_LOG, "a", encoding="utf-8")
    sep = "=" * 78
    _log("\n" + sep, log_f)
    _log(f" RUN  {dt.datetime.now(dt.timezone.utc).isoformat()}  "
         f"mode={config.MODE.upper()}  dry_run={config.DRY_RUN}  "
         f"capital=${config.CAPITAL_USD:,.0f}", log_f)
    _log(f"  data  -> {config.DATA_ENV.upper():4s}  ({config.DATA_BASE_URL})", log_f)
    _log(f"  trade -> {config.TRADE_ENV.upper():4s}  ({config.TRADE_BASE_URL})", log_f)
    if config.IS_PAPER:
        _log(f"  paper-cost model: fee={config.FEE_BPS}bps/side  "
             f"slippage={config.SLIPPAGE_BPS}bps/side", log_f)
    elif config.TRADE_ENV == "live":
        _log(f"  *** REAL MONEY VENUE (TRADE_ENV=live) -- be careful ***", log_f)
    _log(sep, log_f)

    client = DeribitClient()

    # ---------- 1. Snapshot ----------
    _log("\n[1/5] Snapshot ...", log_f)
    try:
        snap = data_fetch.take_snapshot(client)
    except DeribitError as e:
        _log(f"  [FATAL] snapshot failed: {e}", log_f)
        log_f.close()
        return 2
    _log(f"  {snap.summary()}", log_f)

    if snap.btc_surface is None or snap.eth_surface is None:
        _log("  [FATAL] could not build surface for both assets -- aborting.",
             log_f)
        log_f.close()
        return 3

    # ---------- 2. Persist ----------
    _log("\n[2/5] Persist snapshot to live CSVs ...", log_f)
    data_fetch.persist_snapshot(snap)
    _log(f"  appended {snap.timestamp.date()} to "
         f"BTC_surface_live / ETH_surface_live / btc_funding_live", log_f)

    # ---------- 3. Compute signals ----------
    _log("\n[3/5] Compute signals ...", log_f)
    btc_sig, eth_sig, _btc_panel, _eth_panel = signals.compute_all_signals()
    _log(f"  BTC: spot=${btc_sig.spot:,.0f}  skew_z={btc_sig.skew_z:+.2f}  "
         f"funding_z={btc_sig.funding_z:+.2f}  fires={list(btc_sig.fired)}",
         log_f)
    _log(f"  ETH: spot=${eth_sig.spot:,.0f}  skew_z={eth_sig.skew_z:+.2f}  "
         f"fires={list(eth_sig.fired)}", log_f)

    if args.no_trade:
        _log("\n[no-trade] stopping after signals (per --no-trade).", log_f)
        log_f.close()
        return 0

    # ---------- 4. Decide ----------
    _log("\n[4/5] Decide ...", log_f)
    state = st.load_state()
    _log(strategy.explain_state(btc_sig, eth_sig, state), log_f)

    # Reconcile state vs exchange BEFORE deciding (operator can abort)
    try:
        execution.reconcile_with_exchange(client, state)
    except Exception as e:
        _log(f"  [warn] reconcile skipped: {e}", log_f)

    actions = strategy.decide(btc_sig, eth_sig, state)
    if not actions:
        _log("  No actions required.", log_f)

    # ---------- 5. Execute ----------
    _log("\n[5/5] Execute ...", log_f)
    execution.execute(
        client, actions, state,
        skew_z_btc=btc_sig.skew_z,
        skew_z_eth=eth_sig.skew_z,
        funding_z=btc_sig.funding_z,
    )

    if not config.DRY_RUN:
        st.save_state(state)

    # Paper-mode mark-to-market and equity append (skipped in dry-run
    # so a one-shot inspection doesn't pollute the equity series).
    if config.IS_PAPER and not config.DRY_RUN:
        snapshot = paper_equity.update_equity(state, btc_sig.spot, eth_sig.spot)
        _log("", log_f)
        _log(paper_equity.one_line_summary(snapshot), log_f)

    _log("\n[done]", log_f)
    log_f.close()
    return 0


def main() -> int:
    args = parse_args()
    try:
        return run(args)
    except Exception:
        traceback.print_exc()
        return 99


if __name__ == "__main__":
    sys.exit(main())
