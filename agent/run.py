"""One agent cycle (GitHub Actions runs this every hour).

1. Find trades that closed since last run -> journal + instant reflection (learning per trade)
2. Manage open trade (move stop to breakeven after +1R)
3. Safety gates (market open, daily loss, max trades, news, spread)
4. AI committee studies the market; trade only if 2+ agree and the risk engine approves
"""
import json
import sys

import pandas as pd

from . import brain, config, llm, memory, news, notify, risk, structure
from .broker import get_broker
from .indicators import atr, resample, summarize


def _tf(broker, tf):
    try:
        return broker.candles_tf(tf) if hasattr(broker, "candles_tf") else None
    except Exception as e:
        print(f"{tf} candles failed:", e)
        return None


def _with_structure(summary, df, k=3):
    a = structure.analyze(df, k)
    if a:
        summary.update(a)
    return summary


def market_snapshot(broker):
    """Top-down picture: Daily -> 4H -> 1H -> 15M, each with structure, liquidity, zones, S/R and Bollinger."""
    h1 = broker.candles()
    p = broker.price()
    d1 = _tf(broker, "1d")
    if d1 is None or len(d1) < 60:
        d1 = resample(h1, "1D")
    h4 = resample(h1, "4h")
    m15 = _tf(broker, "15m")
    snap = {
        "bid": p["bid"], "ask": p["ask"], "time": str(p["time"]),
        "d1": _with_structure(summarize(d1, "1D", last_n=5), d1, k=2),
        "h4": _with_structure(summarize(h4, "4H", last_n=8), h4),
        "h1": _with_structure(summarize(h1, "1H"), h1),
        "key_levels": structure.session_levels(h1),
        "atr": float(atr(h1).iloc[-1]),
        "last_bar": h1.index[-1],
    }
    if m15 is not None and len(m15) >= 100:
        snap["m15"] = _with_structure(summarize(m15, "15M", last_n=8), m15, k=2)
    return snap


def reflect(trade):
    sys_p = ("You are the trading agent reviewing your own just-closed trade. Be honest and specific. "
             "Write ONE practical lesson (max 40 words) you should apply to future trades.")
    txt, who = llm.ask_text(sys_p, json.dumps(trade, default=str))
    return txt


def handle_closed(broker, opens_by_id):
    for c in broker.sync_closed():
        meta = opens_by_id.get(c["id"], {})
        risk_usd = meta.get("risk_usd") or abs(c["entry"] - c["initial_sl"]) * c["lots"] * config.CONTRACT_SIZE
        r_mult = round(c["pnl"] / risk_usd, 2) if risk_usd else 0
        rec = {"event": "close", "id": c["id"], "side": c["side"], "entry": c["entry"], "exit": c["exit"],
               "exit_reason": c["exit_reason"], "pnl": c["pnl"], "r": r_mult, "lots": c["lots"],
               "setup": meta.get("setup"), "open_time": c.get("open_time")}
        rec["lesson"] = reflect({**rec, "entry_reasons": meta.get("reason")})
        memory.log_trade(rec)
        notify.send(f"{'✅' if c['pnl'] > 0 else '❌'} {config.SYMBOL} {c['side']} closed by {c['exit_reason']}: "
                    f"{c['pnl']:+.2f}$ ({r_mult:+.2f}R)\nLesson: {rec['lesson'] or '-'}")


def manage_open(broker, positions, mkt):
    for p in positions:
        if not p.get("sl") or not p.get("initial_sl"):
            continue
        r_dist = abs(p["entry"] - p["initial_sl"])
        price = mkt["bid"] if p["side"] == "BUY" else mkt["ask"]
        gain = (price - p["entry"]) if p["side"] == "BUY" else (p["entry"] - price)
        at_be = (p["sl"] >= p["entry"]) if p["side"] == "BUY" else (p["sl"] <= p["entry"])
        if r_dist and gain >= config.BREAKEVEN_AT_R * r_dist and not at_be:
            buf = 0.1 * r_dist
            new_sl = round(p["entry"] + buf if p["side"] == "BUY" else p["entry"] - buf, 2)
            broker.modify_sl(p["id"], new_sl)
            notify.send(f"🔒 {config.SYMBOL} {p['side']} +1R reached, stop moved to breakeven ({new_sl}).")


