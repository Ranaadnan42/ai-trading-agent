"""Playbook: well-known strategies used by gold / FX traders worldwide, each backtested on the agent's own
broker candles every cycle. The AI committee is told which strategies are firing right now and how each one
has actually performed (win rate, profit factor, R), so it learns from what really works on XAUUSD.

Strategies (all on 1H, signal on a CLOSED bar, entry next bar, fixed SL/TP, no look-ahead):
 1 ema_pullback        Trend pullback to EMA20/50 in an EMA50>EMA200 trend (classic prop-firm trend play)
 2 turtle_breakout     Richard Dennis Turtle / Donchian 20-bar breakout with EMA200 filter
 3 london_breakout     Break of the Asian session range during London open (07-10 UTC)
 4 asian_sweep         ICT "Judas swing": London fakes out the Asian high/low, closes back inside -> reverse
 5 pdh_pdl_sweep       Smart-money liquidity sweep of previous-day high/low with rejection
 6 connors_rsi2        Larry Connors RSI(2) oversold/overbought pullback inside the EMA200 trend
 7 bb_squeeze_break    John Bollinger squeeze -> breakout close outside the band
 8 bb_reversion        Bollinger band rejection back inside the band while the market is ranging
 9 macd_trend          MACD signal-line cross beyond zero-line side, with EMA200 trend
10 engulf_extreme      Engulfing candle at a 20-bar high/low (price-action reversal)
11 ny_continuation     New York session (13-16 UTC) pullback-and-go in the direction of the London move
"""
import json
import os

import numpy as np
import pandas as pd

from . import config
from .indicators import atr, ema, rsi

PATH = os.path.join(config.DATA_DIR, "playbook.json")
MAX_HOLD = 72          # bars; a trade still open after 3 days is closed at market
COST = 0.35            # spread + slippage in $ per trade (gold)

NAMES = {
    "ema_pullback": "EMA trend pullback",
    "turtle_breakout": "Turtle 20-bar breakout",
    "london_breakout": "London breakout of Asian range",
    "asian_sweep": "Asian range sweep reversal (ICT Judas swing)",
    "pdh_pdl_sweep": "Previous-day high/low liquidity sweep",
    "connors_rsi2": "Connors RSI-2 pullback",
    "bb_squeeze_break": "Bollinger squeeze breakout",
    "bb_reversion": "Bollinger band reversion (range)",
    "macd_trend": "MACD cross with EMA200 trend",
    "engulf_extreme": "Engulfing at 20-bar extreme",
    "ny_continuation": "New York continuation of London move",
}


def features(df):
    f = df[["open", "high", "low", "close"]].copy()
    c = f["close"]
    f["atr"] = atr(f)
    f["e20"], f["e50"], f["e200"] = ema(c, 20), ema(c, 50), ema(c, 200)
    f["rsi2"], f["rsi14"] = rsi(c, 2), rsi(c, 14)
    mid = c.rolling(20).mean()
    sd = c.rolling(20).std()
    f["bb_mid"], f["bb_up"], f["bb_lo"] = mid, mid + 2 * sd, mid - 2 * sd
    width = (f["bb_up"] - f["bb_lo"]) / mid
    f["bb_rank"] = width.rolling(120).rank(pct=True)
    macd = ema(c, 12) - ema(c, 26)
    f["macd"], f["macd_sig"] = macd, ema(macd, 9)
    f["hi20"] = f["high"].shift(1).rolling(20).max()
    f["lo20"] = f["low"].shift(1).rolling(20).min()
    f["hour"] = f.index.hour
    day = f.index.floor("D")
    f["day"] = day
    asia = f[f["hour"] < 7].groupby("day").agg(a_hi=("high", "max"), a_lo=("low", "min"))
    f = f.join(asia, on="day")
    f.loc[f["hour"] < 7, ["a_hi", "a_lo"]] = np.nan          # Asian range only known after 07:00
    daily = f.groupby("day").agg(d_hi=("high", "max"), d_lo=("low", "min"))
    daily[["pdh", "pdl"]] = daily[["d_hi", "d_lo"]].shift(1).values
    f = f.join(daily[["pdh", "pdl"]], on="day")
    lon = f[(f["hour"] >= 7) & (f["hour"] < 12)].groupby("day")["close"].last()
    lon_open = f[f["hour"] == 7].groupby("day")["open"].first()
    f = f.join((lon - lon_open).rename("lon_move"), on="day")
    f.loc[f["hour"] < 12, "lon_move"] = np.nan
    return f


