# bot_app.py
# -----------------------------------------------------------------------------
# Gateway para Microsoft Bot Framework (Web Chat/Teams)
# - Recibe /api/messages (Adapter SDK 4.14.3 valida el token entrante)
# - Llama al backend /n2sql/run
# - Responde SIEMPRE con ConnectorClient.send_to_conversation (sin await)
# - Activity JSON con 'from' y 'recipient' explícitos
# - Diags: /health, /diag/env, /diag/msal, /diag/sdk-token
# -----------------------------------------------------------------------------

import os
import json
from typing import List, Dict, Any, Tuple

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
# 3) Estado corto por conversación (consentimiento)
# -----------------------------------------------------------------------------
CONV_STATE: Dict[str, Dict[str, Any]] = {}

# -----------------------------------------------------------------------------
# 4) Utils: tabla markdown y envío de mensajes
# -----------------------------------------------------------------------------
def _markdown_table(cols: List[str], rows: List[Dict[str, Any]], limit: int = PAGE_LIMIT) -> str:
    if not rows:
        return "_(sin resultados)_"
    header = "| " + " | ".join(cols) + " |"
    sep    = "| " + " | ".join(["---"] * len(cols)) + " |"
    body   = "\n".join("| " + " | ".join(str(r.get(c, "")) for c in cols) + " |" for r in rows[:limit])
    return f"{header}\n{sep}\n{body}\n\n_Mostrando hasta {limit} filas._"

def _extract_context_parts(context: TurnContext) -> Tuple[bool, Dict[str, Any]]:
    """Extrae piezas necesarias del TurnContext y valida que haya lo mínimo para responder."""
    act_in = context.activity
    su           = getattr(act_in, "service_url", None)
    channel_id   = getattr(act_in, "channel_id", None)
    conv_in      = getattr(act_in, "conversation", None)
    conv_id      = conv_in.id if conv_in else None
    user_acc     = getattr(act_in, "from_property", None)  # usuario que escribió
    bot_acc      = getattr(act_in, "recipient", None)      # el bot (este gateway)
    locale       = getattr(act_in, "locale", None) or DEFAULT_LOCALE

    ok = bool(su and channel_id and conv_id and user_acc and bot_acc)
    pieces = {
        "su": su,
        "channel_id": channel_id,
        "conv_id": conv_id,
        "user_acc": user_acc,
        "bot_acc": bot_acc,
        "locale": locale
    }
    return ok, pieces

def _build_connector_and_identities(pieces: Dict[str, Any]) -> Tuple[ConnectorClient, Dict[str, str]]:
    """Crea ConnectorClient + identidades bot/usuario."""
    su       = pieces["su"]
    user_acc = pieces["user_acc"]
    bot_acc  = pieces["bot_acc"]

    # Confiar serviceUrl (recomendado por Microsoft)
    if su:
        try:
            MicrosoftAppCredentials.trust_service_url(su)
        except Exception as e:
            print(f"[gw] WARN trust_service_url: {e}")

    creds = MicrosoftAppCredentials(
        app_id=APP_ID,
        password=APP_PWD,
        channel_auth_tenant=TENANT if (APP_TYPE == "SingleTenant" and TENANT) else None,
        oauth_scope="https://api.botframework.com/.default",
    )
    connector = ConnectorClient(credentials=creds, base_url=su)

    bot_id   = getattr(bot_acc, "id", None) or APP_ID
    bot_name = getattr(bot_acc, "name", None) or "Bot"
    usr_id   = getattr(user_acc, "id", None) or "user"
    usr_name = getattr(user_acc, "name", None) or "Usuario"

    idents = {
        "bot_id": bot_id,
        "bot_name": bot_name,
        "usr_id": usr_id,
        "usr_name": usr_name
    }
    return connector, idents

def _send_text(context: TurnContext, text: str):
    """Envía un mensaje de texto (sin markdown sofisticado). Usa ConnectorClient (sin await)."""
    ok, p = _extract_context_parts(context)
    if not ok:
        print("[gw] No hay contexto suficiente para responder (texto).")
        return
    connector, idents = _build_connector_and_identities(p)
    activity = {
        "type": "message",
        "channelId": p["channel_id"],
        "serviceUrl": p["su"],
        "conversation": {"id": p["conv_id"]},
        "from": {"id": idents["bot_id"], "name": idents["bot_name"], "role": "bot"},
        "recipient": {"id": idents["usr_id"], "name": idents["usr_name"], "role": "user"},
        "text": text,
        "locale": p["locale"],
    }
    try:
        connector.conversations.send_to_conversation(conversation_id=p["conv_id"], activity=activity)
    except Exception as e:
        print(f"[gw] ERROR _send_text.send_to_conversation: {repr(e)}")

async def _reply_markdown(context: TurnContext, title: str, cols: List[str], rows: List[Dict[str, Any]], limit: int = PAGE_LIMIT):
    """Envía Markdown con ConnectorClient (SDK 4.14.3) — sin usar context.send_activity."""
    ok, p = _extract_context_parts(context)
    if not ok:
        print("[gw] No hay contexto suficiente para responder (markdown).")
        return

    md = f"{title}\n\n{_markdown_table(cols, rows, limit)}"

    if REPLY_MODE.lower() == "silent":
        print("[gw] SILENT MODE — markdown construido (no enviado):")
        print(md[:2000])
        return

    connector, idents = _build_connector_and_identities(p)
    activity = {
        "type": "message",
        "channelId": p["channel_id"],
        "serviceUrl": p["su"],
        "conversation": {"id": p["conv_id"]},
        "from": {"id": idents["bot_id"], "name": idents["bot_name"], "role": "bot"},
        "recipient": {"id": idents["usr_id"], "name": idents["usr_name"], "role": "user"},
        "textFormat": "markdown",
        "text": md,
        "locale": p["locale"],
    }
    try:
        connector.conversations.send_to_conversation(conversation_id=p["conv_id"], activity=activity)
    except Exception as e:
        print(f"[gw] ERROR ConnectorClient.send_to_conversation: {repr(e)}")

