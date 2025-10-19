# app.py — Gateway Teams con FastAPI + BotFrameworkAdapter (SDK 4.14.x)
import os
import json
import logging
from typing import Dict, Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse

from botbuilder.core import (
    TurnContext,
    BotFrameworkAdapter,
    BotFrameworkAdapterSettings,
    MessageFactory,
)
from botbuilder.schema import Activity
from botframework.connector.auth import MicrosoftAppCredentials

import msal
import jwt  # PyJWT, para diagnóstico simple sin verificar firma

# =======================
# Tu bot (echo de prueba)
# =======================
class DataTalkBot:
    async def on_turn(self, turn_context: TurnContext):
        if turn_context.activity.type == "message":
            text = (turn_context.activity.text or "").strip()
            if not text:
                text = "(mensaje vacío)"
            await turn_context.send_activity(MessageFactory.text(f"ECO: {text}"))

# ----------------------
# Logging básico
# ----------------------
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(levelname)s:%(name)s:%(message)s",
)
log = logging.getLogger("teams-gateway")

# =========================
# Helpers de configuración
# =========================
def _env(name: str, fallback: str = "") -> str:
    return os.getenv(
        name,
        os.getenv(
            {
                "MICROSOFT_APP_ID": "MicrosoftAppId",
                "MICROSOFT_APP_PASSWORD": "MicrosoftAppPassword",
                "MICROSOFT_APP_TENANT_ID": "MicrosoftAppTenantId",
                "MICROSOFT_APP_TYPE": "MicrosoftAppType",
            }.get(name, ""),
            fallback,
        ),
    )

def public_env_snapshot() -> Dict[str, Any]:
    keys = [
        "MICROSOFT_APP_ID",
        "MICROSOFT_APP_PASSWORD",
        "MICROSOFT_APP_TENANT_ID",
        "MICROSOFT_APP_TYPE",
        "MicrosoftAppId",
        "MicrosoftAppPassword",
        "MicrosoftAppTenantId",
        "MicrosoftAppType",
        "PORT",
    ]
    out = {}
    for k in keys:
        out[k] = "SET(***masked***)" if _env(k) else "MISSING"
    out["EFFECTIVE_APP_ID"] = _env("MICROSOFT_APP_ID")
    out["EFFECTIVE_TENANT"] = _env("MICROSOFT_APP_TENANT_ID")
    out["EFFECTIVE_APP_TYPE"] = _env("MICROSOFT_APP_TYPE") or "(not set)"
    return out

# =====================================
# Credenciales (AppId / Password AAD)
# =====================================
APP_ID = _env("MICROSOFT_APP_ID", "")
APP_PASSWORD = _env("MICROSOFT_APP_PASSWORD", "")

# Adapter clásico 4.14.x (el que ya te funciona)
adapter = BotFrameworkAdapter(BotFrameworkAdapterSettings(APP_ID, APP_PASSWORD))

# Instancia del bot
bot = DataTalkBot()

# ==========================
# Manejo global de errores
# ==========================
async def on_error(context: TurnContext, error: Exception):
    log.error("[BOT ERROR] %s", error, exc_info=True)
    try:
        await context.send_activity("Ocurrió un error procesando tu mensaje.")
    except Exception as e:
        log.error("[BOT ERROR][send_activity] %s", e, exc_info=True)

adapter.on_turn_error = on_error

# ==========
# FastAPI
# ==========
app = FastAPI()

@app.get("/health")
async def health():
    return {"ok": True}

@app.get("/diag/env")
async def diag_env():
    return public_env_snapshot()

# --- Diagnósticos MSAL (útiles para validar secreto/alcance) ---
TENANT = _env("MICROSOFT_APP_TENANT_ID") or "organizations"
AUTH_TENANT = f"https://login.microsoftonline.com/{TENANT}"
AUTH_BF = "https://login.microsoftonline.com/botframework.com"
SCOPE = ["https://api.botframework.com/.default"]

