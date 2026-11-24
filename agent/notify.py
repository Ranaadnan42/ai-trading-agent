"""Reports to the owner on WhatsApp through his AI Manager (Telegram optional backup)."""
import os

import requests

WA_API = "https://fobzocsluzwcqretshnm.supabase.co/functions/v1/wa-report?alert=1"


def send(text):
    print("NOTIFY:", text)
    try:
        requests.post(WA_API, json={"text": ("📈 Trading Agent\n" + text)[:3500]}, timeout=30)
    except Exception as e:
        print("whatsapp failed:", e)
    tok, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not (tok and chat):
        return
    try:
        requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                      json={"chat_id": chat, "text": text[:4000]}, timeout=20)
    except Exception as e:
        print("telegram failed:", e)
