"""
config.py
=========
All tunables for the live deribit deployment in ONE place.  Mirrors the
fund definitions used in `multi_asset_combined.py` so that the live
strategy is byte-for-byte the same edge that was backtested.

Edit CAPITAL_USD and the safety flags via the `.env` file (not here).
"""
from __future__ import annotations
import os

from dotenv import load_dotenv

load_dotenv()


# ============================================================
# Credentials & environment
# ============================================================
CLIENT_ID     = os.getenv("DERIBIT_CLIENT_ID", "").strip()
CLIENT_SECRET = os.getenv("DERIBIT_CLIENT_SECRET", "").strip()

# Deribit has two parallel deployments:
#   live  -> www.deribit.com    (real money + real liquidity)
#   test  -> test.deribit.com   (testnet: monopoly-money but ALSO has
#                                synthetic / very thin option chains, so
#                                signal computation off testnet data is
#                                unreliable)
#
# We split the routing into TWO independent variables:
#
#   DATA_ENV   - where we PULL market data (option chains, index price,
#                funding history, instrument specs).  ALWAYS defaults to
#                'live' because signal quality requires real liquidity.
#                You almost never want to change this.
#
#   TRADE_ENV  - where we SEND orders + fetch our own positions.
#                Defaults to 'test' so a fat-fingered MODE=live run still
#                hits monopoly money first.  Flip to 'live' only after
#                a clean testnet track record.
#
# The two endpoints share the same auth API so credentials minted on
# test.deribit.com only work on test.deribit.com (and vice versa).
DATA_ENV  = os.getenv("DATA_ENV",  "live").strip().lower()
TRADE_ENV = os.getenv("TRADE_ENV", "test").strip().lower()

if DATA_ENV not in ("live", "test"):
    raise ValueError(f"DATA_ENV must be 'live' or 'test', got {DATA_ENV!r}")
if TRADE_ENV not in ("live", "test"):
    raise ValueError(f"TRADE_ENV must be 'live' or 'test', got {TRADE_ENV!r}")

_BASE = {
    "live": "https://www.deribit.com/api/v2",
    "test": "https://test.deribit.com/api/v2",
}
DATA_BASE_URL  = _BASE[DATA_ENV]
TRADE_BASE_URL = _BASE[TRADE_ENV]

# ============================================================
# Mode + capital + safety
# ============================================================
# MODE selects the execution backend.  Two values:
#   "paper"  - simulated fills only.  No DERIBIT_CLIENT_ID / SECRET needed
#              (we only call PUBLIC endpoints for market data).  All state
#              is tracked in `data/*_paper.*` files and a paper equity
#              curve is maintained so you can see live-money-shape PnL.
#   "live"   - real orders sent to Deribit.  Requires valid credentials
#              and `DRY_RUN=false` to actually trade.  State is tracked
#              in `data/*_live.*` files (separate from paper).
MODE = os.getenv("MODE", "paper").strip().lower()
if MODE not in ("paper", "live"):
    raise ValueError(f"MODE must be 'paper' or 'live', got {MODE!r}")
IS_PAPER = (MODE == "paper")
IS_LIVE  = (MODE == "live")

# DRY_RUN is orthogonal to MODE.  When True the script PRINTS the action
# plan but does not persist state or send orders.  Useful as a one-shot
# inspection in either mode.  CLI flag --dry-run forces this on.
DRY_RUN  = os.getenv("DRY_RUN", "false").lower() in ("true", "1", "yes", "y")

CAPITAL_USD = float(os.getenv("CAPITAL_USD", "3000"))

# Paper-mode execution costs (ignored in live mode where Deribit's actual
# fills are used).  Defaults reflect Deribit USDC perp taker (5 bps) plus
# a small slippage estimate; tune to your venue / liquidity.
FEE_BPS      = float(os.getenv("FEE_BPS",      "5"))   # per-side, taker
SLIPPAGE_BPS = float(os.getenv("SLIPPAGE_BPS", "2"))   # per-side market impact

# ============================================================
# Instruments (Deribit USDC-margined linear perpetuals)
# ============================================================
PERP_BTC = "BTC_USDC-PERPETUAL"
PERP_ETH = "ETH_USDC-PERPETUAL"

