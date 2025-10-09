# bot_app.py
# -----------------------------------------------------------------------------
# Gateway para Microsoft Bot Framework (Web Chat/Teams)
# - Recibe /api/messages, valida Authorization entrante con el Adapter
# - Llama a tu backend /n2sql/run
# - Responde en el canal (Teams/Web Chat) con Markdown
# - Usa ConnectorClient para la respuesta (token saliente con scope correcto)
# - Endpoints de diagnóstico: /diag/env, /diag/msal, /diag/sdk-token
# - Compatible con SDK 4.14.3
# -----------------------------------------------------------------------------

import os
import json
from typing import List, Dict, Any

import httpx
import msal
from fastapi import FastAPI, Request, Response

from botbuilder.core import BotFrameworkAdapterSettings, BotFrameworkAdapter, TurnContext
from botbuilder.schema import Activity, ActivityTypes, ChannelAccount
from botframework.connector import ConnectorClient                          # ✅ envío directo
from botframework.connector.auth import MicrosoftAppCredentials, AppCredentials

# -----------------------------------------------------------------------------
# 0) Contexto de nube: fuerza "Public" (evita rutas Gov/DoD que rompen el token)
# -----------------------------------------------------------------------------
os.environ["ChannelService"] = "Public"
os.environ["CHANNEL_SERVICE"] = "Public"

# -----------------------------------------------------------------------------
# 1) Configuración (variables de entorno)
# -----------------------------------------------------------------------------
# Doble nombre por compatibilidad con variantes del SDK
APP_ID  = os.getenv("MICROSOFT_APP_ID") or os.getenv("MicrosoftAppId")
APP_PWD = os.getenv("MICROSOFT_APP_PASSWORD") or os.getenv("MicrosoftAppPassword")

# Tipo de app y Tenant:
# - SingleTenant -> requiere MicrosoftAppTenantId
# - MultiTenant  -> sin TenantId
APP_TYPE = os.getenv("MicrosoftAppType", "MultiTenant")  # "SingleTenant" | "MultiTenant"
TENANT   = os.getenv("MicrosoftAppTenantId")

# Backend (tu servicio N2SQL en Render)
BACKEND_URL = os.getenv("BACKEND_URL", "https://admin-assistant-npsd.onrender.com")

# Modo diagnóstico: "silent" arma el Markdown pero no envía (útil para aislar)
REPLY_MODE  = os.getenv("REPLY_MODE", "active")  # "active" | "silent"


# -----------------------------------------------------------------------------
# 2) Monkey-patch suave: garantizar scope/tenant en credenciales internas del SDK
#    (SDK 4.14.3 no acepta 'oauth_scope' en AdapterSettings; así unificamos)
# -----------------------------------------------------------------------------
_ORIG_APP_CRED_INIT = AppCredentials.__init__

def _patched_appcred_init(self, app_id: str, password: str, *args, **kwargs):
    # Scope correcto para Bot Framework OAuth v2
    if not kwargs.get("oauth_scope"):
        kwargs["oauth_scope"] = "https://api.botframework.com/.default"
    # Tenant por defecto si eres SingleTenant
    if "channel_auth_tenant" not in kwargs and APP_TYPE == "SingleTenant" and TENANT:
        kwargs["channel_auth_tenant"] = TENANT
    return _ORIG_APP_CRED_INIT(self, app_id, password, *args, **kwargs)

AppCredentials.__init__ = _patched_appcred_init  # aplicar antes de crear el adapter


# -----------------------------------------------------------------------------
# 3) FastAPI + Adapter (sin kwargs no soportados por 4.14.3)
# -----------------------------------------------------------------------------
app = FastAPI(title="Teams Gateway")

if not APP_ID or not APP_PWD:
    print("[gw] WARNING: faltan MICROSOFT_APP_ID/MicrosoftAppId o MICROSOFT_APP_PASSWORD/MicrosoftAppPassword.")

