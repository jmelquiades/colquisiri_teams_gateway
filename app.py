# app.py — Teams gateway usando FastAPI + BotFrameworkAdapter (SDK 4.14.x)
import logging
import os
from typing import Optional

import jwt  # PyJWT
import msal
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse

from botbuilder.core import (
    TurnContext,
    BotFrameworkAdapter,
    BotFrameworkAdapterSettings,
)
from botbuilder.schema import Activity
from botframework.connector.auth import MicrosoftAppCredentials

# Tu bot
from bot import DataTalkBot

# -----------------------------------------------------------------------------
# Logging
# -----------------------------------------------------------------------------
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(levelname)s:%(name)s:%(message)s",
)
log = logging.getLogger("teams-gateway")

# -----------------------------------------------------------------------------
# Helpers ENV
# -----------------------------------------------------------------------------
def _env(name: str, fallback: str = "") -> str:
    # Admite mayúsculas y camelCase (compatibilidad con Render)
    return os.getenv(
        name,
        os.getenv(
            {
                "MICROSOFT_APP_ID": "MicrosoftAppId",
                "MICROSOFT_APP_PASSWORD": "MicrosoftAppPassword",
                "MICROSOFT_APP_TENANT_ID": "MicrosoftAppTenantId",
                "MICROSOFT_APP_TYPE": "MicrosoftAppType",
                "TO_CHANNEL_FROM_BOT_OAUTH_SCOPE": "ToChannelFromBotOAuthScope",
            }.get(name, ""),
            fallback,
        ),
    )

def public_env_snapshot() -> dict:
    keys = [
        "MICROSOFT_APP_ID",
        "MICROSOFT_APP_PASSWORD",
        "MICROSOFT_APP_TENANT_ID",
        "MICROSOFT_APP_TYPE",
        "TO_CHANNEL_FROM_BOT_OAUTH_SCOPE",
        "MicrosoftAppId",
        "MicrosoftAppPassword",
        "MicrosoftAppTenantId",
        "MicrosoftAppType",
        "ToChannelFromBotOAuthScope",
        "PORT",
    ]
    out = {}
    for k in keys:
        out[k] = "SET(***masked***)" if _env(k) else "MISSING"
    out["EFFECTIVE_APP_ID"] = _env("MICROSOFT_APP_ID", "")
    out["EFFECTIVE_TENANT"] = _env("MICROSOFT_APP_TENANT_ID", "")
    out["EFFECTIVE_APP_TYPE"] = _env("MICROSOFT_APP_TYPE", "") or "MultiTenant"
    return out

# -----------------------------------------------------------------------------
# Credenciales básicas
# -----------------------------------------------------------------------------
APP_ID = _env("MICROSOFT_APP_ID", "")
APP_PASSWORD = _env("MICROSOFT_APP_PASSWORD", "")
TENANT = _env("MICROSOFT_APP_TENANT_ID", "") or "organizations"

# Adapter clásico (NO CloudAdapter)
adapter = BotFrameworkAdapter(BotFrameworkAdapterSettings(APP_ID, APP_PASSWORD))
log.info("Adapter: BotFrameworkAdapter 4.14.x (SIN CloudAdapter)")

bot = DataTalkBot()

# -----------------------------------------------------------------------------
# Errores globales
# -----------------------------------------------------------------------------
async def on_error(context: TurnContext, error: Exception):
    log.error("[BOT ERROR] %s", error, exc_info=True)
    try:
        await context.send_activity(
            "Ocurrió un error procesando tu mensaje. Estamos corrigiéndolo."
        )
    except Exception as e:
        log.error("[BOT ERROR][send_activity] %s", e, exc_info=True)

adapter.on_turn_error = on_error

# -----------------------------------------------------------------------------
# Utilitarios de diagnóstico
# -----------------------------------------------------------------------------
SCOPE = ["https://api.botframework.com/.default"]
AUTH_TENANT = f"https://login.microsoftonline.com/{TENANT}"
AUTH_BF = "https://login.microsoftonline.com/botframework.com"

def _decode_bearer_unverified(authorization_header: str) -> dict:
    if not authorization_header or not authorization_header.lower().startswith("bearer "):
        return {}
    token = authorization_header.split(" ", 1)[1].strip()
    try:
        return jwt.decode(token, options={"verify_signature": False, "verify_aud": False})
    except Exception:
        return {}

# -----------------------------------------------------------------------------
# FastAPI app
# -----------------------------------------------------------------------------
app = FastAPI()

@app.get("/health")
async def health():
    return {"ok": True}

@app.get("/diag/env")
async def diag_env():
    return public_env_snapshot()

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
    if "application/json" not in req.headers.get("content-type", ""):
        return PlainTextResponse("Content-Type must be application/json", status_code=415)

    body = await req.json()
    activity: Activity = Activity().deserialize(body)
    auth_header = req.headers.get("Authorization", "")

    recipient_raw = getattr(activity.recipient, "id", "")
    channel_id = getattr(activity, "channel_id", "")
    service_url = getattr(activity, "service_url", "")

    normalized = recipient_raw
    if channel_id == "msteams" and recipient_raw.startswith("28:"):
        normalized = recipient_raw.split("28:")[-1]

    log.info(
        "[DIAG] Our APP_ID=%s | activity.recipient.id(raw)=%s | channel=%s | serviceUrl=%s",
        APP_ID, recipient_raw, channel_id, service_url
    )
    if channel_id == "msteams":
        log.info("[DIAG][msteams] normalized=%s", normalized)

    # JWT entrante (solo para diagnóstico)
    claims = _decode_bearer_unverified(auth_header)
    appid = claims.get("appid") or claims.get("azp")
    tid = claims.get("tid")
    ver = claims.get("ver")
    iss = claims.get("iss")
    aud = claims.get("aud")
    log.info("[JWT] iss=%s | aud=%s | appid=%s | tid=%s | ver=%s", iss, aud, appid, tid, ver)

    # Mismatch AppId (manifest apuntando a otro bot)
    target = normalized if channel_id in ("msteams", "skype") else recipient_raw
    if target and APP_ID and target != APP_ID:
        log.error(
            "[MISMATCH] Mensaje para botId=%s, pero proceso firma como=%s. Revisa AppId/secret/manifest.",
            target, APP_ID
        )

    # Confiar en serviceUrl para respuestas salientes
    try:
        MicrosoftAppCredentials.trust_service_url(service_url)
    except Exception as e:
        log.warning("No se pudo registrar trust_service_url(%s): %s", service_url, e)

    async def aux(turn_context: TurnContext):
        try:
            await bot.on_turn(turn_context)
        except Exception as ex:
            log.error("[BOT ERROR] %s", ex, exc_info=True)
            raise

    # Orden correcto en 4.14.x: (activity, auth_header, callback)
    await adapter.process_activity(activity, auth_header, aux)
    return Response(status_code=201)
