# bot_app.py
# -----------------------------------------------------------------------------
# Gateway para Microsoft Bot Framework (Web Chat/Teams)
# - Recibe /api/messages (Adapter SDK 4.14.3 valida el token entrante)
# - Llama al backend /n2sql/run
# - Responde con ConnectorClient.send_to_conversation (sin await)
# - Activity JSON con 'from' y 'recipient' explícitos
# - Diags: /health, /diag/env, /diag/msal, /diag/sdk-token
# -----------------------------------------------------------------------------

import os
import json
from typing import List, Dict, Any

import httpx
import msal
from fastapi import FastAPI, Request, Response

from botbuilder.core import BotFrameworkAdapterSettings, BotFrameworkAdapter, TurnContext
from botbuilder.schema import Activity, ActivityTypes, ChannelAccount
from botframework.connector import ConnectorClient
from botframework.connector.auth import MicrosoftAppCredentials

# -----------------------------------------------------------------------------
# 0) Forzar nube pública (evita endpoints Gov/DoD)
# -----------------------------------------------------------------------------
os.environ["ChannelService"] = "Public"
os.environ["CHANNEL_SERVICE"] = "Public"

# -----------------------------------------------------------------------------
# 1) Configuración (ENV)
# -----------------------------------------------------------------------------
APP_ID  = os.getenv("MICROSOFT_APP_ID") or os.getenv("MicrosoftAppId")
APP_PWD = os.getenv("MICROSOFT_APP_PASSWORD") or os.getenv("MicrosoftAppPassword")

APP_TYPE = os.getenv("MicrosoftAppType", "MultiTenant")  # "SingleTenant" | "MultiTenant"
TENANT   = os.getenv("MicrosoftAppTenantId")

BACKEND_URL   = os.getenv("BACKEND_URL", "https://admin-assistant-npsd.onrender.com")
REPLY_MODE    = os.getenv("REPLY_MODE", "active")  # "active" | "silent"
DEFAULT_LOCALE = os.getenv("DEFAULT_LOCALE", "es-PE")
PAGE_LIMIT     = int(os.getenv("PAGE_LIMIT", "10"))

# -----------------------------------------------------------------------------
# 2) FastAPI + Adapter (SDK 4.14.3)
# -----------------------------------------------------------------------------
app = FastAPI(title="Teams Gateway")

if not APP_ID or not APP_PWD:
    print("[gw] WARNING: faltan MICROSOFT_APP_ID/MicrosoftAppId o MICROSOFT_APP_PASSWORD/MicrosoftAppPassword.")

adapter_settings = BotFrameworkAdapterSettings(APP_ID, APP_PWD)
adapter = BotFrameworkAdapter(adapter_settings)

# -----------------------------------------------------------------------------
# 3) Util: tabla markdown
# -----------------------------------------------------------------------------
def _markdown_table(cols: List[str], rows: List[Dict[str, Any]], limit: int = PAGE_LIMIT) -> str:
    if not rows:
        return "_(sin resultados)_"
    header = "| " + " | ".join(cols) + " |"
    sep    = "| " + " | ".join(["---"] * len(cols)) + " |"
    body   = "\n".join("| " + " | ".join(str(r.get(c, "")) for c in cols) + " |" for r in rows[:limit])
    return f"{header}\n{sep}\n{body}\n\n_Mostrando hasta {limit} filas._"

# -----------------------------------------------------------------------------
# 4) Detección de intent (simple; reemplázalo por tu NLU cuando gustes)
# -----------------------------------------------------------------------------
def detect_intent(text: str) -> str:
    t = (text or "").lower()
    if t.strip() in ("ayuda", "help", "?"):
        return "help"
    if ("vencid" in t and "hoy" in t):
        return "overdue_today"
    if (("top" in t and ("cliente" in t or "client" in t)) or ("saldo" in t and "vencido" in t)):
        return "top_clients_overdue"
    if (("vencen" in t or "vencimiento" in t or "por vencer" in t or "pendiente" in t or "pendientes" in t)
        and ("mes" in t or "mes actual" in t or "este mes" in t)):
        return "invoices_due_this_month"
    # fallback
    return "help"

def _help_text() -> str:
    return (
        "**Puedo ayudarte con:**\n"
        "• `facturas que vencen este mes`\n"
        "• `facturas vencidas hoy`\n"
        "• `top clientes por saldo vencido`\n"
        "\n_Escribe un comando o una frase similar._"
    )

