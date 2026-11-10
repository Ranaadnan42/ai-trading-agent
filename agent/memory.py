"""Agent memory: state, trade journal, and learned lessons (all stored in the repo's data/ folder)."""
import json
import os
from datetime import datetime, timezone

from . import config

STATE = os.path.join(config.DATA_DIR, "state.json")
JOURNAL = os.path.join(config.DATA_DIR, "journal.jsonl")
LESSONS = os.path.join(config.DATA_DIR, "lessons.md")
DECISIONS = os.path.join(config.DATA_DIR, "decisions.jsonl")


def now():
    return datetime.now(timezone.utc)


def iso(dt=None):
    return (dt or now()).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_state():
    os.makedirs(config.DATA_DIR, exist_ok=True)
    if os.path.exists(STATE):
        with open(STATE) as f:
            return json.load(f)
    return {"paper_balance": config.PAPER_START_BALANCE, "open": [], "next_id": 1}


def save_state(s):
    with open(STATE, "w") as f:
        json.dump(s, f, indent=2)


def _append(path, obj):
    with open(path, "a") as f:
        f.write(json.dumps(obj) + "\n")


def _read(path):
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return [json.loads(l) for l in f if l.strip()]


def log_trade(event):
    event.setdefault("time", iso())
    _append(JOURNAL, event)


def log_decision(d):
    d.setdefault("time", iso())
    _append(DECISIONS, d)


def journal():
    return _read(JOURNAL)


def decisions():
    return _read(DECISIONS)


def closed_trades():
    return [e for e in journal() if e.get("event") == "close"]


def trades_opened_on(day):
    return [e for e in journal() if e.get("event") == "open" and e["time"].startswith(day)]


def realized_pnl_on(day):
    return sum(e.get("pnl", 0.0) for e in closed_trades() if e["time"].startswith(day))


def lessons():
    if os.path.exists(LESSONS):
        with open(LESSONS) as f:
            return f.read()
    return "(No lessons yet - this is the first day.)"


def save_lessons(text):
    with open(LESSONS, "w") as f:
        f.write(text.strip() + "\n")


def stats(trades=None):
    trades = closed_trades() if trades is None else trades
    n = len(trades)
    if not n:
        return {"trades": 0}
    wins = [t["pnl"] for t in trades if t["pnl"] > 0]
    losses = [t["pnl"] for t in trades if t["pnl"] <= 0]
    gp, gl = sum(wins), -sum(losses)
    eq, peak, maxdd = 0.0, 0.0, 0.0
    start = config.PAPER_START_BALANCE
    for t in trades:
        eq += t["pnl"]
        peak = max(peak, eq)
        maxdd = max(maxdd, (peak - eq) / (start + peak) * 100)
    return {
        "trades": n,
        "win_rate": round(len(wins) / n * 100, 1),
        "profit_factor": round(gp / gl, 2) if gl else None,
        "net_pnl": round(gp - gl, 2),
        "avg_r": round(sum(t.get("r", 0) for t in trades) / n, 2),
        "max_drawdown_pct": round(maxdd, 2),
    }
