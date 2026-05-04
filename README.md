# deribit/

Paper-trading and live-execution scaffold for the BTC + ETH multi-asset
skew_z strategy backtested in `BTC/multi_asset_combined.py`.  Runs once
per invocation, designed to be called from cron / Task Scheduler / a
hosted job runner.


## Two orthogonal switches

| `MODE`  | `DRY_RUN` | what happens |
|---------|-----------|---------------|
| paper   | false     | **default for getting started.**  Simulates fills with fee+slippage, persists positions to `data/*_paper.*`, builds an equity curve.  No Deribit credentials needed. |
| paper   | true      | one-shot: prints what *would* happen, no persist.  Good for "what does the model think right now?" |
| live    | false     | **REAL orders on Deribit.**  Requires API keys.  Persists to `data/*_live.*`. |
| live    | true      | dry-run with credentials: tests private endpoints (positions fetch + reconcile) without sending orders. |

Recommended progression: paper → live-with-dry-run → live.

## What it actually does (per run)

1. **Snapshot** — pulls BTC + ETH option chains from Deribit, the index
   prices, and the trailing-24h BTC perp funding rate.
2. **Surface** — re-implements the same surface math used in
   `btc_option_skew.py` so today's `skew`, `atm_iv`, `butterfly`,
   `ts_ratio` row drops cleanly into `BTC_surface_live.csv` /
   `ETH_surface_live.csv` (overwrites if today's date already exists).
3. **Signals** — computes `skew_z` (60-day rolling, winsorize_k=6) for
   both assets and `funding_z` (252-day rolling) for BTC.  Identical to
   `multi_asset_combined.py`.
4. **Decide** — for every open position, checks if its hold expired (and
   any optional exit signals).  For every fund whose trigger fires today
   *and* whose gate passes, plans an OPEN.
5. **Allocate** — per `multi_asset_combined.py` rule:
   * if both BTC and ETH have firing funds:  asset weights = 0.5 / 0.5
   * if only one fires:                      that asset gets 1.0
   * each fund inside an asset gets 1/N (4 funds for ETH, 5 for BTC)
6. **Execute** — places market orders on `BTC_USDC-PERPETUAL` and
   `ETH_USDC-PERPETUAL` with `reduce_only=true` on closes.  Quantizes
   contract size to the exchange minimum.
7. **Persist** — updates `data/positions_state_<mode>.json`, appends to
   `data/trade_log_<mode>.csv`, and logs everything to
   `logs/run_<mode>.log`.  In paper mode, also marks-to-market every
   open position and appends a row to `data/equity_paper.csv`.

## Funds (mirrors backtest)

```
ETH (4 funds, 1/4 each):
  skew_short_xhi  : skew_z > +2.0   any      SHORT  hold 1d
  skew_short_hi   : skew_z > +1.5   any      SHORT  hold 1d
  skew_long_lo    : skew_z < -1.5   any      LONG   hold 1d
  skew_long_lo_b  : skew_z < -1.5   btc_up   LONG   hold 2d

BTC (5 funds, 1/5 each):
  same 4 +
  squeeze         : funding_z < -1.0  any    LONG   hold 1d
```

Crypto trades 24/7 → entries are taken at the spot price observed
*at the moment the script runs* (no next-bar wait).  Exits trigger when
`now ≥ entry_ts + hold_days * 24h`.

## Setup -- paper mode (zero credentials required)

```bash
# 1. Install deps
pip install -r requirements.txt

# 2. Settings (defaults are paper, MODE=paper, DRY_RUN=false)
cp .env.example .env
# edit CAPITAL_USD if you want, leave DERIBIT_CLIENT_ID/SECRET blank

# 3. Seed warm-up history so rolling z-scores are valid on first run
python bootstrap.py
# -> copies ../BTC/data/{BTC_surface,ETH_surface,funding}.csv into ./data/

# 4. Inspect today's signals (one-shot, no persist)
python main.py --dry-run --no-trade

# 5. Run paper trading
python main.py
# -> appends a row to data/equity_paper.csv each run
# -> persists open positions to data/positions_state_paper.json
# -> appends every OPEN/CLOSE/REFRESH to data/trade_log_paper.csv

# 6. Schedule it (any cadence; 4-6x per day is fine)
#    Linux cron:
#      5 0,6,12,18 * * *  cd /path/to/deribit && /usr/bin/python3 main.py >> logs/cron.log 2>&1

# 7. Check paper performance any time
python paper_summary.py
```

## Going live (after a satisfying paper track record)

The script splits venue routing into TWO knobs:

* `DATA_ENV`  -- where MARKET DATA is fetched (option chains, index price,
                funding, instrument specs).  **Default `live`** and you
                almost never want to change it: testnet option chains are
                synthetic / very thin and would produce garbage `skew_z`.
* `TRADE_ENV` -- where ORDERS are placed and YOUR positions are read.
                **Default `test`** so a misconfigured `MODE=live` run hits
                monopoly money.

So during the testnet shake-out you have:
**real signals + fake orders**, which is exactly the intermediate stage
between paper and full production.

```bash
# 1. Generate a TESTNET key at test.deribit.com/account/BTC/api/api
#    (testnet has its own user accounts -- separate from prod)

# 2. Drop creds in .env:
#      MODE=live
#      DERIBIT_CLIENT_ID=<testnet id>
#      DERIBIT_CLIENT_SECRET=<testnet secret>
#      DATA_ENV=live          (default: pulls REAL liquid data)
#      TRADE_ENV=test         (default: orders hit testnet)
#      DRY_RUN=true           (start with no orders sent)

# 3. Verify private endpoints work (fetch positions, reconcile)
python main.py --dry-run

# 4. Switch to actual testnet orders: DRY_RUN=false
python main.py
# -> for a few days, verify orders fill correctly and state matches.
#    The PnL on testnet is fake but real-data signals make it useful.

# 5. Flip to production trading:
#      Get a PRODUCTION key at www.deribit.com/account/BTC/api/api
#      Update .env: DERIBIT_CLIENT_ID/SECRET, TRADE_ENV=live
python main.py
```

## Scheduling

The script is idempotent within a calendar day -- re-running won't
double-enter the same fund.  Recommended cadence:

* **once per day** at a stable time (e.g. 00:05 UTC after Deribit's
  daily settlement) for the full close-then-open cycle.
* **every 6-12h** if you want exits to fire promptly when the 24h hold
  elapses; otherwise positions sit one extra day before close.

Example cron (Linux):
```
5 0,6,12,18 * * *  cd /path/to/deribit && /usr/bin/python3 main.py >> logs/cron.log 2>&1
```

Example Task Scheduler (Windows): trigger 4× daily, action `python.exe`
with arguments `c:\path\to\deribit\main.py`.

## Exit signals (optional)

Off by default; turn on in `config.py`:

* `USE_STOP_LOSS = True` + `STOP_LOSS_PCT = 0.04`  
  Closes a leg if unrealized PnL drops below -4%.
* `USE_SIGNAL_REVERSAL = True` + `REVERSAL_BUFFER = 0.5`  
  Closes a leg if its skew_z crosses back through 0 by 0.5σ
  (e.g. opened short on `skew_z > +2`, exit when `skew_z < -0.5`).

These deviate from the backtest which only uses time-based exits.
Validate any change in a paper-trading window before going live.

## File layout

```
deribit/
  .env                          # gitignored; settings + credentials
  .env.example
  config.py                     # all knobs (mode-aware paths)
  deribit_api.py                # REST wrapper (auth lazy: paper needs no creds)
  data_fetch.py                 # snapshot + persist (shared *_live histories)
  signals.py                    # rolling z + trigger evaluation
  strategy.py                   # action planning (CLOSE/OPEN/REFRESH)
  execution.py                  # send orders / simulate fills, reconcile, log
  state.py                      # JSON state I/O + trade log
  paper_equity.py               # mark-to-market + equity curve append (paper)
  paper_summary.py              # standalone PnL/Sharpe report tool
  bootstrap.py                  # one-time warm-up copy
  main.py                       # orchestrator
  smoke_test.py                 # end-to-end test, no creds needed
  data/                         # gitignored
    BTC_surface_live.csv        # shared market data (warm-up)
    ETH_surface_live.csv
    btc_funding_live.csv
    positions_state_paper.json  # paper state
    positions_state_live.json   # live state (created when MODE=live)
    trade_log_paper.csv
    trade_log_live.csv
    equity_paper.csv            # one row per run; equity, realized, MTM
    equity_live.csv             # only populated when MODE=live
  logs/                         # gitignored: run logs (per-mode files)
```

## Paper equity tracking

Every run in paper mode appends one row to `data/equity_paper.csv`:

```
timestamp,btc_spot,eth_spot,n_open,realized_cum,unrealized,equity,
open_btc_usd,open_eth_usd
```

* `realized_cum` -- running total of net PnL on closed paper positions
  (parsed back out of the CLOSE rows in `trade_log_paper.csv`).
* `unrealized`   -- mark-to-market on currently open positions, net of
  the entry-side fee (close fee will be charged on close).
* `equity`       -- `CAPITAL_USD + realized_cum + unrealized`.

Per-position MTM uses the actual *quantity* implied by the position's
entry price:

```
qty       = pos.size_usd / pos.entry_price        # in BTC or ETH
pnl_gross = pos.direction * (current_price - entry_price) * qty
fee_open  = pos.size_usd * FEE_BPS / 10_000
unrealized_one_pos = pnl_gross - fee_open
```

`paper_summary.py` then turns this into Sharpe, Sortino, MaxDD plus a
per-fund close-pnl breakdown.

## Safety checklist before going live

- [ ] Ran `MODE=paper` for ≥ 2 weeks; `paper_summary.py` shows behaviour
      consistent with the backtest (positive realized + reasonable
      Sharpe / drawdown profile)
- [ ] `.env` has correct `DERIBIT_CLIENT_ID` / `DERIBIT_CLIENT_SECRET`
      for the venue that `TRADE_ENV` points at (testnet creds for
      `TRADE_ENV=test`, prod creds for `TRADE_ENV=live`)
- [ ] `DATA_ENV=live` (real liquid data -- almost always what you want)
- [ ] `TRADE_ENV=test` for at least one full week of testnet trading
      with `MODE=live`
- [ ] `bootstrap.py` was run; `data/BTC_surface_live.csv` covers
      ≥ 60 trailing days so `skew_z` is non-NaN on first run
- [ ] `data/btc_funding_live.csv` covers ≥ 252 days for `funding_z`
- [ ] Verified `python main.py --dry-run` reports the same `skew_z` /
      `funding_z` values as `multi_asset_combined.py` would for today
- [ ] You can read `data/trade_log_<mode>.csv` and explain every row
- [ ] `CAPITAL_USD` reflects what you actually want to risk
- [ ] You've thought through what happens if the script *doesn't* run
      for a day (hold expiry will only fire on the next run -- positions
      stay live an extra cycle, no order leak)