def signals(f):
    """Return {strategy: (buy_mask, sell_mask, sl_buy, sl_sell, rr)} - SL levels are absolute prices."""
    o, h, l, c, a = f["open"], f["high"], f["low"], f["close"], f["atr"]
    bull, bear = c > o, c < o
    up_tr = (f["e50"] > f["e200"]) & (c > f["e200"])
    dn_tr = (f["e50"] < f["e200"]) & (c < f["e200"])
    swing_lo, swing_hi = l.rolling(5).min(), h.rolling(5).max()
    rng = (f["e50"] - f["e50"].shift(20)).abs() < 1.0 * a                    # flat EMA50 = ranging
    s = {}
    s["ema_pullback"] = (up_tr & (l <= f["e20"]) & (c > f["e20"]) & bull & (f["rsi14"] > 40),
                         dn_tr & (h >= f["e20"]) & (c < f["e20"]) & bear & (f["rsi14"] < 60),
                         swing_lo - 0.5 * a, swing_hi + 0.5 * a, 2.0)
    s["turtle_breakout"] = ((c > f["hi20"]) & (c > f["e200"]), (c < f["lo20"]) & (c < f["e200"]),
                            c - 2 * a, c + 2 * a, 2.0)
    a_rng = f["a_hi"] - f["a_lo"]
    lon = (f["hour"] >= 7) & (f["hour"] < 10) & (a_rng < 2.5 * a)
    s["london_breakout"] = (lon & (c > f["a_hi"]) & (c.shift() <= f["a_hi"]),
                            lon & (c < f["a_lo"]) & (c.shift() >= f["a_lo"]),
                            np.maximum(f["a_lo"], c - 2.5 * a) - 0.3 * a, np.minimum(f["a_hi"], c + 2.5 * a) + 0.3 * a, 2.0)
    js = (f["hour"] >= 7) & (f["hour"] < 12)
    s["asian_sweep"] = (js & (l < f["a_lo"]) & (c > f["a_lo"]) & bull,
                        js & (h > f["a_hi"]) & (c < f["a_hi"]) & bear,
                        l - 0.5 * a, h + 0.5 * a, 2.0)
    body = (c - o).abs()
    s["pdh_pdl_sweep"] = ((l < f["pdl"]) & (c > f["pdl"]) & ((np.minimum(o, c) - l) > body),
                          (h > f["pdh"]) & (c < f["pdh"]) & ((h - np.maximum(o, c)) > body),
                          l - 0.5 * a, h + 0.5 * a, 2.0)
    s["connors_rsi2"] = (up_tr & (f["rsi2"] < 10), dn_tr & (f["rsi2"] > 90), c - 1.5 * a, c + 1.5 * a, 1.5)
    sq = f["bb_rank"].shift() < 0.2
    s["bb_squeeze_break"] = (sq & (c > f["bb_up"]), sq & (c < f["bb_lo"]),
                             np.minimum(f["bb_mid"], c - 0.8 * a) - 0.5 * a, np.maximum(f["bb_mid"], c + 0.8 * a) + 0.5 * a, 2.0)
    s["bb_reversion"] = (rng & (l < f["bb_lo"]) & (c > f["bb_lo"]) & bull,
                         rng & (h > f["bb_up"]) & (c < f["bb_up"]) & bear,
                         l - 0.5 * a, h + 0.5 * a, 1.5)
    xu = (f["macd"] > f["macd_sig"]) & (f["macd"].shift() <= f["macd_sig"].shift())
    xd = (f["macd"] < f["macd_sig"]) & (f["macd"].shift() >= f["macd_sig"].shift())
    s["macd_trend"] = (up_tr & xu & (f["macd"] < 0), dn_tr & xd & (f["macd"] > 0), c - 1.5 * a, c + 1.5 * a, 2.0)
    eng_up = bull & (c > o.shift()) & (o < c.shift()) & (c.shift() < o.shift())
    eng_dn = bear & (c < o.shift()) & (o > c.shift()) & (c.shift() > o.shift())
    s["engulf_extreme"] = (eng_up & (l.rolling(2).min() <= f["lo20"]), eng_dn & (h.rolling(2).max() >= f["hi20"]),
                           l.rolling(2).min() - 0.5 * a, h.rolling(2).max() + 0.5 * a, 2.0)
    ny = (f["hour"] >= 13) & (f["hour"] < 16)
    s["ny_continuation"] = (ny & (f["lon_move"] > 0.8 * a) & (l <= f["e20"]) & (c > f["e20"]) & bull,
                            ny & (f["lon_move"] < -0.8 * a) & (h >= f["e20"]) & (c < f["e20"]) & bear,
                            swing_lo - 0.5 * a, swing_hi + 0.5 * a, 2.0)
    return s


