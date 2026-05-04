"""
paper_summary.py
================
Read `data/equity_paper.csv` + `data/trade_log_paper.csv` and print a
performance breakdown for the paper book.

Run any time:  python paper_summary.py
"""
from __future__ import annotations

import os

import numpy as np
import pandas as pd

import config

ANN = 365   # crypto trades 24/7


def _load_equity() -> pd.DataFrame | None:
    if not os.path.exists(config.EQUITY_CSV):
        return None
    df = pd.read_csv(config.EQUITY_CSV, parse_dates=["timestamp"])
    df = df.sort_values("timestamp").reset_index(drop=True)
    return df


def _load_trades() -> pd.DataFrame | None:
    if not os.path.exists(config.TRADE_LOG):
        return None
    df = pd.read_csv(config.TRADE_LOG, parse_dates=["timestamp"])
    return df


def _series_stats(equity: pd.Series) -> dict:
    """Sharpe / Sortino / CAGR / MaxDD on per-run equity points."""
    eq = equity.values.astype(float)
    if len(eq) < 2:
        return {"sharpe": 0, "sortino": 0, "cagr": 0, "maxdd": 0,
                "total": eq[-1] / eq[0] - 1 if len(eq) else 0}
    rets = np.diff(eq) / eq[:-1]
    days = (equity.index[-1] - equity.index[0]).total_seconds() / 86400 \
        if hasattr(equity.index, "to_pydatetime") else len(eq) - 1
    if days < 1:
        days = max(1, len(eq) - 1)
    obs_per_year = len(rets) / max(1, days) * 365
    if rets.std() == 0:
        sh = sort = 0.0
    else:
        sh = rets.mean() / rets.std() * np.sqrt(obs_per_year)
        down = rets[rets < 0].std() if (rets < 0).any() else 0.0
        sort = (rets.mean() / down * np.sqrt(obs_per_year)) if down > 0 else 0.0
    total = eq[-1] / eq[0] - 1
    cagr  = (1 + total) ** (365 / max(1, days)) - 1
    maxdd = ((eq / np.maximum.accumulate(eq)) - 1).min()
    return {"sharpe": sh, "sortino": sort, "cagr": cagr,
            "maxdd": maxdd, "total": total, "obs_per_year": obs_per_year,
            "days": days, "n_points": len(eq)}


def main():
    print("=" * 76)
    print(f" PAPER BOOK SUMMARY  ({config.EQUITY_CSV})")
    print("=" * 76)

    eq_df = _load_equity()
    if eq_df is None or len(eq_df) == 0:
        print("\nNo equity history yet -- run `python main.py` (in paper mode) first.")
        return

    print(f"\n  capital seed     : ${config.CAPITAL_USD:,.2f}")
    print(f"  fee model        : {config.FEE_BPS} bps/side")
    print(f"  slippage model   : {config.SLIPPAGE_BPS} bps/side")
    print(f"  history          : {len(eq_df)} runs   "
          f"{eq_df['timestamp'].min()} -> {eq_df['timestamp'].max()}")

    last = eq_df.iloc[-1]
    pct = (last["equity"] / config.CAPITAL_USD - 1) * 100
    print(f"\n  current equity   : ${last['equity']:,.2f}  ({pct:+.2f}%)")
    print(f"  realized cumul.  : ${last['realized_cum']:+,.2f}")
    print(f"  unrealized       : ${last['unrealized']:+,.2f}")
    print(f"  open positions   : {int(last['n_open'])}")
    print(f"  net BTC notional : ${last['open_btc_usd']:+,.0f}")
    print(f"  net ETH notional : ${last['open_eth_usd']:+,.0f}")

    eq_indexed = eq_df.set_index("timestamp")["equity"]
    s = _series_stats(eq_indexed)
    print("\n  --- equity-curve stats (per-run sampling) ---")
    print(f"  total return     : {s['total']*100:+.2f}%")
    print(f"  CAGR-equivalent  : {s['cagr']*100:+.2f}%   "
          f"(based on {s['days']:.1f} elapsed days, "
          f"{s['obs_per_year']:.0f} runs/yr)")
    print(f"  Sharpe           : {s['sharpe']:+.2f}")
    print(f"  Sortino          : {s['sortino']:+.2f}")
    print(f"  Max drawdown     : {s['maxdd']*100:+.2f}%")

    tr = _load_trades()
    if tr is None or len(tr) == 0:
        print("\n  no trades logged yet")
        return

    closes = tr[tr["action"] == "CLOSE"].copy()
    opens  = tr[tr["action"] == "OPEN"]
    print("\n  --- trade activity ---")
    print(f"  opens            : {len(opens)}")
    print(f"  closes           : {len(closes)}")
    print(f"  refreshes        : {(tr['action'] == 'REFRESH').sum()}")

    if len(closes):
        # Pull pnl_net out of reason field
        def _pnl(r):
            if "pnl_net=" not in str(r):
                return np.nan
            try:
                tail = str(r).split("pnl_net=")[1].split("USD")[0]
                return float(tail)
            except Exception:
                return np.nan
        closes["pnl_usd"] = closes["reason"].apply(_pnl)
        wins = closes[closes["pnl_usd"] > 0]
        loss = closes[closes["pnl_usd"] < 0]
        print(f"  win rate         : "
              f"{len(wins)/len(closes)*100:.1f}%   "
              f"({len(wins)}W / {len(loss)}L)")
        print(f"  avg trade        : ${closes['pnl_usd'].mean():+,.2f}")
        print(f"  best / worst     : "
              f"${closes['pnl_usd'].max():+,.2f} / "
              f"${closes['pnl_usd'].min():+,.2f}")

        # By fund
        print("\n  --- per-fund PnL  (closed positions only) ---")
        per = (closes.groupby(["asset", "fund"])["pnl_usd"]
               .agg(["count", "sum", "mean"])
               .rename(columns={"count": "n", "sum": "pnl_total",
                                "mean": "pnl_avg"})
               .sort_values("pnl_total", ascending=False))
        per["pnl_total"] = per["pnl_total"].round(2)
        per["pnl_avg"]   = per["pnl_avg"].round(2)
        print(per.to_string())


if __name__ == "__main__":
    main()
