"""Reports to the owner on Telegram (WhatsApp comes later with the AI Manager)."""
import os

import requests


def send(text):
    print("NOTIFY:", text)
    tok, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not (tok and chat):
        return
    try:
        requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                      json={"chat_id": chat, "text": text[:4000]}, timeout=20)
    except Exception as e:
        print("telegram failed:", e)
