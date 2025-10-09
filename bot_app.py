# bot_app.py
# -----------------------------------------------------------------------------
# Gateway para Microsoft Bot Framework (Web Chat/Teams)
# - Recibe /api/messages (SDK 4.14.3 valida el token entrante).
# - Detecta intención (con memoria de conversación para seguimientos).
# - Extrae filtros simples (día del mes, próximas X semanas/días).
# - Llama al backend /n2sql/run con { user, intent, utterance, filters }.
# - Responde al canal con ConnectorClient.send_to_conversation (SIN await).
# - Diags: /health, /diag/env, /diag/msal, /diag/sdk-token, /diag/session
# -----------------------------------------------------------------------------

import os
import re
import json
from typing import List, Dict, Any, Optional

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

BACKEND_URL    = os.getenv("BACKEND_URL", "https://admin-assistant-npsd.onrender.com")
REPLY_MODE     = os.getenv("REPLY_MODE", "active")  # "active" | "silent"
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
# 3) Memoria de sesión simple (en RAM, por conversación)
#    - Guarda el último intent para permitir seguimientos ("y el 22?")
# -----------------------------------------------------------------------------
SESS: Dict[str, Dict[str, Any]] = {}  # { conv_id: {"last_intent": str | None} }

def _get_conv_id(ctx_or_activity) -> Optional[str]:
    conv = getattr(ctx_or_activity, "conversation", None)
    return getattr(conv, "id", None) if conv else None

def _get_session(conv_id: Optional[str]) -> Dict[str, Any]:
    if not conv_id:
        return {}
    if conv_id not in SESS:
        SESS[conv_id] = {"last_intent": None}
    return SESS[conv_id]

# -----------------------------------------------------------------------------
# 4) Util: tabla markdown
# -----------------------------------------------------------------------------
def _markdown_table(cols: List[str], rows: List[Dict[str, Any]], limit: int = PAGE_LIMIT) -> str:
    if not rows:
        return "_(sin resultados)_"
    header = "| " + " | ".join(cols) + " |"
    sep    = "| " + " | ".join(["---"] * len(cols)) + " |"
    body   = "\n".join("| " + " | ".join(str(r.get(c, "")) for c in cols) + " |" for r in rows[:limit])
    return f"{header}\n{sep}\n{body}\n\n_Mostrando hasta {limit} filas._"

# -----------------------------------------------------------------------------
# 5) Detección de intent + filtros
# -----------------------------------------------------------------------------
DAY_RE = re.compile(r"(?:\b(?:y\s+)?(?:el\s+)?)?(?:día\s+)?(\d{1,2})\b")

def extract_filters(text: str) -> Dict[str, Any]:
    """
    Extrae filtros sencillos desde el texto del usuario.
    - date_day: día del mes (1..31) si aparece "el 13", "día 22", "y el 7", etc.
    - range_next_days: si dice "próximas 2 semanas", "siguientes 10 días", etc.
    - whole_month: si dice "todo el mes".
    """
    t = (text or "").lower()
    filters: Dict[str, Any] = {}

    # día del mes
    m = DAY_RE.search(t)
    if m:
        try:
            day = int(m.group(1))
            if 1 <= day <= 31:
                filters["date_day"] = day
        except Exception:
            pass

    # "todo el mes"
    if "todo el mes" in t or (("mes" in t) and ("todo" in t or "entero" in t)):
        filters["whole_month"] = True

    # próximas X semanas/días
    if "próxim" in t or "siguient" in t:
        # intenta capturar número (p.ej. "2 semanas", "10 dias")
        m_num = re.search(r"(\d{1,2})\s+(?:semana|semanas|día|días|dias)", t)
        if m_num:
            n = int(m_num.group(1))
            if "semana" in t or "semanas" in t:
                filters["range_next_days"] = n * 7
            else:
                filters["range_next_days"] = n
        else:
            # por defecto, próximas 2 semanas
            filters["range_next_days"] = 14

    return filters