def main():
    state = memory.load_state()
    broker = get_broker(state)
    today = memory.iso()[:10]
    try:
        opens_by_id = {e["id"]: e for e in memory.journal() if e.get("event") == "open"}
        handle_closed(broker, opens_by_id)

        mkt = market_snapshot(broker)
        positions = broker.positions()
        manage_open(broker, positions, mkt)
        positions = broker.positions()
        acct = broker.account()

        def skip(why):
            memory.log_decision({"action": "SKIP", "why": why, "price": mkt["bid"]})
            print("SKIP:", why)

        age_h = (pd.Timestamp.now(tz="UTC") - mkt["last_bar"]).total_seconds() / 3600
        if age_h > 3:
            return skip(f"market closed / stale data ({age_h:.1f}h old)")
        guard = risk.daily_guard(acct["balance"], acct["equity"], memory.realized_pnl_on(today),
                                 len(memory.trades_opened_on(today)), len(positions))
        if guard:
            return skip(guard)
        spread = mkt["ask"] - mkt["bid"]
        if spread > config.MAX_SPREAD:
            return skip(f"spread too wide ({spread:.2f})")
        blocked, news_text = news.news_status()
        if blocked:
            return skip(news_text)
        if len(llm.available_members()) < config.MIN_AGREE:
            return skip("not enough AI keys configured")

        recent = [{k: t.get(k) for k in ("side", "pnl", "r", "exit_reason", "setup", "lesson")}
                  for t in memory.closed_trades()[-5:]]
        ctx = brain.build_context(mkt, acct, positions, news_text, memory.lessons(), recent)
        d = brain.committee_decide(ctx, mkt)
        log = {"action": d["action"], "price": mkt["bid"],
               "votes": [{"member": v["member"], "action": v["action"],
                          "reason": (v.get("said") or {}).get("reason") or v.get("raw", "")[:150]} for v in d["votes"]]}

        if d["action"] in ("BUY", "SELL"):
            side = d["action"]
            entry = mkt["ask"] if side == "BUY" else mkt["bid"]
            lv = risk.check_levels(side, entry, d["sl"], d["tp"], mkt["atr"])
            if isinstance(lv, str):
                log.update(action="REJECTED", why=lv)
            else:
                sl, tp = lv
                limits = broker.lot_limits() if hasattr(broker, "lot_limits") else (0.01, 100, 0.01, config.CONTRACT_SIZE)
                lots, risk_usd = risk.lot_size(acct["balance"], entry, sl, *limits)
                if lots <= 0:
                    log.update(action="REJECTED", why="lot size too small for risk limit")
                else:
                    pos = broker.open(side, lots, sl, tp, comment=f"ai {d.get('setup', '')}")
                    memory.log_trade({"event": "open", "id": pos["id"], "side": side, "entry": pos["entry"],
                                      "sl": sl, "tp": tp, "lots": lots, "risk_usd": risk_usd,
                                      "confidence": round(d["confidence"]), "setup": d.get("setup"),
                                      "reason": d["reason"], "mode": broker.name})
                    notify.send(f"📈 {config.SYMBOL} {side} {lots} lots @ {pos['entry']:.2f}\n"
                                f"SL {sl} | TP {tp} | risk {risk_usd}$ ({config.RISK_PCT}%)\n"
                                f"Mode: {broker.name}\nWhy: {d['reason'][:600]}")
        memory.log_decision(log)
        print(json.dumps(log, indent=1, default=str))
    finally:
        memory.save_state(state)
        broker.close()


if __name__ == "__main__":
    sys.exit(main())