def _sane(entry, sl, a):
    d = abs(entry - sl)
    return config.MIN_SL_ATR * a <= d <= config.MAX_SL_ATR * a


def backtest(f, sig):
    O, H, L, C, A = (f[k].values for k in ("open", "high", "low", "close", "atr"))
    hours = f["hour"].values
    times = f.index
    n = len(f)
    out = {}
    for name, (buy, sell, slb, sls, rr) in sig.items():
        buy, sell = buy.fillna(False).values, sell.fillna(False).values
        slb, sls = np.asarray(slb, float), np.asarray(sls, float)
        trades, i = [], 200
        while i < n - 2:
            side = 1 if buy[i] else -1 if sell[i] else 0
            if not side or hours[i] in (21, 22):
                i += 1
                continue
            entry = O[i + 1]
            sl = slb[i] if side == 1 else sls[i]
            if np.isnan(sl) or (sl - entry) * side >= 0 or not _sane(entry, sl, A[i]):
                i += 1
                continue
            risk = abs(entry - sl)
            tp = entry + side * rr * risk
            r, j = None, i + 1
            for j in range(i + 1, min(n, i + 1 + MAX_HOLD)):
                if (side == 1 and L[j] <= sl) or (side == -1 and H[j] >= sl):
                    r = -1.0
                    break
                if (side == 1 and H[j] >= tp) or (side == -1 and L[j] <= tp):
                    r = rr
                    break
            if r is None:
                if j >= n - 1 and j < i + MAX_HOLD:
                    break                       # still open at the end of data -> not counted
                r = (C[j] - entry) * side / risk
            r -= COST / risk
            trades.append((times[i], side, round(r, 3)))
            i = j + 1
        out[name] = trades
    return out


def _stats(rs):
    if not rs:
        return {"trades": 0}
    rs = np.array(rs)
    wins, losses = rs[rs > 0], rs[rs <= 0]
    pf = wins.sum() / -losses.sum() if losses.sum() < 0 else 99.0
    eq = np.cumsum(rs)
    dd = float((np.maximum.accumulate(eq) - eq).max())
    streak = mx = 0
    for r in rs:
        streak = streak + 1 if r <= 0 else 0
        mx = max(mx, streak)
    return {"trades": len(rs), "win_rate": round(100 * len(wins) / len(rs), 1), "profit_factor": round(float(pf), 2),
            "avg_r": round(float(rs.mean()), 3), "total_r": round(float(rs.sum()), 1),
            "max_dd_r": round(dd, 1), "max_loss_streak": mx,
            "t_stat": round(float(rs.mean() / (rs.std(ddof=1) / np.sqrt(len(rs)))), 2) if len(rs) > 2 and rs.std() > 0 else 0}


def live_results():
    """How each playbook strategy did in the agent's REAL trades (journal 'playbook' tags)."""
    from . import memory
    opens = {e["id"]: e for e in memory.journal() if e.get("event") == "open"}
    res = {}
    for t in memory.closed_trades():
        for name in (opens.get(t.get("id"), {}).get("playbook") or []):
            res.setdefault(name, []).append(t.get("r") or 0)
    return {k: {"trades": len(v), "total_r": round(sum(v), 2)} for k, v in res.items()}