def _help_text() -> str:
    return (
        "Hola, soy Lucero. Hoy te ayudo con facturación.\n\n"
        "**Puedo ayudarte con:**\n"
        "• `facturas que vencen este mes`\n"
        "• `facturas vencidas este mes`\n"
        "• `facturas vencidas hoy`\n"
        "• `facturas que vencen en las próximas 2 semanas`\n"
        "También puedo entender: `las del 13`, `y de todo el mes?`, `próximas dos semanas`.\n"
    )

def detect_intent(text: str, last_intent: Optional[str] = None) -> str:
    """
    Detección de intención ligera + soporte de seguimiento:
    - Si ya hubo un intent y el nuevo texto solo agrega filtros (día, todo el mes, próximas semanas),
      reusa la última intención (o cambia a "invoices_due_next_days" si corresponde).
    """
    t = (text or "").lower().strip()

    # Seguimientos basados en último intent
    has_day = DAY_RE.search(t) is not None
    whole_month = "todo el mes" in t or (("mes" in t) and ("todo" in t or "entero" in t))
    looks_range = ("próxim" in t or "siguient" in t) and ("día" in t or "dias" in t or "semana" in t or "semanas" in t)

    if last_intent:
        if has_day or whole_month:
            return last_intent
        if looks_range:
            return "invoices_due_next_days"

    # Small talk / ayuda
    if t in ("ayuda", "help", "?") or any(x in t for x in ["hola", "buenos días", "buenas", "qué hora", "que hora", "qué día", "que día", "que dia", "qué dia"]):
        return "help"

    # “mes completo” explícito
    if whole_month:
        return last_intent or "invoices_due_this_month"

    # vencidas del mes
    if ("vencid" in t or "atrasad" in t) and ("mes" in t or "este mes" in t):
        return "overdue_this_month"

    # próximas X días / semanas
    if looks_range:
        return "invoices_due_next_days"

    # por vencer / pendientes del mes
    if (("vencen" in t or "vencimiento" in t or "por vencer" in t or "pendiente" in t or "pendientes" in t)
        and ("mes" in t or "mes actual" in t or "este mes" in t)):
        return "invoices_due_this_month"

    # vencidas hoy
    if ("vencid" in t and "hoy" in t):
        return "overdue_today"

    return "help"

# -----------------------------------------------------------------------------
# 6) Responder al canal con ConnectorClient (¡sin reply_to_id!)
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

    # Validaciones mínimas
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
# 7) Handler principal de mensajes
# -----------------------------------------------------------------------------
async def on_message(context: TurnContext):
    text = (context.activity.text or "").strip()
    user: ChannelAccount = context.activity.from_property or ChannelAccount(id="u1", name="Usuario")
    conv_id = _get_conv_id(context.activity)
    sess = _get_session(conv_id)
    last_intent = sess.get("last_intent")

    # Detecta intención con memoria
    intent = detect_intent(text, last_intent=last_intent)

    # Ayuda / saludo
    if intent == "help":
        # resetea last_intent para no arrastrar contexto si solo chatea
        sess["last_intent"] = None
        await context.send_activity(_help_text())
        return

    # Filtros detectados
    filters = extract_filters(text)

    # Si el usuario dijo "todo el mes", limpiamos filtros puntuales de día
    if filters.get("whole_month"):
        filters.pop("date_day", None)

    # Llamada al backend N2SQL
    payload = {
        "user": {"id": user.id or "u1", "name": user.name or "Usuario"},
        "intent": intent,
        "utterance": text,
        "filters": filters,  # el backend puede ignorarlo si aún no lo soporta
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

    # Actualiza last_intent SOLO si hubo resultados o si el intent es válido
    sess["last_intent"] = intent

    await _reply_markdown(context, title, cols, rows, limit=PAGE_LIMIT)

# -----------------------------------------------------------------------------
# 8) Rutas HTTP (health/diag + BF)
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

@app.get("/diag/session")
def diag_session():
    # diagnóstico simple de memoria en RAM
    return {"conversations": list(SESS.keys()), "memory": SESS}

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
