# bot_app.py
# -----------------------------------------------------------------------------
# Gateway para Microsoft Bot Framework (Web Chat/Teams) que:
# 1) Recibe Activities en /api/messages
# 2) Autentica con el header Authorization que manda Bot Service
# 3) Llama a tu backend (/n2sql/run)
# 4) Responde en el mismo hilo del canal (Web Chat/Teams) en Markdown
# -----------------------------------------------------------------------------

import os
import json
import httpx
import msal
from typing import List, Dict, Any, Optional

from fastapi import FastAPI, Request, Response
from botbuilder.core import BotFrameworkAdapterSettings, BotFrameworkAdapter, TurnContext
from botbuilder.schema import Activity, ActivityTypes, ChannelAccount
from botframework.connector.auth import MicrosoftAppCredentials

# -----------------------------
# CONFIG (variables de entorno)
# -----------------------------
# Doble nombre por compatibilidad con distintas rutas del SDK
APP_ID  = os.getenv("MICROSOFT_APP_ID") or os.getenv("MicrosoftAppId")
APP_PWD = os.getenv("MICROSOFT_APP_PASSWORD") or os.getenv("MicrosoftAppPassword")

# Tipo de App y Tenant para construir la authority correcta del token saliente
# - SingleTenant: requiere MicrosoftAppTenantId
# - MultiTenant: no requiere TenantId
APP_TYPE = os.getenv("MicrosoftAppType", "MultiTenant")  # "SingleTenant" | "MultiTenant"
TENANT   = os.getenv("MicrosoftAppTenantId")

# Tu backend ya desplegado (donde vive /n2sql/run)
BACKEND_URL = os.getenv("BACKEND_URL", "https://admin-assistant-npsd.onrender.com")

# Modo diagnóstico: si pones REPLY_MODE=silent, arma el markdown pero no envía (útil para aislar problemas de token)
REPLY_MODE = os.getenv("REPLY_MODE", "active")  # "active" | "silent"


# -----------------------------
# APP + ADAPTER
# -----------------------------
app = FastAPI(title="Teams Gateway")

if not APP_ID or not APP_PWD:
    # Mensaje de ayuda en logs si faltan credenciales
    print("[gw] WARNING: faltan MICROSOFT_APP_ID/MicrosoftAppId o MICROSOFT_APP_PASSWORD/MicrosoftAppPassword.")

# El adapter del Bot Framework validará el Authorization entrante
adapter_settings = BotFrameworkAdapterSettings(APP_ID, APP_PWD)
adapter = BotFrameworkAdapter(adapter_settings)


# -----------------------------
# HELPERS
# -----------------------------
def _markdown_table(cols: List[str], rows: List[Dict[str, Any]], limit: int = 10) -> str:
    """
    Construye una tablita Markdown con las primeras 'limit' filas.
    """
    if not rows:
        return "_(sin resultados)_"
    header = "| " + " | ".join(cols) + " |"
    sep    = "| " + " | ".join(["---"] * len(cols)) + " |"
    body   = "\n".join("| " + " | ".join(str(r.get(c, "")) for c in cols) + " |" for r in rows[:limit])
    return f"{header}\n{sep}\n{body}\n\n_Mostrando hasta {limit} filas._"


async def _reply_md(context: TurnContext, title: str, cols: List[str], rows: List[Dict[str, Any]], limit: int = 10):
    """
    Envía un mensaje Markdown de vuelta al canal.

    - Antes de responder, "confiamos" el serviceUrl para que el SDK pueda enviar el reply.
    - Si REPLY_MODE = "silent", no envía; solo imprime en logs (útil para depurar problemas de token saliente).
    """
    # Confiar el serviceUrl (recomendado por MS para evitar bloqueos al responder)
    su = getattr(context.activity, "service_url", None)
    if su:
        try:
            MicrosoftAppCredentials.trust_service_url(su)
        except Exception as e:
            print(f"[gw] WARN trust_service_url: {e}")

    table_md = _markdown_table(cols, rows, limit)
    md = f"{title}\n\n{table_md}"

    if REPLY_MODE.lower() == "silent":
        print("[gw] SILENT MODE — markdown construido (no enviado):")
        print(md[:2000])  # truncar por si es largo
        return

    try:
        await context.send_activity(md)
    except Exception as e:
        # Si fallara al enviar (p. ej., token saliente), dejamos huella clara
        print(f"[gw] ERROR send_activity: {repr(e)}")
        # Aquí no relanzamos para no romper la pipeline; Bot Service ya recibió 200 de nuestro endpoint.


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
                # Si el backend responde error, informamos al usuario
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
    """
    Ping básico. Útil para despertar el servicio en Render.
    """
    return {"ok": True, "service": "teams-gateway"}

@app.get("/health")
def health():
    """
    Health check para Render.
    """
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
    Si devuelve ok: true → el AppId/Secret/Authority están correctos.
    """
    if not APP_ID or not APP_PWD:
        return {"ok": False, "error": "Faltan AppId/Secret"}

    # Authority según tipo de app
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
    # Scope correcto para Bot Framework
    res = cca.acquire_token_for_client(scopes=["https://api.botframework.com/.default"])
    if "access_token" in res:
        return {"ok": True, "token_type": res.get("token_type", "Bearer"), "expires_in": res.get("expires_in")}
    return {"ok": False, "aad_error": res.get("error"), "aad_error_description": res.get("error_description")}

@app.options("/api/messages")
def options_messages():
    """
    Algunos entornos podrían disparar OPTIONS. Lo aceptamos con 200.
    """
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