def evaluate(df, now=None):
    """Backtest every strategy on df and list what fires on the latest closed bars."""
    f = features(df)
    now = now or pd.Timestamp.now(tz="UTC")
    if len(f) and f.index[-1] + pd.Timedelta(hours=1) > now:
        f_closed = f.iloc[:-1]                         # last bar is still forming
    else:
        f_closed = f
    sig = signals(f_closed)
    bt = backtest(f_closed, sig)
    recent_from = f_closed.index[-1] - pd.Timedelta(days=90) if len(f_closed) else None
    live = live_results()
    table = {}
    for name, trades in bt.items():
        st = _stats([r for _, _, r in trades])
        st["last_90d"] = _stats([r for t, _, r in trades if t >= recent_from])
        st["live"] = live.get(name, {"trades": 0})
        s90 = st["last_90d"]
        st["proven"] = bool(st["trades"] >= 30 and st.get("profit_factor", 0) >= 1.25 and st.get("avg_r", 0) > 0.1
                            and st.get("t_stat", 0) >= 2.0                                    # real edge, not luck
                            and (s90.get("trades", 0) < 5 or s90.get("avg_r", 0) >= 0)
                            and st["live"].get("total_r", 0) > -4)          # real losses can demote a strategy
        st["status"] = "PROVEN" if st["proven"] else ("LOSING" if st.get("profit_factor", 1) < 0.95 else "WEAK")
        table[name] = st

    firing = []
    last = f_closed.iloc[-3:]
    for name, (buy, sell, slb, sls, rr) in sig.items():
        for k in range(len(last)):
            idx = last.index[k]
            pos = f_closed.index.get_loc(idx)
            for side, mask, sl in (("BUY", buy, slb), ("SELL", sell, sls)):
                if bool(mask.fillna(False).iloc[pos]):
                    slv = float(np.asarray(sl, float)[pos])
                    if not np.isnan(slv):
                        firing.append({"strategy": name, "side": side, "bars_ago": len(last) - 1 - k,
                                       "sl": round(slv, 2), "rr": rr, "status": table[name]["status"]})
    report = {"computed_at": str(now), "bars_tested": int(len(f_closed)),
              "from": str(f_closed.index[0]) if len(f_closed) else None, "strategies": table, "firing": firing}
    try:
        with open(PATH, "w") as fh:
            json.dump(report, fh, indent=1, default=str)
    except OSError:
        pass
    return report


def for_prompt(rep):
    ranked = sorted(rep["strategies"].items(), key=lambda kv: kv[1].get("avg_r", -9), reverse=True)
    lines = [f"Backtested on {rep['bars_tested']} 1H bars of this broker's {config.SYMBOL} since {str(rep['from'])[:10]} "
             f"(fixed SL/TP, costs included):"]
    for name, st in ranked:
        if not st["trades"]:
            lines.append(f"- {NAMES[name]}: no trades")
            continue
        s90 = st["last_90d"]
        lines.append(f"- [{st['status']}] {NAMES[name]}: {st['trades']} trades, win {st['win_rate']}%, PF {st['profit_factor']}, "
                     f"avg {st['avg_r']}R, last90d avg {s90.get('avg_r', '-')}R ({s90.get('trades', 0)} tr), "
                     f"live {st['live'].get('trades', 0)} tr {st['live'].get('total_r', 0)}R")
    if rep["firing"]:
        lines.append("FIRING NOW (signal on the last closed 1H bars):")
        for s in rep["firing"]:
            lines.append(f"- {s['side']} by {NAMES[s['strategy']]} [{s['status']}], {s['bars_ago']} bar(s) ago, "
                         f"its stop {s['sl']}, target {s['rr']}R")
    else:
        lines.append("FIRING NOW: none")
    return "\n".join(lines)


def confirming(rep, side):
    """Proven strategies that fired in this direction on the latest closed bars."""
    return sorted({s["strategy"] for s in rep.get("firing", []) if s["side"] == side and s["status"] == "PROVEN"})


def any_proven(rep):
    return any(st.get("proven") for st in rep.get("strategies", {}).values())
