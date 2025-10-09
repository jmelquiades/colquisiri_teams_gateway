# -----------------------------------------------------------------------------
# Gateway para Microsoft Bot Framework (Web Chat/Teams)
# - Recibe /api/messages (Adapter SDK 4.14.3 valida token entrante)
# - Llama al backend /n2sql/run con intent + filtros detectados
# - Responde con ConnectorClient.send_to_conversation (SDK 4.14.3: NO await)
# - Memoria corta por conversación para refinamientos ("y de todo el mes?")
# - Diags: /health, /diag/env, /diag/msal, /diag/sdk-token
# -----------------------------------------------------------------------------

import os
import json
import re
from typing import List, Dict, Any

import httpx
import msal
from fastapi import FastAPI, Request, Response

from botbuilder.core import BotFrameworkAdapterSettings, BotFrameworkAdapter, TurnContext
from botbuilder.schema import Activity, ActivityTypes, ChannelAccount
from botframework.connector import ConnectorClient
from botframework.connector.auth import MicrosoftAppCredentials

# Forzar nube pública (evita endpoints Gov/DoD)
os.environ["ChannelService"] = "Public"
os.environ["CHANNEL_SERVICE"] = "Public"

# Configuración (ENV)
APP_ID  = os.getenv("MICROSOFT_APP_ID") or os.getenv("MicrosoftAppId")
APP_PWD = os.getenv("MICROSOFT_APP_PASSWORD") or os.getenv("MicrosoftAppPassword")
APP_TYPE = os.getenv("MicrosoftAppType", "MultiTenant")  # "SingleTenant" | "MultiTenant"
TENANT   = os.getenv("MicrosoftAppTenantId")

BACKEND_URL    = os.getenv("BACKEND_URL", "https://admin-assistant-npsd.onrender.com")
REPLY_MODE     = os.getenv("REPLY_MODE", "active")   # "active" | "silent"
DEFAULT_LOCALE = os.getenv("DEFAULT_LOCALE", "es-PE")
PAGE_LIMIT     = int(os.getenv("PAGE_LIMIT", "10"))

# Estado de conversación simple (en memoria del proceso)
CONV_STATE: dict[str, dict] = {}  # { conversation_id: {"last_intent": str, "last_filters": dict} }

# FastAPI + Adapter
app = FastAPI(title="Teams Gateway")

if not APP_ID or not APP_PWD:
    print("[gw] WARNING: faltan MICROSOFT_APP_ID/MicrosoftAppId o MICROSOFT_APP_PASSWORD/MicrosoftAppPassword.")

adapter_settings = BotFrameworkAdapterSettings(APP_ID, APP_PWD)
adapter = BotFrameworkAdapter(adapter_settings)

# Util: tabla markdown
def _markdown_table(cols: List[str], rows: List[Dict[str, Any]], limit: int = PAGE_LIMIT) -> str:
    if not rows:
        return "_(sin resultados)_"
    header = "| " + " | ".join(cols) + " |"
    sep    = "| " + " | ".join(["---"] * len(cols)) + " |"
    body   = "\n".join("| " + " | ".join(str(r.get(c, "")) for c in cols) + " |" for r in rows[:limit])
    return f"{header}\n{sep}\n{body}\n\n_Mostrando hasta {limit} filas._"

# NLU ligero: detección de intent
def detect_intent(text: str, last_intent: str | None = None) -> str:
    t = (text or "").lower().strip()

    # ayuda / small talk
    if t in ("ayuda", "help", "?") or any(x in t for x in ["hola", "buenos días", "buenas", "que día es", "qué hora"]):
        return "help"

    # refinamiento contextual "todo el mes"
    if "todo el mes" in t or ("mes" in t and ("todo" in t or "entero" in t)):
        if last_intent in ("invoices_due_this_month", "overdue_this_month", "invoices_due_next_days"):
            return last_intent or "invoices_due_this_month"

    # vencidas del mes
    if ("vencid" in t or "atrasad" in t) and ("mes" in t or "este mes" in t):
        return "overdue_this_month"

    # próximas X días / semanas
    if ("próxim" in t or "siguient" in t) and ("día" in t or "dias" in t or "semana" in t or "semanas" in t):
        return "invoices_due_next_days"

    # por vencer este mes / pendientes del mes
    if (("vencen" in t or "vencimiento" in t or "por vencer" in t or "pendiente" in t or "pendientes" in t)
        and ("mes" in t or "mes actual" in t or "este mes" in t)):
        return "invoices_due_this_month"

    # vencidas hoy
    if ("vencid" in t and "hoy" in t):
        return "overdue_today"

    return "help"

