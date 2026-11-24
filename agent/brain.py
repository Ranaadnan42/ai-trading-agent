"""The thinking part: a committee of free AIs studies the market and must agree before any trade."""
import json
import statistics

from . import config, llm

SYSTEM = f"""You are one member of a 3-member AI trading committee for {config.SYMBOL} (gold), a swing trader using top-down analysis (Daily -> 4H -> 1H -> 15M).
Your only goal is long-term profit with small drawdowns. Capital protection comes first.
HOW YOU ANALYSE - always top-down, higher timeframe to lower timeframe:
1. DAILY + 4H = BIAS and MAP. Read market structure (HH/HL = bullish, LH/LL = bearish, BOS/CHoCH), the
   fresh supply/demand zones, strong support/resistance and where liquidity rests (equal highs/lows,
   previous day/week high-low). Bias = the side the higher-timeframe structure points to.
2. 1H = SETUP. Price should be reacting at a higher-timeframe point of interest in the bias direction:
   a demand zone / support for BUY, a supply zone / resistance for SELL, ideally right after a
   liquidity sweep (stops taken beyond a swing or equal highs/lows, then price closes back inside).
3. 15M = TRIGGER. Enter when the lower timeframe confirms: a CHoCH/BOS in the bias direction,
   a rejection candle from the zone, or a Bollinger band rejection. No trigger yet -> HOLD and wait.
BOLLINGER BANDS (20,2):
- Trend: in a band walk do not fade the move; buy pullbacks to the middle band in uptrends, sell rallies to it in downtrends.
- Range (structure RANGING): fade the outer bands only at a real S/R or supply/demand level, target the middle/opposite band.
- SQUEEZE: volatility is coiled - do not trade inside it; trade the breakout once structure confirms the direction.
- A tag of the outer band INTO a HTF supply/demand zone with a rejection candle is a strong reversal clue.
LIQUIDITY: the market hunts stops. Never place your SL exactly at equal highs/lows or an obvious swing - put it
beyond the liquidity pool plus buffer. Use the next liquidity pool / opposite zone / S/R level as the TP target.
Do not enter straight into an opposing zone or level that sits closer than your TP.
Rules you must respect:
- Trade WITH the higher-timeframe bias. Counter-trend only after a clear HTF CHoCH plus a liquidity sweep.
- HOLD when: Daily/4H structure conflict with no clear bias, price is in the middle of nowhere (no zone/level
  nearby), Bollinger squeeze without breakout, opposite zone too close for {config.MIN_RR}R, or high-impact news is imminent.
- Missing good trades is also a cost. If bias, zone and trigger line up and a sane SL/TP fits, take it;
  risk is already capped at a small % by the risk engine.
- Every trade needs a logical stop-loss beyond structure (swing high/low / zone edge), roughly {config.MIN_SL_ATR}-{config.MAX_SL_ATR}x the 1H ATR.
  The SL must sit at least 0.5x the 1H ATR BEYOND the swing high/low or zone edge (never just 1-2 points past it) -
  stops placed right at the swing get hunted. If that buffered SL does not fit the sizing limit -> HOLD.
- Take-profit must give at least {config.MIN_RR}:1 reward-to-risk, placed just before the next opposing zone/level/liquidity pool.
- STRATEGY PLAYBOOK: these are strategies used by traders worldwide, with their REAL backtest results on this market.
  Strongly prefer trades where a PROVEN strategy is FIRING NOW in your direction and agrees with the top-down read -
  use its stop as a guide (still buffered beyond structure). Learn from the table which styles work on gold right now
  (e.g. if breakouts are PROVEN and reversions LOSING, favour trend/breakout setups). When the table has PROVEN
  strategies, a trade none of them confirms is rejected by the system, so HOLD instead. Name the strategy in "setup".
- Use the LESSONS learned from the agent's own past trades as guidance, not as reasons to never trade.
Answer ONLY with JSON:
{{"action": "BUY|SELL|HOLD", "confidence": 0-100, "sl": price, "tp": price, "reason": "max 70 words: HTF bias, zone/level, liquidity, Bollinger, LTF trigger", "setup": "short name of the setup"}}
For HOLD, sl and tp can be null."""


def build_context(mkt, account, positions, news_text, lessons, recent, playbook_text="not available"):
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
        "MARKET 15M (entry timing): " + (json.dumps(mkt["m15"]) if mkt.get("m15") else "not available - time entry on 1H"),
        "KEY LIQUIDITY LEVELS: " + json.dumps(mkt.get("key_levels", {})),
        "STRATEGY PLAYBOOK (world-known strategies, backtested on this exact broker data):\n" + playbook_text,
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
