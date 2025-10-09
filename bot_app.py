# bot_app.py
import os, json
import httpx
from typing import Dict, Any
from fastapi import FastAPI, Request, Response
from botbuilder.core import BotFrameworkAdapterSettings, BotFrameworkAdapter, TurnContext
from botbuilder.schema import Activity, ActivityTypes, ChannelAccount

BACKEND_URL = os.getenv("BACKEND_URL", "https://admin-assistant-npsd.onrender.com")
MICROSOFT_APP_ID = os.getenv("MICROSOFT_APP_ID")
MICROSOFT_APP_PASSWORD = os.getenv("MICROSOFT_APP_PASSWORD")

app = FastAPI(title="Teams Gateway")

# sanity checks útiles en logs
if not MICROSOFT_APP_ID or not MICROSOFT_APP_PASSWORD:
    print("[gw] WARNING: faltan MICROSOFT_APP_ID o MICROSOFT_APP_PASSWORD. Web Chat/Teams fallará con 401.")

adapter = BotFrameworkAdapter(
    BotFrameworkAdapterSettings(MICROSOFT_APP_ID, MICROSOFT_APP_PASSWORD)
)

async def _reply_md(context: TurnContext, title: str, cols: list[str], rows: list[dict], limit: int = 10):
    if not rows:
        await context.send_activity(f"{title}\n\n_(sin resultados)_")
        return
    header = "| " + " | ".join(cols) + " |"
    sep = "| " + " | ".join(["---"] * len(cols)) + " |"
    body = "\n".join("| " + " | ".join(str(r.get(c, "")) for c in cols) + " |" for r in rows[:limit])
    await context.send_activity(f"{title}\n\n{header}\n{sep}\n{body}\n\n_Mostrando hasta {limit} filas._")

async def on_message(context: TurnContext):
    text = (context.activity.text or "").strip()
    user = context.activity.from_property or ChannelAccount(id="u1", name="Usuario")

    # routing simple (demo)
    intent = "invoices_due_this_month" if ("vencen" in text.lower() and "mes" in text.lower()) else "top_clients_overdue"

    payload = {"user": {"id": user.id, "name": user.name}, "intent": intent, "utterance": text}

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(f"{BACKEND_URL}/n2sql/run", json=payload)
        if r.status_code >= 400:
            await context.send_activity(f"⚠️ Backend: {r.text}")
            return
        data = r.json()

    cols, rows = data.get("columns", []), data.get("rows", [])
    title = f"**{intent}** — {data.get('summary','')}"
    await _reply_md(context, title, cols, rows)

@app.post("/api/messages")
async def api_messages(req: Request):
    body = await req.json()
    activity = Activity().deserialize(body)

    # ✅ tomar Authorization real (case-insensitive)
    auth_header = req.headers.get("authorization") or req.headers.get("Authorization") or ""

    # log mínimo, sin exponer el token
    print(f"[gw] has_auth={bool(auth_header)} prefix={auth_header[:10] if auth_header else ''}")

    async def aux(turn: TurnContext):
        if activity.type == ActivityTypes.message:
            await on_message(turn)
        else:
            # ack otros tipos (conversationUpdate, etc.)
            pass

    # ✅ pasar el header al adapter (¡no ""!)
    await adapter.process_activity(activity, auth_header, aux)
    return Response(status_code=200, content=json.dumps({"ok": True}), media_type="application/json")

@app.get("/health")
def health():
    return {"ok": True}
