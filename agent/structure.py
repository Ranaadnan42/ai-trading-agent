"""Price-action reading for one timeframe, turned into plain numbers the AI committee can use.

- Market structure : swing highs/lows -> HH/HL/LH/LL, trend, last BOS / CHoCH
- Liquidity        : equal highs/lows (stop pools), previous day/week high-low, recent liquidity sweeps
- Supply / demand  : base before a strong impulse move, still fresh (not yet revisited)
- Support / resist : swing levels touched several times, nearest above and below price
- Bollinger bands  : 20/2 bands, %B, band width, squeeze / expansion, band walk
"""
import numpy as np
import pandas as pd

from .indicators import atr


def _r(x):
    return None if x is None or (isinstance(x, float) and np.isnan(x)) else round(float(x), 2)


# ------------------------------------------------------------------ swings & structure
def swings(df, k=3):
    """Fractal swing points: a high/low that is the extreme of k bars on each side (confirmed only)."""
    h, l = df["high"].values, df["low"].values
    out = []
    for i in range(k, len(df) - k):
        if h[i] == h[i - k:i + k + 1].max():
            out.append((i, "H", h[i]))
        if l[i] == l[i - k:i + k + 1].min():
            out.append((i, "L", l[i]))
    return out


def market_structure(df, sw):
    highs = [s for s in sw if s[1] == "H"]
    lows = [s for s in sw if s[1] == "L"]
    labels = []
    for seq, up, dn in ((highs, "HH", "LH"), (lows, "HL", "LL")):
        for a, b in zip(seq, seq[1:]):
            labels.append((b[0], up if b[2] > a[2] else dn, b[2]))
    labels.sort()
    last = [lab for _, lab, _ in labels[-4:]]
    hh_hl = sum(x in ("HH", "HL") for x in last)
    lh_ll = sum(x in ("LH", "LL") for x in last)
    trend = "BULLISH" if hh_hl >= 3 else "BEARISH" if lh_ll >= 3 else "RANGING"

    # break of structure: a close beyond the latest confirmed swing high/low
    close = df["close"].values
    event = None
    if highs and lows:
        sh, slw = highs[-1], lows[-1]
        for j in range(max(sh[0], slw[0]) + 1, len(df)):
            if close[j] > sh[2]:
                event = ("BOS_UP" if trend != "BEARISH" else "CHOCH_UP", sh[2], j)
                break
            if close[j] < slw[2]:
                event = ("BOS_DOWN" if trend != "BULLISH" else "CHOCH_DOWN", slw[2], j)
                break
    res = {
        "trend": trend,
        "last_swings": [f"{lab}@{p:.2f}" for _, lab, p in labels[-6:]],
        "last_swing_high": _r(highs[-1][2]) if highs else None,
        "last_swing_low": _r(lows[-1][2]) if lows else None,
    }
    if event:
        res["break"] = f"{event[0]} through {event[1]:.2f}, {len(df) - 1 - event[2]} bars ago"
    return res


# ------------------------------------------------------------------ liquidity
def liquidity(df, sw, a):
    price = df["close"].iloc[-1]
    tol = 0.15 * a
    res = {}
    for kind, name in (("H", "equal_highs_above"), ("L", "equal_lows_below")):
        pts = [p for _, t, p in sw[-40:] if t == kind]
        pools = []
        for i, p in enumerate(pts):
            same = [q for q in pts if abs(q - p) <= tol]
            if len(same) >= 2:
                lvl = round(float(np.mean(same)), 2)
                if all(abs(lvl - x) > tol for x in pools):
                    pools.append(lvl)
        pools = [p for p in pools if (p > price if kind == "H" else p < price)]
        pools.sort(key=lambda p: abs(p - price))
        res[name] = pools[:3]

    # liquidity sweep: in last 5 bars a wick took a swing high/low but the bar closed back inside
    sweep = None
    old = [s for s in sw if s[0] < len(df) - 5]
    hi = max((s for s in old[-12:] if s[1] == "H"), key=lambda s: s[2], default=None)
    lo = min((s for s in old[-12:] if s[1] == "L"), key=lambda s: s[2], default=None)
    for j in range(len(df) - 5, len(df)):
        bar = df.iloc[j]
        if hi and bar.high > hi[2] and bar.close < hi[2]:
            sweep = f"buy-side liquidity swept above {hi[2]:.2f} (bearish sign), {len(df) - 1 - j} bars ago"
        if lo and bar.low < lo[2] and bar.close > lo[2]:
            sweep = f"sell-side liquidity swept below {lo[2]:.2f} (bullish sign), {len(df) - 1 - j} bars ago"
    if sweep:
        res["recent_sweep"] = sweep
    return res


def session_levels(h1):
    """Previous day / week high-low: the classic liquidity pools."""
    out = {}
    d = h1.resample("1D").agg({"high": "max", "low": "min"}).dropna()
    w = h1.resample("W-SUN").agg({"high": "max", "low": "min"}).dropna()
    if len(d) >= 2:
        out["prev_day_high"], out["prev_day_low"] = _r(d["high"].iloc[-2]), _r(d["low"].iloc[-2])
    if len(w) >= 2:
        out["prev_week_high"], out["prev_week_low"] = _r(w["high"].iloc[-2]), _r(w["low"].iloc[-2])
    return out