# -----------------------------------------------------------------------------
# 5) Respuesta al canal con ConnectorClient (¡sin reply_to_id!)
# -----------------------------------------------------------------------------
async def _reply_markdown(context: TurnContext, title: str, cols: List[str], rows: List[Dict[str, Any]], limit: int = PAGE_LIMIT):
    """
    Envía Markdown con ConnectorClient (SDK 4.14.3):
    - Usamos send_to_conversation (llamada SINCRÓNICA).
    - Activity JSON con claves exactas: 'from', 'recipient', 'conversation', 'channelId', 'serviceUrl'.
    - Credenciales con scope .default y tenant si aplica.
    """
    act_in = context.activity
    su           = getattr(act_in, "service_url", None)
    channel_id   = getattr(act_in, "channel_id", None)
    conv_in      = getattr(act_in, "conversation", None)
    conv_id      = conv_in.id if conv_in else None
    user_acc     = getattr(act_in, "from_property", None)  # usuario que escribió
    bot_acc      = getattr(act_in, "recipient", None)      # el bot (este gateway)
    locale       = getattr(act_in, "locale", None) or DEFAULT_LOCALE

    # Confiar serviceUrl (recomendado por Microsoft)
    if su:
        try:
            MicrosoftAppCredentials.trust_service_url(su)
        except Exception as e:
            print(f"[gw] WARN trust_service_url: {e}")

    md = f"{title}\n\n{_markdown_table(cols, rows, limit)}"

    if REPLY_MODE.lower() == "silent":
        print("[gw] SILENT MODE — markdown construido (no enviado):")
        print(md[:2000])
        return

    # Validaciones mínimas para responder
    if not (su and channel_id and conv_id and user_acc and bot_acc):
        try:
            await context.send_activity("⚠️ No se encontró información suficiente del canal para responder.")
        except Exception as e:
            print(f"[gw] ERROR send_activity (fallback): {repr(e)}")
        return

    # Identidades del Activity
    bot_id   = getattr(bot_acc, "id", None) or APP_ID
    bot_name = getattr(bot_acc, "name", None) or "Bot"
    usr_id   = getattr(user_acc, "id", None) or "user"
    usr_name = getattr(user_acc, "name", None) or "Usuario"

    # Credenciales salientes (scope .default + tenant si aplica)
    creds = MicrosoftAppCredentials(
        app_id=APP_ID,
        password=APP_PWD,
        channel_auth_tenant=TENANT if (APP_TYPE == "SingleTenant" and TENANT) else None,
        oauth_scope="https://api.botframework.com/.default",
    )
    connector = ConnectorClient(credentials=creds, base_url=su)

    # Activity JSON — OJO: clave 'from' literal
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
        # Llamada SINCRÓNICA en 4.14.3 (no usar await)
        connector.conversations.send_to_conversation(conversation_id=conv_id, activity=reply_activity)
    except Exception as e:
        print(f"[gw] ERROR ConnectorClient.send_to_conversation: {repr(e)}")
        # Último intento con send_activity (puede fallar por credenciales salientes)
        try:
            await context.send_activity("⚠️ No pude enviar la respuesta por el canal. Intenta de nuevo.")
        except Exception as e2:
            print(f"[gw] ERROR send_activity (fallback tras ConnectorClient): {repr(e2)}")

# -----------------------------------------------------------------------------
# 6) Handler principal de mensajes
# -----------------------------------------------------------------------------
async def on_message(context: TurnContext):
    text = (context.activity.text or "").strip()
    user: ChannelAccount = context.activity.from_property or ChannelAccount(id="u1", name="Usuario")

    intent = detect_intent(text)
    if intent == "help":
        await context.send_activity(_help_text())
        return

    payload = {
        "user": {"id": user.id or "u1", "name": user.name or "Usuario"},
        "intent": intent,
        "utterance": text,
    }

    # Llamada al backend N2SQL
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

    await _reply_markdown(context, title, cols, rows, limit=PAGE_LIMIT)

# -----------------------------------------------------------------------------
# 7) Rutas HTTP (health/diag + BF)
# -----------------------------------------------------------------------------
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
    # Útil para CORS/preflight de pruebas locales
    return Response(status_code=200)

@app.post("/api/messages")
async def api_messages(req: Request):
    body = await req.json()
    activity = Activity().deserialize(body)

    # token de Bot Service (autenticación entrante)
    auth_header = req.headers.get("authorization") or req.headers.get("Authorization") or ""
    client = req.client.host if req.client else "unknown"
    print(f"[gw] has_auth={bool(auth_header)} from={client}")

    async def aux(turn_context: TurnContext):
        if activity.type == ActivityTypes.message:
            await on_message(turn_context)
        else:
            # otros tipos (conversationUpdate, invoke, etc.) se pueden manejar si hace falta
            pass

    await adapter.process_activity(activity, auth_header, aux)
    return Response(status_code=200, content=json.dumps({"ok": True}), media_type="application/json")