# -----------------------------------------------------------------------------
# 5) NLU simple + ayuda
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
    return "help"

def _help_text() -> str:
    return (
        "Hola, soy **Lucero**. Hoy te ayudo con **facturación**.\n\n"
        "**Puedo ayudarte con:**\n"
        "• `facturas que vencen este mes`\n"
        "• `facturas vencidas hoy`\n"
        "• `top clientes por saldo vencido`\n"
        "\n_Escribe un comando o una frase similar._"
    )

# -----------------------------------------------------------------------------
# 6) Consentimiento
# -----------------------------------------------------------------------------
YES_WORDS = {"si", "sí", "guardar", "ok", "dale", "claro", "de acuerdo", "acepto", "perfecto"}
NO_WORDS  = {"no", "luego", "después", "otro momento"}
CRIT_WORDS = {"critico", "crítico", "urgente", "prioridad"}

def _user_says_yes(t: str) -> bool:
    t = (t or "").lower().strip()
    return any(w in t for w in YES_WORDS)

def _user_says_no(t: str) -> bool:
    t = (t or "").lower().strip()
    return any(w in t for w in NO_WORDS)

def _user_says_critical(t: str) -> bool:
    t = (t or "").lower().strip()
    return any(w in t for w in CRIT_WORDS)

# -----------------------------------------------------------------------------
# 7) Handler principal
# -----------------------------------------------------------------------------
async def on_message(context: TurnContext):
    text = (context.activity.text or "").strip()
    user: ChannelAccount = context.activity.from_property or ChannelAccount(id="u1", name="Usuario")
    act = context.activity

    conv_id = act.conversation.id if act.conversation else "conv"
    state = CONV_STATE.get(conv_id) or {}

    # (A) ¿estábamos esperando consentimiento?
    if state.get("awaiting_feedback"):
        if _user_says_yes(text) or _user_says_critical(text):
            priority = "critical" if _user_says_critical(text) else "normal"
            fb = state.get("feedback_payload") or {}
            fb.update({"priority": priority, "user_opt_in": True})
            try:
                async with httpx.AsyncClient(timeout=15) as client:
                    await client.post(f"{BACKEND_URL}/learning/feedback", json=fb)
                _send_text(context, "¡Listo! Guardé tu solicitud para entrenamiento. Gracias por ayudarme a mejorar 🙌")
            except Exception as e:
                print(f"[gw] ERROR enviando feedback: {repr(e)}")
                _send_text(context, "Tomé nota interna para entrenamiento. ¡Gracias!")
            finally:
                state["awaiting_feedback"] = False
                state["feedback_payload"] = None
                CONV_STATE[conv_id] = state
            return

        if _user_says_no(text):
            _send_text(context, "¡Perfecto! Continuamos. Si deseas, puedo mostrar **facturas que vencen este mes** o **vencidas hoy**.")
            state["awaiting_feedback"] = False
            state["feedback_payload"] = None
            CONV_STATE[conv_id] = state
            return

        _send_text(context, "¿Deseas que guarde tu pedido para entrenamiento? Responde **guardar**, **crítico** o **luego**.")
        return

    # (B) flujo normal
    intent = detect_intent(text)
    if intent == "help":
        _send_text(context, _help_text())
        return

    payload = {
        "user": {"id": user.id or "u1", "name": user.name or "Usuario"},
        "intent": intent,
        "utterance": text,
    }

    # llamada backend
    data: Dict[str, Any]
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(f"{BACKEND_URL}/n2sql/run", json=payload)
            if r.status_code >= 400:
                _send_text(context, f"⚠️ Backend: {r.text}")
                return
            data = r.json()
    except Exception as e:
        print(f"[gw] ERROR llamando backend: {repr(e)}")
        _send_text(context, "Ocurrió un detalle al consultar el backend. Intentémoslo de nuevo en un momento, por favor.")
        return

    cols: List[str] = data.get("columns", []) or []
    rows: List[Dict[str, Any]] = data.get("rows", []) or []
    summary: str = data.get("summary", "") or f"{len(rows)} filas."
    title = f"**{intent}** — {summary}"

    # si no hay resultados, ofrecer guardar para entrenamiento
    if not rows:
        _send_text(context, "Gracias por tu pedido. Estoy en mejora continua 💡.")
        _send_text(context, "¿Deseas que guarde tu solicitud para que el equipo me entrene y la próxima vez te ayude mejor? (responde **guardar**, **crítico** o **luego**)")
        # payload de feedback
        tenant_id = None
        try:
            cd = act.channel_data or {}
            tenant_id = (cd.get("tenant") or {}).get("id")
        except Exception:
            pass

        fb_payload = {
            "user": {"id": user.id, "name": user.name},
            "utterance_raw": text,
            "normalized_text": text,
            "language": (act.locale or DEFAULT_LOCALE)[:2],
            "timezone": "America/Lima",
            "detected_intent": intent,
            "entities_json": None,
            "row_count": 0,
            "sql_used": data.get("sql"),
            "category": "no_results",
            "priority": "normal",
            "user_opt_in": False,
            "channel_id": act.channel_id,
            "conversation_id": conv_id,
            "message_id": act.id,
            "service_url": act.service_url,
            "tenant_id": tenant_id,
            "extra_json": {"backend_stats": data.get("stats")}
        }
        state["awaiting_feedback"] = True
        state["feedback_payload"] = fb_payload
        CONV_STATE[conv_id] = state
        return

    # entregar resultados
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
