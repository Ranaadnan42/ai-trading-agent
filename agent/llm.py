"""Free AI brains: Google Gemini, Groq, OpenRouter (free models). No paid API needed."""
import json
import os
import re
import time

import requests

TIMEOUT = 90

# Committee members. A member without its key is skipped automatically.
COMMITTEE = [
    {"name": "Gemini", "provider": "gemini", "model": os.getenv("GEMINI_MODEL", "gemini-3.5-flash")},
    {"name": "Groq-Llama", "provider": "groq", "model": os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")},
    {"name": "OpenRouter", "provider": "openrouter", "model": os.getenv("OPENROUTER_MODEL", "auto-free")},
]
KEYS = {"gemini": "GEMINI_API_KEY", "groq": "GROQ_API_KEY", "openrouter": "OPENROUTER_API_KEY"}


# Backups: used only to fill the committee up to 3 when a main member has no key.
BACKUPS = [
    {"name": "Groq-Qwen", "provider": "groq", "model": "qwen/qwen3-32b"},
    {"name": "Gemini-Lite", "provider": "gemini", "model": "gemini-flash-lite-latest"},
    {"name": "Groq-GPT-OSS", "provider": "groq", "model": "openai/gpt-oss-120b"},
]


def has_key(m):
    return bool(os.getenv(KEYS[m["provider"]]))


def available_members():
    have = has_key
    members = [m for m in COMMITTEE if have(m)]
    for b in BACKUPS:
        if len(members) >= 3:
            break
        if have(b):
            members.append(b)
    return members


def _gemini(model, system, user):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    body = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json"},
    }
    r = requests.post(url, params={"key": os.environ["GEMINI_API_KEY"]}, json=body, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()["candidates"][0]["content"]["parts"][0]["text"]


def _openai_style(url, key, model, system, user, extra_headers=None):
    headers = {"Authorization": f"Bearer {key}", **(extra_headers or {})}
    body = {"model": model, "temperature": 0.2,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    r = requests.post(url, headers=headers, json=body, timeout=TIMEOUT)
    if r.status_code >= 400:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
    return r.json()["choices"][0]["message"]["content"]


_groq_cache = None


def _groq_live_models(prefer):
    """Groq retires models often; pick working chat models live from its /models list."""
    global _groq_cache
    if _groq_cache is None:
        try:
            r = requests.get("https://api.groq.com/openai/v1/models", timeout=30,
                             headers={"Authorization": f"Bearer {os.environ['GROQ_API_KEY']}"})
            skip = ("whisper", "tts", "guard", "embed", "orpheus", "playai", "compound", "prompt")
            _groq_cache = [m["id"] for m in r.json().get("data", [])
                           if m.get("active", True) and not any(x in m["id"].lower() for x in skip)]
            print("Groq live models:", _groq_cache)
        except Exception as e:
            print("Groq model list failed:", e)
            _groq_cache = []
    pref = [prefer.split("/")[-1].split("-")[0].lower(), "llama", "qwen", "gpt-oss", "kimi", "deepseek"]
    return sorted(_groq_cache, key=lambda i: next((n for n, p in enumerate(pref) if p in i.lower()), 99))


_free_models_cache = None


def _openrouter_free_models():
    """Free model names change often, so pick them live from OpenRouter's list."""
    global _free_models_cache
    if _free_models_cache is None:
        try:
            data = requests.get("https://openrouter.ai/api/v1/models", timeout=30).json()["data"]
            free = [m["id"] for m in data if m["id"].endswith(":free")]
            pref = ["deepseek", "qwen", "llama", "gemma", "mistral"]
            free.sort(key=lambda i: next((n for n, p in enumerate(pref) if p in i), 99))
            _free_models_cache = free[:4]
        except Exception:
            _free_models_cache = ["deepseek/deepseek-chat-v3-0324:free"]
    return _free_models_cache


def ask(member, system, user, retries=2):
    """Returns (parsed_json_or_None, raw_text_or_error)."""
    p = member["provider"]
    last_err = ""
    models = [member["model"]]
    if p == "openrouter" and member["model"] == "auto-free":
        models = _openrouter_free_models()
    elif p == "groq":
        live = _groq_live_models(member["model"])
        models = ([member["model"]] if member["model"] in live or not live else []) + \
                 [m for m in live if m != member["model"]][:2]
    for model in models:
        for attempt in range(retries):
            try:
                if p == "gemini":
                    txt = _gemini(model, system, user)
                elif p == "groq":
                    txt = _openai_style("https://api.groq.com/openai/v1/chat/completions",
                                        os.environ["GROQ_API_KEY"], model, system, user)
                else:
                    txt = _openai_style("https://openrouter.ai/api/v1/chat/completions",
                                        os.environ["OPENROUTER_API_KEY"], model, system, user,
                                        {"X-Title": "AI Trading Agent"})
                return parse_json(txt), f"[{model}] {txt}"
            except Exception as e:
                last_err = f"[{model}] {type(e).__name__}: {str(e)[:200]}"
                time.sleep(3 * (attempt + 1))
    return None, last_err


def ask_text(system, user):
    """Free-text answer from the first brain that works (used for reflections/reviews)."""
    for m in available_members():
        js, raw = ask(m, system + "\nReply as JSON: {\"text\": \"...\"}", user)
        if js and js.get("text"):
            return js["text"], m["name"]
    return None, None


def parse_json(txt):
    txt = re.sub(r"<think>.*?</think>", "", txt or "", flags=re.S)
    txt = re.sub(r"```(?:json)?", "", txt)
    start, depth = txt.find("{"), 0
    if start < 0:
        return None
    for i in range(start, len(txt)):
        if txt[i] == "{":
            depth += 1
        elif txt[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(txt[start:i + 1])
                except json.JSONDecodeError:
                    return None
    return None