adapter_settings = BotFrameworkAdapterSettings(APP_ID, APP_PWD)
adapter = BotFrameworkAdapter(adapter_settings)


# -----------------------------------------------------------------------------
# 4) Helpers de presentación
# -----------------------------------------------------------------------------
def _markdown_table(cols: List[str], rows: List[Dict[str, Any]], limit: int = 10) -> str:
    """
    Construye tabla Markdown con hasta 'limit' filas.
    """
    if not rows:
        return "_(sin resultados)_"
    header = "| " + " | ".join(cols) + " |"
    sep    = "| " + " | ".join(["---"] * len(cols)) + " |"
    body   = "\n".join("| " + " | ".join(str(r.get(c, "")) for c in cols) + " |" for r in rows[:limit])
    return f"{header}\n{sep}\n{body}\n\n_Mostrando hasta {limit} filas._"


async def _reply_md(context: TurnContext, title: str, cols: List[str], rows: List[Dict[str, Any]], limit: int = 10):
    """
    Envía Markdown usando ConnectorClient directamente (evita problemas de token saliente):
    - Confiamos el serviceUrl (recomendación MS).
    - Construimos credenciales con scope '.default' y tenant si aplica.
    - Enviamos con conversations.reply_to_activity (mismo hilo).
    """
    # Datos del canal necesarios para responder
    su = getattr(context.activity, "service_url", None)
    conv = getattr(context.activity, "conversation", None)
    conv_id = conv.id if conv else None
    reply_to_id = getattr(context.activity, "id", None)

    # Confiar el serviceUrl
    if su:
        try:
            MicrosoftAppCredentials.trust_service_url(su)
        except Exception as e:
            print(f"[gw] WARN trust_service_url: {e}")

    # Markdown final
    md = f"{title}\n\n{_markdown_table(cols, rows, limit)}"

    # Modo silencioso: imprime y no envía (útil para diagnósticos)
    if REPLY_MODE.lower() == "silent":
        print("[gw] SILENT MODE — markdown construido (no enviado):")
        print(md[:2000])
        return

    # Si no tuviéramos los IDs del hilo, hacemos fallback informativo
    if not (su and conv_id and reply_to_id):
        try:
            await context.send_activity("⚠️ No se encontró información suficiente del canal para responder.")
        except Exception as e:
            print(f"[gw] ERROR send_activity (fallback): {repr(e)}")
        return

    # Credenciales explícitas (scope correcto + tenant si aplica)
    creds = MicrosoftAppCredentials(
        app_id=APP_ID,
        password=APP_PWD,
        channel_auth_tenant=TENANT if (APP_TYPE == "SingleTenant" and TENANT) else None,
        oauth_scope="https://api.botframework.com/.default",
    )

    # Cliente apuntando al serviceUrl del activity
    connector = ConnectorClient(credentials=creds, base_url=su)

    # Activity de respuesta en Markdown
    reply_activity = {
        "type": "message",
        "textFormat": "markdown",
        "text": md,
    }

    try:
        await connector.conversations.reply_to_activity(
            conversation_id=conv_id,
            activity_id=reply_to_id,
            activity=reply_activity,
        )
    except Exception as e:
        # Último intento con send_activity, por si el cliente directo fallara
        print(f"[gw] ERROR ConnectorClient.reply_to_activity: {repr(e)}")
        try:
            await context.send_activity("⚠️ No pude enviar la respuesta por el canal. Intenta de nuevo.")
        except Exception as e2:
            print(f"[gw] ERROR send_activity (fallback tras ConnectorClient): {repr(e2)}")