INDEX_BTC = "btc_usd"
INDEX_ETH = "eth_usd"

# Currency for option chain pulls (Deribit settles options in BTC / ETH)
OPT_CCY_BTC = "BTC"
OPT_CCY_ETH = "ETH"

# Currency for USDC perp positions
PERP_CCY = "USDC"

# ============================================================
# Signal computation
# ============================================================
SKEW_LB        = 60       # rolling window for skew z-score (matches backtest)
FUND_Z_LB      = 252      # rolling window for funding z-score
WINSORIZE_K    = 6.0
WINSORIZE_W    = 252
HV_WINDOW      = 20       # only used if you ever add vrp signals
ANN            = 365      # crypto trades 24/7

# Min observations before z-scores are emitted (warm-up).
SKEW_MIN_OBS   = 20
FUND_MIN_OBS   = 60

# ============================================================
# Funds  (mirrors multi_asset_combined.py exactly)
# ============================================================
# 4 ETH funds (skew_z only)
ETH_FUNDS: dict[str, dict] = {
    "skew_short_xhi": {"trigger": ("skew_z", ">",  2.0), "gate": "any",
                       "direction": -1, "hold_days": 1},
    "skew_short_hi":  {"trigger": ("skew_z", ">",  1.5), "gate": "any",
                       "direction": -1, "hold_days": 1},
    "skew_long_lo":   {"trigger": ("skew_z", "<", -1.5), "gate": "any",
                       "direction": +1, "hold_days": 1},
    "skew_long_lo_b": {"trigger": ("skew_z", "<", -1.5), "gate": "btc_up",
                       "direction": +1, "hold_days": 2},
}

# BTC = same 4 + BTC-only `squeeze` (funding_z<-1 LONG)
BTC_FUNDS: dict[str, dict] = {
    **ETH_FUNDS,
    "squeeze": {"trigger": ("funding_z", "<", -1.0), "gate": "any",
                "direction": +1, "hold_days": 1},
}

# ============================================================
# Exit signal options  (all OFF by default to match backtest exits)
# ============================================================
USE_STOP_LOSS         = False
STOP_LOSS_PCT         = 0.04   # 4% adverse from entry => emergency close

USE_SIGNAL_REVERSAL   = False
# If on, close a position when its trigger z has crossed back through 0
# by more than REVERSAL_BUFFER (e.g. entered short on skew_z>+2, exit
# when skew_z drops below +0.5).
REVERSAL_BUFFER       = 0.5

# ============================================================
# Run cadence guard rails
# ============================================================
# A "day" for hold-period bookkeeping is exactly 24h elapsed since entry,
# regardless of when the script runs.  Crypto is 24/7.
HOLD_DAY_SECONDS      = 24 * 60 * 60

# Refuse to act if last successful run was less than this long ago.
# Set to 0 to allow back-to-back runs (useful for testing).
MIN_RUN_INTERVAL_SECONDS = 0

# ============================================================
# Paths
# ============================================================
DATA_DIR  = os.path.join(os.path.dirname(__file__), "data")
LOGS_DIR  = os.path.join(os.path.dirname(__file__), "logs")
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(LOGS_DIR, exist_ok=True)

# Surface + funding histories are SHARED across modes (they're just
# market data; they aren't influenced by paper vs live execution).
BTC_SURF_LIVE = os.path.join(DATA_DIR, "BTC_surface_live.csv")
ETH_SURF_LIVE = os.path.join(DATA_DIR, "ETH_surface_live.csv")
FUND_LIVE     = os.path.join(DATA_DIR, "btc_funding_live.csv")

# State / trade log / equity ARE mode-specific so paper and live never
# stomp on each other's books.
_SUFFIX     = "paper" if IS_PAPER else "live"
STATE_FILE  = os.path.join(DATA_DIR, f"positions_state_{_SUFFIX}.json")
TRADE_LOG   = os.path.join(DATA_DIR, f"trade_log_{_SUFFIX}.csv")
EQUITY_CSV  = os.path.join(DATA_DIR, f"equity_{_SUFFIX}.csv")
RUN_LOG     = os.path.join(LOGS_DIR, f"run_{_SUFFIX}.log")
