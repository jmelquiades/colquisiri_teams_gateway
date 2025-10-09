import os, json, httpx
from typing import Dict, Any
from fastapi import FastAPI, Request, Response
from botbuilder.core import BotFrameworkAdapterSettings, BotFrameworkAdapter, TurnContext
from botbuilder.schema import Activity, ActivityTypes, ChannelAccount

BACKEND_URL = os.getenv("BACKEND_URL", "https://admin-assistant-npsd.onrender.com")
MICROSOFT_APP_ID = os.getenv("MICROSOFT_APP_ID")
MICROSOFT_APP_PASSWORD = os.getenv("MICROSOFT_APP_PASSWORD")

app = FastAPI(title="Teams Gateway")

adapter = BotFrameworkAdapter(BotFrameworkAdapterSettings(MICROSOFT_APP_ID, MICROSOFT_APP_PASSWORD))

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

    # Routing simple de demo (puedes sustituirlo por tu intents.router real)
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

async def _process(req: Request) -> Response:
    body = await req.json()
    activity = Activity().deserialize(body)

    async def aux(turn: TurnContext):
        if activity.type == ActivityTypes.message:
            await on_message(turn)
        else:
            # Ack para otros tipos de Activity
            pass

    await adapter.process_activity(activity, "", aux)
    return Response(status_code=200, content=json.dumps({"ok": True}), media_type="application/json")

@app.post("/api/messages")
async def api_messages(req: Request):
    return await _process(req)

@app.get("/health")
def health():
    return {"ok": True}
