"""Daily learning (runs every night after the New York close).

Reads the day's trades, skipped/HOLD decisions and per-trade lessons, then rewrites
data/lessons.md - the agent's playbook that every hourly decision reads.
Risk rules in config.py are NOT touched; only the agent's judgment improves.
"""
import json
from datetime import timedelta

from . import config, llm, memory, notify

SYSTEM = """You are the trading agent doing your nightly self-review. You trade gold swing setups.
Look at today's trades, the decisions where you held or were rejected, and your current lessons.
Rewrite your LESSONS playbook: keep what is proven, fix what failed, add new specific lessons, remove vague or
contradicted ones. Max 10 bullet points, each concrete and actionable (conditions -> what to do).
Never suggest increasing risk, removing stop-losses, or breaking hard rules.
Balance matters: lessons must come from REAL closed trades. If there were few or no trades, do NOT add new
"do not trade" rules - instead remove or relax rules that blocked reasonable trend trades (an agent that never
trades cannot learn or earn). Keep at most 10 bullets, and at least half must describe when TO take a trade.
Reply as JSON: {"lessons": "- bullet\\n- bullet ...", "summary": "2-3 sentence honest summary of today"}"""


def main():
    today = memory.iso()[:10]
    since = memory.iso(memory.now() - timedelta(days=1))
    day_trades = [t for t in memory.closed_trades() if t["time"] >= since]
    day_decisions = [d for d in memory.decisions() if d["time"] >= since]
    all_stats = memory.stats()
    day_stats = memory.stats(day_trades)

    payload = {
        "today_stats": day_stats, "all_time_stats": all_stats,
        "today_trades": day_trades,
        "today_decisions": [{k: d.get(k) for k in ("time", "action", "why", "price", "votes")} for d in day_decisions][-30:],
        "current_lessons": memory.lessons(),
    }
    summary = "No AI available for review."
    for m in llm.available_members():
        js, raw = llm.ask(m, SYSTEM, json.dumps(payload, default=str)[:60000])
        if js and js.get("lessons"):
            header = (f"# Lessons playbook (updated {today} by {m['name']})\n"
                      f"<!-- all-time: {json.dumps(all_stats)} -->\n\n")
            memory.save_lessons(header + js["lessons"])
            summary = js.get("summary", "")
            break

    ok = (all_stats.get("trades", 0) >= 30 and (all_stats.get("profit_factor") or 0) > 1.3
          and all_stats.get("max_drawdown_pct", 100) < 10)
    notify.send(
        f"📊 Daily report {today} ({config.MODE})\n"
        f"Today: {json.dumps(day_stats)}\nAll-time: {json.dumps(all_stats)}\n"
        f"Real-money test passed: {'YES' if ok else 'not yet (need 30+ trades, PF>1.3, DD<10%)'}\n\n{summary}"
    )


if __name__ == "__main__":
    main()