@app.get("/diag/msal")
async def diag_msal():
    log.info("Initializing with Entra authority: %s", AUTH_TENANT)
    try:
        appc = msal.ConfidentialClientApplication(
            client_id=APP_ID,
            client_credential=APP_PASSWORD,
            authority=AUTH_TENANT,
        )
        token = appc.acquire_token_for_client(scopes=SCOPE)
        ok = "access_token" in token
        payload = {"ok": ok, "keys": list(token.keys()), "authority": AUTH_TENANT}
        if not ok:
            payload["error"] = token
        return JSONResponse(payload, status_code=200 if ok else 500)
    except Exception as e:
        return JSONResponse({"ok": False, "exception": str(e)}, status_code=500)

@app.get("/diag/msal-bf")
async def diag_msal_bf():
    log.info("Initializing with Entra authority: %s", AUTH_BF)
    try:
        appc = msal.ConfidentialClientApplication(
            client_id=APP_ID,
            client_credential=APP_PASSWORD,
            authority=AUTH_BF,
        )
        token = appc.acquire_token_for_client(scopes=SCOPE)
        ok = "access_token" in token
        payload = {"ok": ok, "keys": list(token.keys()), "authority": AUTH_BF}
        if not ok:
            payload["error"] = token
        return JSONResponse(payload, status_code=200 if ok else 500)
    except Exception as e:
        return JSONResponse({"ok": False, "exception": str(e)}, status_code=500)

@app.post("/api/messages")
async def messages(req: Request):
    # Validación Content-Type simple
    if "application/json" not in req.headers.get("content-type", ""):
        return PlainTextResponse("Content-Type must be application/json", status_code=415)

    body = await req.json()
    activity: Activity = Activity().deserialize(body)
    auth_header = req.headers.get("Authorization", "")

    # Diags de llegada
    recipient_raw = getattr(activity.recipient, "id", "")
    channel_id = getattr(activity, "channel_id", "")
    service_url = getattr(activity, "service_url", "")

    normalized = recipient_raw
    if channel_id == "msteams" and recipient_raw.startswith("28:"):
        normalized = recipient_raw.split("28:")[-1]

    log.info(
        "[DIAG] Our APP_ID=%s | activity.recipient.id(raw)=%s | channel=%s | serviceUrl=%s",
        APP_ID, recipient_raw, channel_id, service_url,
    )
    if channel_id == "msteams":
        log.info("[DIAG][msteams] normalized=%s", normalized)

    # (Opcional) log de claims sin verificar — solo para depurar formato del token
    try:
        if auth_header.startswith("Bearer "):
            token = auth_header.split(" ", 1)[1]
            claims = jwt.decode(token, options={"verify_signature": False, "verify_aud": False})
            iss = claims.get("iss")
            aud = claims.get("aud")
            appid = claims.get("appid") or claims.get("azp")
            tid = claims.get("tid")
            ver = claims.get("ver")
            log.info("[JWT] iss=%s | aud=%s | appid=%s | tid=%s | ver=%s", iss, aud, appid, tid, ver)
    except Exception as e:
        log.warning("[JWT] no se pudieron leer claims sin verificación: %s", e)

    # MUY IMPORTANTE: confiar en el serviceUrl antes de enviar respuestas
    try:
        MicrosoftAppCredentials.trust_service_url(service_url)
    except Exception as e:
        log.warning("trust_service_url error: %s", e)

    async def aux(turn_context: TurnContext):
        try:
            await bot.on_turn(turn_context)
        except Exception as ex:
            log.error("[BOT ERROR] %s", ex, exc_info=True)
            raise

    # Orden correcto para Adapter 4.14.x: (activity, auth_header, callback)
    await adapter.process_activity(activity, auth_header, aux)
    return Response(status_code=201)

# ==========
# Main local
# ==========
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "10000"))
    uvicorn.run("app:app", host="0.0.0.0", port=port)
