"""Locked settings. The agent can learn lessons, but it can NEVER change these."""
import os


def _f(name, default):
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return float(default)


# paper = virtual money on real prices | mt5 = Exness MT5 directly | metaapi = via MetaApi
MODE = os.getenv("MODE", "paper").strip().lower()
SYMBOL = os.getenv("SYMBOL", "XAUUSD")          # Exness standard demo may need "XAUUSDm"
PAPER_DATA_TICKER = os.getenv("PAPER_DATA_TICKER", "GC=F")  # Yahoo gold futures for paper mode
CONTRACT_SIZE = _f("CONTRACT_SIZE", 100)          # 1 lot gold = 100 oz -> $100 per $1 move

# ---- Risk rules (hard-locked) ----
RISK_PCT = min(_f("RISK_PCT", 1.0), 2.0)          # % of balance risked per trade, never above 2
# Smallest lot (0.01 gold = $1 per $1 move) can exceed RISK_PCT on a small account; allow it up to this cap.
MIN_LOT_MAX_RISK_PCT = min(_f("MIN_LOT_MAX_RISK_PCT", 5.0), 6.0)
MAX_DAILY_LOSS_PCT = 3.0                          # stop trading for the day after -3%
MAX_TRADES_PER_DAY = 3
MAX_OPEN_POSITIONS = 1
MIN_RR = 1.5                                      # take-profit must be >= 1.5x stop distance
MIN_SL_ATR, MAX_SL_ATR = 0.6, 3.0                 # stop distance must be sane vs 1h ATR
MIN_CONFIDENCE = 60
MIN_AGREE = 2                                     # at least 2 AIs must agree on direction
MAX_SPREAD = _f("MAX_SPREAD", 0.6)                # skip if gold spread is wider (in $)
PLAYBOOK_GATE = _f("PLAYBOOK_GATE", 1) > 0           # trade only when a PROVEN backtested strategy confirms
BREAKEVEN_AT_R = 1.0                              # move SL to entry after +1R
NEWS_BLOCK_MINUTES = 45                           # no new trades +-45 min of high-impact USD news
PAPER_START_BALANCE = 10_000.0
PAPER_SPREAD = 0.30

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
