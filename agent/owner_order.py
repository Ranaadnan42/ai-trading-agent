"""Owner orders from WhatsApp (AI Manager) -> the trading agent changes its own code/settings.

Adnan tells his WhatsApp Manager e.g. "trading mein risk 0.5% kar do" or "Friday ko trade na karo".
The manager queues it; .github/workflows/orders.yml picks it up and runs this file:
an AI reads the agent's code, makes the smallest change that does EXACTLY the order, the code is
checked (compile + import), committed, and the result goes back to WhatsApp.
Bad change -> nothing is pushed, Adnan is told why.
"""
import glob
import json
import os
import re
import subprocess
import sys

import requests

from . import notify

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ALLOWED = sorted([os.path.relpath(p, ROOT) for p in glob.glob(os.path.join(ROOT, "agent", "*.py"))]
                 + ["data/lessons.md"])
NEVER = {"agent/owner_order.py", "agent/notify.py", "agent/setup_metaapi.py"}

SYSTEM = """You are the developer of Adnan's AI Trading Agent (Python, runs on GitHub Actions, trades Gold XAUUSD
on his Exness MT5 DEMO account; 3 free AIs vote; risk rules in agent/config.py; lessons in data/lessons.md).
Adnan (the owner) sent an ORDER on WhatsApp. Do EXACTLY what he asked - completely, nothing extra, change nothing else.
- If it is only a question, answer it from the code/data and make no edits.
- Make the smallest correct edits. Keep Python valid. Never touch passwords, tokens, logins, secrets or workflow files.
- Settings/rules -> agent/config.py. Strategy/trading rules in plain words -> data/lessons.md or agent/brain.py.
- If the order cannot be done in code (needs login, money, new account, new secret), say so in needs_owner.
Reply ONLY JSON:
{"answer": "Roman Urdu, 1-3 short lines: what you changed (old -> new) or the answer",
 "edits": [{"path": "agent/config.py", "old": "exact text copied from the file (unique)", "new": "replacement"}],
 "needs_owner": []}"""


def sh(cmd):
    p = subprocess.run(cmd, shell=True, cwd=ROOT, capture_output=True, text=True)
    return (p.stdout + p.stderr).strip(), p.returncode


def code_context():
    parts = []
    for f in ALLOWED:
        try:
            parts.append(f"===== {f} =====\n" + open(os.path.join(ROOT, f), encoding="utf-8").read())
        except OSError:
            pass
    return "\n\n".join(parts)


def brain(user):
    last = ""
    key = os.getenv("GEMINI_API_KEY")
    for model in ("gemini-3.5-flash", "gemini-flash-latest", "gemini-3.1-flash-lite"):
        if not key:
            break
        try:
            r = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                              params={"key": key}, timeout=180, json={
                                  "systemInstruction": {"parts": [{"text": SYSTEM}]},
                                  "contents": [{"role": "user", "parts": [{"text": user}]}],
                                  "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json"}})
            txt = r.json()["candidates"][0]["content"]["parts"][0]["text"]
            js = parse(txt)
            if js:
                return js
        except Exception as e:
            last = f"{model}: {e}"
    key = os.getenv("GROQ_API_KEY")
    for model in ("openai/gpt-oss-120b", "moonshotai/kimi-k2-instruct", "llama-3.3-70b-versatile"):
        if not key:
            break
        try:
            r = requests.post("https://api.groq.com/openai/v1/chat/completions", timeout=180,
                              headers={"Authorization": f"Bearer {key}"},
                              json={"model": model, "temperature": 0.1, "max_tokens": 6000,
                                    "messages": [{"role": "system", "content": SYSTEM},
                                                 {"role": "user", "content": user}]})
            js = parse(r.json()["choices"][0]["message"]["content"])
            if js:
                return js
        except Exception as e:
            last = f"{model}: {e}"
    raise RuntimeError(last or "koi AI nahi chala")


def parse(txt):
    txt = re.sub(r"<think>.*?</think>|```(?:json)?", "", txt or "", flags=re.S)
    s, e = txt.find("{"), txt.rfind("}")
    try:
        return json.loads(txt[s:e + 1]) if s >= 0 else None
    except json.JSONDecodeError:
        return None


def apply(edits):
    done = []
    for ed in edits or []:
        path, old, new = ed.get("path", ""), ed.get("old", ""), ed.get("new", "")
        if path not in ALLOWED or path in NEVER or not old:
            raise ValueError(f"{path}: is file ko badalna mana hai")
        fp = os.path.join(ROOT, path)
        src = open(fp, encoding="utf-8").read()
        if src.count(old) != 1:
            raise ValueError(f"{path}: badalne wali jagah sahi nahi mili")
        open(fp, "w", encoding="utf-8").write(src.replace(old, new, 1))
        done.append(path)
    return done


def check():
    out, rc = sh(f"{sys.executable} -m py_compile agent/*.py && "
                 f"MODE=paper {sys.executable} -c 'import agent.run, agent.review, agent.brain, agent.risk, agent.config'")
    return rc == 0, out[-400:]


def push(msg):
    sh('git config user.name "trading-agent" && git config user.email "actions@users.noreply.github.com"')
    sh(f"git add -A agent data/lessons.md && git commit -q -m {json.dumps(msg[:120])}")
    for _ in range(5):
        sh("git pull -q --rebase")
        out, rc = sh("git push -q")
        if rc == 0:
            return True
    return False


def handle(order):
    js = brain(f"OWNER ORDER: {order}\n\nCODE AND DATA:\n{code_context()}")
    answer = str(js.get("answer") or "").strip()
    needs = js.get("needs_owner") or []
    edits = js.get("edits") or []
    if edits:
        try:
            files = apply(edits)
            ok, err = check()
            if not ok:
                raise ValueError(f"naya code chal nahi raha ({err})")
            if not push(f"owner order: {order}"):
                raise ValueError("GitHub par save na ho saka")
            answer += f"\n✅ Badal diya ({', '.join(sorted(set(files)))}). Agle cycle se laagu."
        except Exception as e:
            sh("git checkout -- agent data/lessons.md")
            answer = f"❌ Yeh tabdeeli nahi ho saki: {e}. Kuch nahi badla."
    for n in needs[:3]:
        answer += f"\n⚠️ aap ki zaroorat: {n}"
    notify.send(f"Aap ka hukam: \"{order[:120]}\"\n{answer or 'kar diya'}")


def main():
    cmds = json.loads(os.getenv("ORDERS") or "[]")
    for c in cmds:
        order = str(c.get("order") or "").strip()
        if not order:
            continue
        try:
            handle(order)
        except Exception as e:
            notify.send(f"Aap ka hukam: \"{order[:120]}\"\n❌ abhi nahi ho saka ({str(e)[:150]}). Thori der baad dobara kahein.")


if __name__ == "__main__":
    main()
