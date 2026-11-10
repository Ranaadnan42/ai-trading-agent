import numpy as np
import pandas as pd


def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def rsi(s, n=14):
    d = s.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = up / dn.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(50)


def atr(df, n=14):
    pc = df["close"].shift()
    tr = pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def resample(df, rule):
    o = df.resample(rule, label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last"}
    )
    return o.dropna()


def summarize(df, label, last_n=12):
    """Compact, number-rich picture of one timeframe for the AI."""
    c = df["close"]
    e20, e50, e200 = ema(c, 20), ema(c, 50), ema(c, 200)
    macd = ema(c, 12) - ema(c, 26)
    hist = macd - ema(macd, 9)
    a = atr(df)
    r = rsi(c)
    last = df.iloc[-1]
    trend = "UP" if e20.iloc[-1] > e50.iloc[-1] > e200.iloc[-1] else (
        "DOWN" if e20.iloc[-1] < e50.iloc[-1] < e200.iloc[-1] else "MIXED")
    candles = [
        f"{i.strftime('%m-%d %H:%M')} O{row.open:.2f} H{row.high:.2f} L{row.low:.2f} C{row.close:.2f}"
        for i, row in df.tail(last_n).iterrows()
    ]
    return {
        "timeframe": label,
        "close": round(float(last.close), 2),
        "ema20": round(float(e20.iloc[-1]), 2),
        "ema50": round(float(e50.iloc[-1]), 2),
        "ema200": round(float(e200.iloc[-1]), 2),
        "trend": trend,
        "rsi14": round(float(r.iloc[-1]), 1),
        "macd_hist": round(float(hist.iloc[-1]), 3),
        "macd_hist_prev": round(float(hist.iloc[-2]), 3),
        "atr14": round(float(a.iloc[-1]), 2),
        "high_20": round(float(df["high"].tail(20).max()), 2),
        "low_20": round(float(df["low"].tail(20).min()), 2),
        "high_50": round(float(df["high"].tail(50).max()), 2),
        "low_50": round(float(df["low"].tail(50).min()), 2),
        "recent_candles": candles,
    }