# NLU ligero: extracción de filtros (día del mes, rango próximas semanas/días)
SPAN_NUMS = {"uno":1,"una":1,"dos":2,"tres":3,"cuatro":4,"cinco":5,"seis":6,"siete":7,"ocho":8,"nueve":9,"diez":10,
             "once":11,"doce":12,"trece":13,"catorce":14,"quince":15}

def _word_to_int(tok: str) -> int | None:
    tok = tok.lower()
    if tok.isdigit(): return int(tok)
    return SPAN_NUMS.get(tok)

def extract_filters(text: str, intent: str) -> dict:
    t = (text or "").lower()
    filters: dict = {}

    # día específico (ej: "el 13", "13 de este mes", "día 7")
    m = re.search(r"(?:día\s+)?(\d{1,2})\b(?:\s*de\s+este\s+mes)?", t)
    if m:
        day = int(m.group(1))
        if 1 <= day <= 31:
            filters["date_day"] = day

    # próximas X semanas/días (ej: "próximas 2 semanas", "siguientes diez días")
    if "próxim" in t or "siguient" in t:
        m = re.search(r"(próxim[oa]s?|siguient[ea]s?)\s+(\d+|[a-zá]+)\s+(semana|semanas|día|días)", t)
        if m:
            num = _word_to_int(m.group(2)) or 1
            unit = m.group(3)
            days = num * 7 if "semana" in unit else num
            filters["range_days"] = days

    # "todo el mes" => limpia filtros
    if "todo el mes" in t or ("mes" in t and ("todo" in t or "entero" in t)):
        filters.pop("date_day", None)
        filters.pop("range_days", None)

    # default para próximas N días
    if intent == "invoices_due_next_days" and "range_days" not in filters:
        filters["range_days"] = 14  # por defecto 2 semanas

    return filters

def _help_text() -> str:
    return (
        "Hola, soy Lucero. Hoy te ayudo con facturación.\n\n"
        "**Puedo ayudarte con:**\n"
        "• `facturas que vencen este mes`\n"
        "• `facturas vencidas este mes`\n"
        "• `facturas vencidas hoy`\n"
        "• `facturas que vencen en las próximas 2 semanas`\n"
        "_También puedo entender: `las del 13`, `y de todo el mes?`, `próximas dos semanas`._"
    )

# Respuesta al canal con ConnectorClient (SDK 4.14.3)
async def _reply_markdown(context: TurnContext, title: str, cols: List[str], rows: List[Dict[str, Any]], limit: int = PAGE_LIMIT):
    act_in = context.activity
    su           = getattr(act_in, "service_url", None)
    channel_id   = getattr(act_in, "channel_id", None)
    conv_in      = getattr(act_in, "conversation", None)
    conv_id      = conv_in.id if conv_in else None
    user_acc     = getattr(act_in, "from_property", None)
    bot_acc      = getattr(act_in, "recipient", None)
    locale       = getattr(act_in, "locale", None) or DEFAULT_LOCALE

    # Confiar serviceUrl
    if su:
        try:
            MicrosoftAppCredentials.trust_service_url(su)
        except Exception as e:
            print(f"[gw] WARN trust_service_url: {e}")

    md = f"{title}\n\n{_markdown_table(cols, rows, limit)}"

    if REPLY_MODE.lower() == "silent":
        print("[gw] SILENT MODE — markdown (no enviado):")
        print(md[:2000])
        return

    if not (su and channel_id and conv_id and user_acc and bot_acc):
        try:
            await context.send_activity("⚠️ No se encontró información suficiente del canal para responder.")
        except Exception as e:
            print(f"[gw] ERROR send_activity (fallback): {repr(e)}")
        return

    bot_id   = getattr(bot_acc, "id", None) or APP_ID
    bot_name = getattr(bot_acc, "name", None) or "Bot"
    usr_id   = getattr(user_acc, "id", None) or "user"
    usr_name = getattr(user_acc, "name", None) or "Usuario"

    creds = MicrosoftAppCredentials(
        app_id=APP_ID,
        password=APP_PWD,
        channel_auth_tenant=TENANT if (APP_TYPE == "SingleTenant" and TENANT) else None,
        oauth_scope="https://api.botframework.com/.default",
    )
    connector = ConnectorClient(credentials=creds, base_url=su)

    reply_activity = {
        "type": "message",
        "channelId": channel_id,
        "serviceUrl": su,
        "conversation": {"id": conv_id},
        "from": {"id": bot_id, "name": bot_name, "role": "bot"},
        "recipient": {"id": usr_id, "name": usr_name, "role": "user"},
        "textFormat": "markdown",
        "text": md,
        "locale": locale,
    }

    try:
        connector.conversations.send_to_conversation(conversation_id=conv_id, activity=reply_activity)
    except Exception as e:
        print(f"[gw] ERROR ConnectorClient.send_to_conversation: {repr(e)}")
        try:
            await context.send_activity("⚠️ No pude enviar la respuesta por el canal. Intenta de nuevo.")
        except Exception as e2:
            print(f"[gw] ERROR send_activity (fallback tras ConnectorClient): {repr(e2)}")