# ------------------------------------------------------------------ supply / demand
def supply_demand(df, a_series, lookback=150):
    """Zone = the last opposite candle (base) right before a strong impulse (>= 1.5x ATR in <= 3 bars).
    Kept only while fresh: price has not closed through it since."""
    o, h, l, c = (df[x].values for x in ("open", "high", "low", "close"))
    av = a_series.values
    n = len(df)
    price = c[-1]
    zones = []
    for i in range(max(1, n - lookback), n - 3):
        move = c[i + 3] - c[i] if i + 3 < n else c[-1] - c[i]
        if abs(move) < 1.5 * av[i]:
            continue
        kind = "demand" if move > 0 else "supply"
        top, bot = h[i], l[i]
        later = c[i + 1:]
        broken = (later < bot).any() if kind == "demand" else (later > top).any()
        if broken:
            continue
        # touched again after leaving = less fresh
        touches = int(((l[i + 4:] <= top) & (h[i + 4:] >= bot)).sum()) if kind == "demand" else \
            int(((h[i + 4:] >= bot) & (l[i + 4:] <= top)).sum())
        zones.append({"type": kind, "top": round(top, 2), "bottom": round(bot, 2), "retests": touches,
                      "dist": round(abs((bot if kind == "supply" else top) - price), 2)})
    # merge overlapping zones of the same type, keep the closest
    zones.sort(key=lambda z: z["dist"])
    keep = []
    for z in zones:
        if not any(k["type"] == z["type"] and z["bottom"] <= k["top"] and z["top"] >= k["bottom"] for k in keep):
            keep.append(z)
    dem = [z for z in keep if z["type"] == "demand" and z["top"] <= price + 0.2 * av[-1]][:2]
    sup = [z for z in keep if z["type"] == "supply" and z["bottom"] >= price - 0.2 * av[-1]][:2]
    fmt = lambda z: f"{z['bottom']}-{z['top']} (retests {z['retests']}, {z['dist']} away)"
    return {"demand_zones_below": [fmt(z) for z in dem], "supply_zones_above": [fmt(z) for z in sup]}


# ------------------------------------------------------------------ support / resistance
def support_resistance(df, sw, a):
    price = df["close"].iloc[-1]
    tol = 0.35 * a
    levels = []
    for _, _, p in sw:
        for lv in levels:
            if abs(lv["p"] - p) <= tol:
                lv["p"] = (lv["p"] * lv["n"] + p) / (lv["n"] + 1)
                lv["n"] += 1
                break
        else:
            levels.append({"p": p, "n": 1})
    strong = [lv for lv in levels if lv["n"] >= 2]
    res = sorted((lv for lv in strong if lv["p"] > price), key=lambda lv: lv["p"])[:3]
    sup = sorted((lv for lv in strong if lv["p"] < price), key=lambda lv: -lv["p"])[:3]
    f = lambda lv: f"{lv['p']:.2f} ({lv['n']} touches)"
    return {"resistance_above": [f(x) for x in res], "support_below": [f(x) for x in sup]}


# ------------------------------------------------------------------ Bollinger
def bollinger(df, n=20, k=2.0):
    c = df["close"]
    mid = c.rolling(n).mean()
    sd = c.rolling(n).std(ddof=0)
    up, lo = mid + k * sd, mid - k * sd
    width = (up - lo) / mid * 100
    pb = (c - lo) / (up - lo)
    w = width.dropna()
    pct = float((w.tail(120) < w.iloc[-1]).mean() * 100) if len(w) else 50.0
    state = "SQUEEZE (low volatility, breakout coming)" if pct <= 15 else \
        "EXPANDING (strong move)" if pct >= 85 and width.iloc[-1] > width.iloc[-3] else "NORMAL"
    last5 = pb.tail(5)
    walk = "walking UPPER band (strong uptrend)" if (last5 > 0.85).sum() >= 3 else \
        "walking LOWER band (strong downtrend)" if (last5 < 0.15).sum() >= 3 else None
    bar = df.iloc[-1]
    signal = None
    if bar.high >= up.iloc[-1] and bar.close < up.iloc[-1]:
        signal = "rejected from upper band"
    elif bar.low <= lo.iloc[-1] and bar.close > lo.iloc[-1]:
        signal = "rejected from lower band"
    out = {"upper": _r(up.iloc[-1]), "middle": _r(mid.iloc[-1]), "lower": _r(lo.iloc[-1]),
           "percent_b": round(float(pb.iloc[-1]), 2), "width_pct": round(float(width.iloc[-1]), 2),
           "width_rank_120": round(pct), "state": state}
    if walk:
        out["band_walk"] = walk
    if signal:
        out["last_candle"] = signal
    return out


# ------------------------------------------------------------------ one timeframe
def analyze(df, k=3):
    if df is None or len(df) < 40:
        return None
    a_series = atr(df)
    a = float(a_series.iloc[-1])
    sw = swings(df, k)
    return {
        "structure": market_structure(df, sw),
        "liquidity": liquidity(df, sw, a),
        "supply_demand": supply_demand(df, a_series),
        "sr_levels": support_resistance(df, sw[-60:], a),
        "bollinger": bollinger(df),
    }
