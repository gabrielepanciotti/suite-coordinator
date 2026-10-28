#!/usr/bin/env python3
"""Allarme se il bot Telegram non consuma gli aggiornamenti (pending fermi >20 min). Cron ogni 5 min sul minipc."""
import json, os, time, urllib.request
ENV = os.path.expanduser("~/personal/Coordinator/.env")
STATE = os.path.expanduser("~/personal/Coordinator/data/bot_alive.json")
API = "http://127.0.0.1:8081"
env = {}
for line in open(ENV, encoding="utf-8"):
    if "=" in line and not line.startswith("#"):
        k, v = line.rstrip("\n").split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
tok = env.get("TELEGRAM_BOT_TOKEN") or env.get("BOT_TOKEN")
owner = (env.get("PROFILE_OWNER_IDS") or "").split(",")[0].strip()

def call(method, api=API, **kw):
    data = json.dumps(kw).encode()
    req = urllib.request.Request(f"{api}/bot{tok}/{method}", data, {"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=20))["result"]

try:
    st = json.load(open(STATE))
except Exception:
    st = {}
now = time.time()
try:
    pend = call("getWebhookInfo").get("pending_update_count", 0)
except Exception as e:
    pend = -1
if pend == 0:
    st = {"since": None, "alerted": False}
elif pend < 0:
    # server locale giu': avviso dal server cloud (l'unico raggiungibile in quel caso)
    st.setdefault("since", now)
    if st["since"] is None:
        st["since"] = now
    if now - st["since"] > 10 * 60 and not st.get("alerted") and owner:
        try:
            call("sendMessage", api="https://api.telegram.org", chat_id=int(owner),
                 text="⚠️ Il server Telegram del minipc non risponde da oltre 10 minuti: il bot è fermo. Controlla i container telegram-bot-api e coordinator-bot.")
            st["alerted"] = True
        except Exception:
            pass
else:
    st.setdefault("since", now)
    if st["since"] is None:
        st["since"] = now
    if now - st["since"] > 20 * 60 and not st.get("alerted") and owner and pend > 0:
        call("sendMessage", chat_id=int(owner),
             text=f"⚠️ Il bot non sta leggendo Telegram: {pend} messaggi in attesa da oltre 20 minuti. Non si perdono (Telegram li tiene 24 ore), ma vanno ripresi: controlla il container coordinator-bot.")
        st["alerted"] = True
json.dump(st, open(STATE, "w"))
