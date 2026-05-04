"""
bootstrap.py
============
One-time setup: copy the historical surface + funding CSVs from the
backtest folder into deribit/data/ so the rolling z-scores have full
warm-up data on the very first live run.

Usage:
    python bootstrap.py                # copies ../BTC/data/* -> ./data/*_live.csv
    python bootstrap.py --reset        # also wipes positions_state.json
"""
import argparse
import os
import shutil
import sys

import config

SRC_BTC_SURF = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "BTC", "data", "BTC_surface.csv"))
SRC_ETH_SURF = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "BTC", "data", "ETH_surface.csv"))
SRC_FUNDING  = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "BTC", "data", "funding.csv"))


def _copy(src: str, dst: str, overwrite: bool) -> None:
    if not os.path.exists(src):
        print(f"  [SKIP] source not found: {src}")
        return
    if os.path.exists(dst) and not overwrite:
        print(f"  [keep] {dst} (already exists -- pass --overwrite to replace)")
        return
    shutil.copy2(src, dst)
    print(f"  [copy] {src}")
    print(f"      -> {dst}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--overwrite", action="store_true",
                   help="Replace any existing live CSVs.")
    p.add_argument("--reset", action="store_true",
                   help="Also wipe positions_state.json and trade_log.csv.")
    args = p.parse_args()

    print("[bootstrap] copying warm-up history from ../BTC/data/ ...")
    _copy(SRC_BTC_SURF, config.BTC_SURF_LIVE, args.overwrite)
    _copy(SRC_ETH_SURF, config.ETH_SURF_LIVE, args.overwrite)
    _copy(SRC_FUNDING,  config.FUND_LIVE,     args.overwrite)

    if args.reset:
        for f in (config.STATE_FILE, config.TRADE_LOG):
            if os.path.exists(f):
                os.remove(f)
                print(f"  [wipe] {f}")
    print("\n[bootstrap] done.  Next steps:")
    print("  1. Copy .env.example -> .env and fill in DERIBIT_CLIENT_ID/SECRET")
    print("  2. python main.py --dry-run        (verify everything works)")
    print("  3. Set DRY_RUN=false in .env once you trust it.")


if __name__ == "__main__":
    main()
