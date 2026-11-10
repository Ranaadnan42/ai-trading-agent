"""Hard risk engine. The AI proposes, this file disposes. Nothing here is learnable."""
import math

from . import config


def check_levels(side, entry, sl, tp, atr):
    """Clamp SL into the sane ATR band, then require minimum reward:risk. Returns (sl, tp) or reason str."""
    dist = abs(entry - sl)
    lo, hi = config.MIN_SL_ATR * atr, config.MAX_SL_ATR * atr
    if dist > hi:
        return f"stop too wide ({dist:.2f} > {hi:.2f})"
    if dist < lo:
        dist = lo
        sl = entry - dist if side == "BUY" else entry + dist
    reward = (tp - entry) if side == "BUY" else (entry - tp)
    if reward / dist < config.MIN_RR:
        return f"reward:risk {reward / dist:.2f} below {config.MIN_RR}"
    return round(sl, 2), round(tp, 2)


def lot_size(balance, entry, sl, min_lot=0.01, max_lot=100.0, step=0.01, contract=config.CONTRACT_SIZE):
    risk_usd = balance * config.RISK_PCT / 100
    per_lot = abs(entry - sl) * contract
    if per_lot <= 0:
        return 0.0, 0.0
    lots = math.floor(risk_usd / per_lot / step) * step
    if lots < min_lot:
        # Even the smallest lot would risk too much -> skip unless only slightly over.
        if min_lot * per_lot > risk_usd * 1.5:
            return 0.0, 0.0
        lots = min_lot
    lots = round(min(lots, max_lot), 2)
    return lots, round(lots * per_lot, 2)


def daily_guard(balance, equity, realized_today, trades_today, open_count):
    start_balance = balance - realized_today
    day_pnl = realized_today + (equity - balance)
    if day_pnl <= -start_balance * config.MAX_DAILY_LOSS_PCT / 100:
        return f"daily loss limit hit ({day_pnl:.2f})"
    if trades_today >= config.MAX_TRADES_PER_DAY:
        return "max trades for today reached"
    if open_count >= config.MAX_OPEN_POSITIONS:
        return "a position is already open"
    return None
