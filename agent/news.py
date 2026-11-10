"""High-impact USD news filter (NFP, CPI, FOMC...). Free weekly calendar feed."""
from datetime import datetime, timedelta, timezone

import requests

from . import config

FEED = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"


def upcoming_events():
    try:
        data = requests.get(FEED, timeout=20, headers={"User-Agent": "Mozilla/5.0"}).json()
    except Exception as e:
        print("news feed unavailable:", e)
        return None
    out = []
    for ev in data:
        if ev.get("country") != "USD" or ev.get("impact") != "High":
            continue
        try:
            t = datetime.fromisoformat(ev["date"]).astimezone(timezone.utc)
        except Exception:
            continue
        out.append({"title": ev.get("title"), "time": t})
    return out


def news_status(now=None):
    """Returns (blocked: bool, text for the AI)."""
    now = now or datetime.now(timezone.utc)
    evs = upcoming_events()
    if evs is None:
        return False, "News calendar unavailable - be extra careful."
    win = timedelta(minutes=config.NEWS_BLOCK_MINUTES)
    blocking = [e for e in evs if abs(e["time"] - now) <= win]
    soon = [e for e in evs if timedelta(0) < e["time"] - now <= timedelta(hours=24)]
    txt = "; ".join(f"{e['title']} at {e['time']:%a %H:%M} UTC" for e in soon) or "No high-impact USD news in next 24h."
    if blocking:
        return True, "BLOCKED by news: " + ", ".join(e["title"] for e in blocking)
    return False, txt
