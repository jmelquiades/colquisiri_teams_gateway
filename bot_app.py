# bot_app.py
# -----------------------------------------------------------------------------
# Gateway para Microsoft Bot Framework (Web Chat/Teams) que:
# 1) Recibe Activities en /api/messages
# 2) Autentica con el header Authorization que manda Bot Service
# 3) Llama a tu backend (/n2sql/run)
# 4) Responde en el mismo hilo (Web Chat/Teams) en Markdown
# Incluye diagnósticos: /diag/env, /diag/msal, /diag/sdk-token
# -----------------------------------------------------------------------------

import os
import json
from typing import List, Dict, Any

import httpx
import msal
from fastapi import FastAPI, Request, Response
from botbuilder.core import BotFrameworkAdapterSettings, BotFrameworkAdapter, TurnContext
from botbuilder.schema import Activity, ActivityTypes, ChannelAccount
from botframework.connector.auth import MicrosoftAppCredentials

# -------------------------------------------------------------------
# Fuerza entorno "Public" para evitar rutas Gov/DoD en el SDK.
# (Inocuo si ya estás en Public; previene tokens sin 'access_token'.)
# -------------------------------------------------------------------
os.environ["ChannelService"] = "Public"
os.environ["CHANNEL_SERVICE"] = "Public"

# -----------------------------
# CONFIG (variables de entorno)
# -----------------------------
# Doble nombre por compatibilidad con distintas rutas del SDK
APP_ID  = os.getenv("MICROSOFT_APP_ID") or os.getenv("MicrosoftAppId")
APP_PWD = os.getenv("MICROSOFT_APP_PASSWORD") or os.getenv("MicrosoftAppPassword")

# Tipo de app y Tenant:
#  - SingleTenant -> requiere MicrosoftAppTenantId
#  - MultiTenant  -> sin TenantId
APP_TYPE = os.getenv("MicrosoftAppType", "MultiTenant")  # "SingleTenant" | "MultiTenant"
TENANT   = os.getenv("MicrosoftAppTenantId")

# Tu backend ya desplegado (donde vive /n2sql/run)
BACKEND_URL = os.getenv("BACKEND_URL", "https://admin-assistant-npsd.onrender.com")

# Modo diagnóstico: si pones REPLY_MODE=silent, arma el markdown pero no envía
REPLY_MODE = os.getenv("REPLY_MODE", "active")  # "active" | "silent"


# -----------------------------
# APP + ADAPTER
# -----------------------------
app = FastAPI(title="Teams Gateway")

if not APP_ID or not APP_PWD:
    print("[gw] WARNING: faltan MICROSOFT_APP_ID/MicrosoftAppId o MICROSOFT_APP_PASSWORD/MicrosoftAppPassword.")

adapter_settings = BotFrameworkAdapterSettings(APP_ID, APP_PWD)
adapter = BotFrameworkAdapter(adapter_settings)


# -----------------------------
# HELPERS
# -----------------------------
def _markdown_table(cols: List[str], rows: List[Dict[str, Any]], limit: int = 10) -> str:
    """Construye una tablita Markdown con las primeras 'limit' filas."""
    if not rows:
        return "_(sin resultados)_"
    header = "| " + " | ".join(cols) + " |"
    sep    = "| " + " | ".join(["---"] * len(cols)) + " |"
    body   = "\n".join("| " + " | ".join(str(r.get(c, "")) for c in cols) + " |" for r in rows[:limit])
    return f"{header}\n{sep}\n{body}\n\n_Mostrando hasta {limit} filas._"