# -----------------------------------------------------------------------------
# 5) Lógica principal de mensajes
# -----------------------------------------------------------------------------
async def on_message(context: TurnContext):
    """
    Flujo:
    - Lee texto y usuario
    - Determina intent (demo)
    - Llama backend /n2sql/run
    - Responde con Markdown
    """
    text = (context.activity.text or "").strip()
    user: ChannelAccount = context.activity.from_property or ChannelAccount(id="u1", name="Usuario")

    # DEMO de intent (ajústalo con tu router real cuando quieras)
    intent = "invoices_due_this_month" if ("vencen" in text.lower() and "mes" in text.lower()) else "top_clients_overdue"

    payload = {
        "user": {"id": user.id or "u1", "name": user.name or "Usuario"},
        "intent": intent,
        "utterance": text,
    }

    # Llamada al backend
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
    await _reply_md(context, title, cols, rows, limit=10)


# -----------------------------------------------------------------------------
# 6) Rutas HTTP (health/diag + BF)
# -----------------------------------------------------------------------------
@app.get("/")
def root():
    """Ping básico para Render."""
    return {"ok": True, "service": "teams-gateway"}

@app.get("/health")
def health():
    """Health check simple."""
    return {"ok": True}

@app.get("/diag/env")
def diag_env():
    """
    Diagnóstico de variables (sin exponer secretos).
    """
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
    """
    Verifica token saliente usando MSAL directamente (fuera del SDK).
    """
    if not APP_ID or not APP_PWD:
        return {"ok": False, "error": "Faltan AppId/Secret"}
    authority = (
        f"https://login.microsoftonline.com/{TENANT}"
        if (APP_TYPE == "SingleTenant" and TENANT)
        else "https://login.microsoftonline.com/organizations"
    )
    cca = msal.ConfidentialClientApplication(
        client_id=APP_ID,
        client_credential=APP_PWD,
        authority=authority,
    )
    res = cca.acquire_token_for_client(scopes=["https://api.botframework.com/.default"])
    if "access_token" in res:
        return {"ok": True, "token_type": res.get("token_type", "Bearer"), "expires_in": res.get("expires_in")}
    return {"ok": False, "aad_error": res.get("error"), "aad_error_description": res.get("error_description")}

@app.get("/diag/sdk-token")
def diag_sdk_token():
    """
    Verifica token saliente usando las mismas credenciales que usamos en ConnectorClient.
    """
    try:
        creds = MicrosoftAppCredentials(
            app_id=APP_ID,
            password=APP_PWD,
            channel_auth_tenant=TENANT if (APP_TYPE == "SingleTenant" and TENANT) else None,
            oauth_scope="https://api.botframework.com/.default",
        )
        tok = creds.get_access_token()  # "Bearer eyJ..." o similar
        ok = bool(tok)
        prefix = tok[:12] if tok else ""
        return {"ok": ok, "prefix": prefix}
    except Exception as e:
        return {"ok": False, "error": repr(e)}

@app.options("/api/messages")
def options_messages():
    """Acepta OPTIONS en /api/messages (por si hay preflight)."""
    return Response(status_code=200)

@app.post("/api/messages")
async def api_messages(req: Request):
    """
    Endpoint principal del Bot Framework:
    - Lee Activity
    - Pasa Authorization al adapter (requisito para validar)
    - Ejecuta on_message en mensajes de usuario
    - Retorna 200 al Bot Service (la respuesta al usuario viaja por el canal)
    """
    body = await req.json()
    activity = Activity().deserialize(body)

    # Authorization real del Bot Service (case-insensitive)
    auth_header = req.headers.get("authorization") or req.headers.get("Authorization") or ""
    client = req.client.host if req.client else "unknown"
    print(f"[gw] has_auth={bool(auth_header)} from={client}")

    async def aux(turn_context: TurnContext):
        if activity.type == ActivityTypes.message:
            await on_message(turn_context)
        else:
            # conversationUpdate, invoke, etc. (si los quieres manejar luego)
            pass

    # Validación y pipeline del SDK
    await adapter.process_activity(activity, auth_header, aux)

    return Response(status_code=200, content=json.dumps({"ok": True}), media_type="application/json")