# Handler principal
async def on_message(context: TurnContext):
    text = (context.activity.text or "").strip()
    conv_id = getattr(getattr(context.activity, "conversation", None), "id", "default")
    state = CONV_STATE.get(conv_id, {})

    user: ChannelAccount = context.activity.from_property or ChannelAccount(id="u1", name="Usuario")

    intent = detect_intent(text, state.get("last_intent"))
    if intent == "help":
        await context.send_activity(_help_text())
        return

    filters = extract_filters(text, intent)

    payload = {
        "user": {"id": user.id or "u1", "name": user.name or "Usuario"},
        "intent": intent,
        "utterance": text,
        "filters": filters,
    }

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(f"{BACKEND_URL}/n2sql/run", json=payload)
            if r.status_code >= 400:
                await context.send_activity(f"⚠️ Backend: {r.text}")
                return
            data = r.json()
    except Exception as e:
        print(f"[gw] ERROR llamando backend: {repr(e)}")
        await context.send_activity("⚠️ Ocurrió un problema al consultar el backend.")
        return

    cols: List[str] = data.get("columns", []) or []
    rows: List[Dict[str, Any]] = data.get("rows", []) or []
    summary: str = data.get("summary", "") or f"{len(rows)} filas."
    title = f"**{intent}** — {summary}"

    CONV_STATE[conv_id] = {"last_intent": intent, "last_filters": filters}

    await _reply_markdown(context, title, cols, rows, limit=PAGE_LIMIT)

# Rutas HTTP (health/diag + BF)
@app.get("/")
def root():
    return {"ok": True, "service": "teams-gateway"}

@app.get("/health")
def health():
    return {"ok": True}

@app.get("/diag/env")
def diag_env():
    aid = APP_ID or ""
    apw = APP_PWD or ""
    return {
        "has_app_id": bool(aid),
        "app_id_len": len(aid),
        "has_secret": bool(apw),
        "secret_len": len(apw),
        "app_type": APP_TYPE,
        "tenant_set": bool(TENANT),
    }

@app.get("/diag/msal")
def diag_msal():
    if not APP_ID or not APP_PWD:
        return {"ok": False, "error": "Faltan AppId/Secret"}
    authority = (
        f"https://login.microsoftonline.com/{TENANT}"
        if (APP_TYPE == "SingleTenant" and TENANT)
        else "https://login.microsoftonline.com/organizations"
    )
    cca = msal.ConfidentialClientApplication(client_id=APP_ID, client_credential=APP_PWD, authority=authority)
    res = cca.acquire_token_for_client(scopes=["https://api.botframework.com/.default"])
    if "access_token" in res:
        return {"ok": True, "token_type": res.get("token_type", "Bearer"), "expires_in": res.get("expires_in")}
    return {"ok": False, "aad_error": res.get("error"), "aad_error_description": res.get("error_description")}

@app.get("/diag/sdk-token")
def diag_sdk_token():
    try:
        creds = MicrosoftAppCredentials(
            app_id=APP_ID,
            password=APP_PWD,
            channel_auth_tenant=TENANT if (APP_TYPE == "SingleTenant" and TENANT) else None,
            oauth_scope="https://api.botframework.com/.default",
        )
        tok = creds.get_access_token()
        ok = bool(tok)
        prefix = tok[:12] if tok else ""
        return {"ok": ok, "prefix": prefix}
    except Exception as e:
        return {"ok": False, "error": repr(e)}

@app.options("/api/messages")
def options_messages():
    return Response(status_code=200)

@app.post("/api/messages")
async def api_messages(req: Request):
    body = await req.json()
    activity = Activity().deserialize(body)

    auth_header = req.headers.get("authorization") or req.headers.get("Authorization") or ""
    client = req.client.host if req.client else "unknown"
    print(f"[gw] has_auth={bool(auth_header)} from={client}")

    async def aux(turn_context: TurnContext):
        if activity.type == ActivityTypes.message:
            await on_message(turn_context)
        else:
            pass

    await adapter.process_activity(activity, auth_header, aux)
    return Response(status_code=200, content=json.dumps({"ok": True}), media_type="application/json")
