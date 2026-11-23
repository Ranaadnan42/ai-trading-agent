"""The thinking part: a committee of free AIs studies the market and must agree before any trade."""
import json
import statistics

from . import config, llm

SYSTEM = f"""You are one member of a 3-member AI trading committee for {config.SYMBOL} (gold), a swing trader on 1H/4H/Daily charts.
Your only goal is long-term profit with small drawdowns. Capital protection comes first.
Rules you must respect:
- Trade WITH the higher-timeframe trend (Daily/4H). In a clear trend, good entries are: a pullback to 1H EMA20/EMA50,
  a break-and-retest of a level, or a 1H continuation candle after a small consolidation. Strong trends stay
  "overbought/oversold" for a long time, so RSI extremes alone are NOT a reason to skip a trend trade.
- HOLD only when there is a concrete reason: no clear trend (mixed/choppy), price sits right at a major level
  against your direction, the stop would have to be unreasonably wide, or high-impact news is imminent.
- Missing good trades is also a cost. If the trend is clear and a sane SL/TP fits, take the trade; risk is
  already capped at a small % by the risk engine.
- Every trade needs a logical stop-loss beyond recent structure (swing high/low), roughly {config.MIN_SL_ATR}-{config.MAX_SL_ATR}x the 1H ATR.
- Take-profit must give at least {config.MIN_RR}:1 reward-to-risk, placed before obvious resistance/support.
- Avoid trading straight into major levels, during choppy/MIXED conditions, or right before high-impact news.
- Use the LESSONS learned from the agent's own past trades as guidance, not as reasons to never trade.
Answer ONLY with JSON:
{{"action": "BUY|SELL|HOLD", "confidence": 0-100, "sl": price, "tp": price, "reason": "max 60 words", "setup": "short name of the setup"}}
For HOLD, sl and tp can be null."""


def build_context(mkt, account, positions, news_text, lessons, recent):
    return "\n".join([
        f"ACCOUNT: balance {account['balance']:.2f}, equity {account['equity']:.2f}. Open positions: {json.dumps(positions) if positions else 'none'}",
        f"CURRENT PRICE: bid {mkt['bid']:.2f} ask {mkt['ask']:.2f} (UTC {mkt['time']})",
        f"SIZING LIMIT: small account - the stop-loss can be at most "
        f"{account['balance'] * max(config.RISK_PCT, config.MIN_LOT_MAX_RISK_PCT) / 100 / (0.01 * config.CONTRACT_SIZE):.1f} "
        f"points from entry (and at least {config.MIN_SL_ATR * mkt['atr']:.1f}). Pick an entry/structure where that fits, else HOLD.",
        f"NEWS: {news_text}",
        "MARKET DAILY: " + json.dumps(mkt["d1"]),
        "MARKET 4H: " + json.dumps(mkt["h4"]),
        "MARKET 1H: " + json.dumps(mkt["h1"]),
        "RECENT CLOSED TRADES: " + (json.dumps(recent) if recent else "none yet"),
        "LESSONS LEARNED:\n" + lessons,
        "Decide now.",
    ])


def _valid(v, price):
    try:
        a = str(v.get("action", "HOLD")).upper()
        if a not in ("BUY", "SELL"):
            return "HOLD"
        sl, tp, conf = float(v["sl"]), float(v["tp"]), float(v.get("confidence", 0))
        if conf < config.MIN_CONFIDENCE:
            return "HOLD"
        if a == "BUY" and sl < price < tp:
            return a
        if a == "SELL" and tp < price < sl:
            return a
    except (TypeError, ValueError, KeyError):
        pass
    return "HOLD"


def committee_decide(context, mkt, ask=llm.ask):
    members = llm.available_members()
    mid = (mkt["bid"] + mkt["ask"]) / 2
    votes = []
    spares = [b for b in llm.BACKUPS if b not in members and llm.has_key(b)]
    for m in members:
        js, raw = ask(m, SYSTEM, context)
        while js is None and spares:      # member failed (e.g. 429) -> a backup AI takes its seat
            votes.append({"member": m["name"], "action": "ERROR", "raw": raw[:300]})
            m = spares.pop(0)
            js, raw = ask(m, SYSTEM, context)
        if js is None:
            votes.append({"member": m["name"], "action": "ERROR", "raw": raw[:300]})
            continue
        votes.append({"member": m["name"], "action": _valid(js, mid), "said": js})
    buys = [v for v in votes if v["action"] == "BUY"]
    sells = [v for v in votes if v["action"] == "SELL"]
    decision = {"action": "HOLD", "votes": votes}
    for side, group, other in (("BUY", buys, sells), ("SELL", sells, buys)):
        if len(group) >= config.MIN_AGREE and not other:
            decision.update({
                "action": side,
                "sl": statistics.median(float(v["said"]["sl"]) for v in group),
                "tp": statistics.median(float(v["said"]["tp"]) for v in group),
                "confidence": statistics.mean(float(v["said"]["confidence"]) for v in group),
                "reason": " | ".join(f"{v['member']}: {v['said'].get('reason', '')}" for v in group),
                "setup": group[0]["said"].get("setup", ""),
            })
    return decision