async def _reply_md(context: TurnContext, title: str, cols: List[str], rows: List[Dict[str, Any]], limit: int = 10):
    """
    Envía un mensaje Markdown de vuelta al canal.
    - Antes de responder, "confiamos" el serviceUrl (recomendado por MS).
    - Si REPLY_MODE = "silent", imprime el markdown en logs y no envía.
    """
    su = getattr(context.activity, "service_url", None)
    if su:
        try:
            MicrosoftAppCredentials.trust_service_url(su)
        except Exception as e:
            print(f"[gw] WARN trust_service_url: {e}")

    md = f"{title}\n\n{_markdown_table(cols, rows, limit)}"

    if REPLY_MODE.lower() == "silent":
        print("[gw] SILENT MODE — markdown construido (no enviado):")
        print(md[:2000])  # truncar por si es largo
        return

    try:
        await context.send_activity(md)
    except Exception as e:
        # Si fallara al enviar (p. ej., token saliente), dejamos huella clara
        print(f"[gw] ERROR send_activity: {repr(e)}")
        # No relanzamos para no romper la pipeline; Bot Service ya recibió 200.


async def on_message(context: TurnContext):
    """
    Maneja mensajes de usuario:
    - Lee el texto
    - Decide intención (demo simple)
    - Llama al backend /n2sql/run
    - Responde con tabla Markdown
    """
    text = (context.activity.text or "").strip()
    user: ChannelAccount = context.activity.from_property or ChannelAccount(id="u1", name="Usuario")

    # Enrutamiento DEMO (puedes reemplazarlo por tu router real)
    intent = "invoices_due_this_month" if ("vencen" in text.lower() and "mes" in text.lower()) else "top_clients_overdue"

    payload = {
        "user": {"id": user.id or "u1", "name": user.name or "Usuario"},
        "intent": intent,
        "utterance": text,
    }

    # Llamada a tu backend
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


# -----------------------------
# RUTAS HTTP (health/diag + BF)
# -----------------------------
@app.get("/")
def root():
    """Ping básico. Útil para despertar el servicio en Render."""
    return {"ok": True, "service": "teams-gateway"}

@app.get("/health")
def health():
    """Health check para Render."""
    return {"ok": True}

@app.get("/diag/env")
def diag_env():
    """
    Diagnóstico de variables: no expone secretos, solo longitudes y flags.
    Útil para saber si el contenedor cargó las vars correctas.
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
    Prueba inequívoca de token saliente contra AAD (fuera del SDK).
    Si devuelve ok: true → AppId/Secret/Authority están correctos.
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
    Pide un token usando el MISMO mecanismo del SDK (cercano a send_activity).
    Si aquí es ok: true, el reply debería funcionar.
    """
    try:
        creds = MicrosoftAppCredentials(
            app_id=APP_ID,
            password=APP_PWD,
            channel_auth_tenant=TENANT if (APP_TYPE == "SingleTenant" and TENANT) else None,
            oauth_scope="https://api.botframework.com/.default",
        )
        tok = creds.get_access_token()  # string "Bearer eyJ..." o similar
        ok = bool(tok)
        prefix = tok[:12] if tok else ""
        return {"ok": ok, "prefix": prefix}
    except Exception as e:
        return {"ok": False, "error": repr(e)}

@app.options("/api/messages")
def options_messages():
    """Algunos entornos disparan OPTIONS. Lo aceptamos con 200."""
    return Response(status_code=200)

@app.post("/api/messages")
async def api_messages(req: Request):
    """
    Endpoint principal del Bot Framework.
    - Lee el body como Activity
    - Toma el header Authorization (Bearer ...) y se lo pasa al adapter
    - Delega el manejo a on_message (u otros tipos de Activity)
    """
    body = await req.json()
    activity = Activity().deserialize(body)

    # Authorization real del Bot Service (requisito para authenticate_request)
    auth_header = req.headers.get("authorization") or req.headers.get("Authorization") or ""
    client = req.client.host if req.client else "unknown"
    print(f"[gw] has_auth={bool(auth_header)} from={client}")

    async def aux(turn_context: TurnContext):
        if activity.type == ActivityTypes.message:
            await on_message(turn_context)
        else:
            # Aquí se pueden manejar conversationUpdate, invoke, etc. si los necesitas
            pass

    # El adapter valida el token entrante y, si todo OK, nos deja procesar
    await adapter.process_activity(activity, auth_header, aux)

    # Respondemos 200 a Bot Service (el mensaje al usuario se manda en send_activity)
    return Response(status_code=200, content=json.dumps({"ok": True}), media_type="application/json")
