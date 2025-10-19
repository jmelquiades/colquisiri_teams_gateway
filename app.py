# app.py — Teams Gateway (FastAPI + BotFrameworkAdapter 4.14.x)

import os
import json
import logging
from typing import Dict

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse

from botbuilder.core import (
    BotFrameworkAdapter,
    BotFrameworkAdapterSettings,
    ActivityHandler,
    TurnContext,
    MessageFactory,
)
from botbuilder.schema import Activity
from botframework.connector.auth import MicrosoftAppCredentials

import msal

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
    """
    Lee primero MAYÚSCULAS; si no existe, intenta camelCase (compat Render).
    """
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


def public_env_snapshot() -> Dict[str, str]:
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
        v = _env(k)
        out[k] = "SET(***masked***)" if v else "MISSING"
    # Derivados útiles
    out["EFFECTIVE_APP_ID"] = _env("MICROSOFT_APP_ID")
    out["EFFECTIVE_TENANT"] = _env("MICROSOFT_APP_TENANT_ID")
    out["EFFECTIVE_APP_TYPE"] = _env("MICROSOFT_APP_TYPE") or "MultiTenant"
    return out


# =====================================
# Credenciales y “airbag” de scope
# =====================================
# Airbag: si el scope no está en env, lo ponemos nosotros para 4.14.x
os.environ.setdefault(
    "ToChannelFromBotOAuthScope", "https://api.botframework.com/.default"
)
os.environ.setdefault(
    "TO_CHANNEL_FROM_BOT_OAUTH_SCOPE", "https://api.botframework.com/.default"
)

APP_ID = _env("MICROSOFT_APP_ID", "")
APP_PASSWORD = _env("MICROSOFT_APP_PASSWORD", "")

adapter = BotFrameworkAdapter(BotFrameworkAdapterSettings(APP_ID, APP_PASSWORD))


# ===============
# Bot sencillo
# ===============
class EchoBot(ActivityHandler):
    async def on_message_activity(self, turn_context: TurnContext):
        text = (turn_context.activity.text or "").strip()
        await turn_context.send_activity(MessageFactory.text(f"ECO: {text}"))


bot = EchoBot()


# ==========================
# Manejo global de errores
# ==========================
async def on_error(context: TurnContext, error: Exception):
    log.error("[BOT ERROR] %s", error, exc_info=True)
    try:
        await context.send_activity(
            "Ocurrió un error procesando tu mensaje. Estamos corrigiéndolo."
        )
    except Exception as e:
        log.error("[BOT ERROR][send_activity] %s", e, exc_info=True)


adapter.on_turn_error = on_error


# ==========
# FastAPI
# ==========
app = FastAPI()


@app.get("/health")
async def health():
    log.info("Health check solicitado.")
    return {"ok": True}


@app.get("/diag/env")
async def diag_env():
    return JSONResponse(public_env_snapshot())


# --- Diagnósticos MSAL (para validar secreto y tenant) ---
TENANT = _env("MICROSOFT_APP_TENANT_ID") or "organizations"
AUTH_TENANT = f"https://login.microsoftonline.com/{TENANT}"
AUTH_BF = "https://login.microsoftonline.com/botframework.com"
SCOPE = ["https://api.botframework.com/.default"]


@app.get("/diag/msal")
async def diag_msal():
    try:
        appc = msal.ConfidentialClientApplication(
            client_id=APP_ID, client_credential=APP_PASSWORD, authority=AUTH_TENANT
        )
        token = appc.acquire_token_for_client(scopes=SCOPE)
        ok = "access_token" in token
        payload = {"ok": ok, "keys": list(token.keys()), "authority": AUTH_TENANT}
        if not ok:
            payload["error"] = {k: v for k, v in token.items() if k != "access_token"}
        return JSONResponse(payload, status_code=200 if ok else 500)
    except Exception as e:
        return JSONResponse({"ok": False, "exception": str(e)}, status_code=500)


@app.get("/diag/msal-bf")
async def diag_msal_bf():
    try:
        appc = msal.ConfidentialClientApplication(
            client_id=APP_ID, client_credential=APP_PASSWORD, authority=AUTH_BF
        )
        token = appc.acquire_token_for_client(scopes=SCOPE)
        ok = "access_token" in token
        payload = {"ok": ok, "keys": list(token.keys()), "authority": AUTH_BF}
        if not ok:
            payload["error"] = {k: v for k, v in token.items() if k != "access_token"}
        return JSONResponse(payload, status_code=200 if ok else 500)
    except Exception as e:
        return JSONResponse({"ok": False, "exception": str(e)}, status_code=500)


# ==========
# Endpoint de Teams
# ==========
@app.post("/api/messages")
async def messages(request: Request):
    if "application/json" not in (request.headers.get("Content-Type") or ""):
        return PlainTextResponse("Content-Type must be application/json", status_code=415)

    body = await request.json()
    activity: Activity = Activity().deserialize(body)
    auth_header = request.headers.get("Authorization", "")

    # Diag mínimo
    recipient_raw = getattr(activity.recipient, "id", "")
    channel_id = getattr(activity, "channel_id", "")
    service_url = getattr(activity, "service_url", "")

    normalized = recipient_raw
    if channel_id == "msteams" and recipient_raw.startswith("28:"):
        normalized = recipient_raw.split("28:")[-1]

    log.info(
        "[DIAG] Our APP_ID=%s | activity.recipient.id(raw)=%s | channel=%s | serviceUrl=%s",
        APP_ID,
        recipient_raw,
        channel_id,
        service_url,
    )
    if channel_id == "msteams":
        log.info("[DIAG][msteams] normalized=%s", normalized)

    # Muy importante: confiar el serviceUrl antes de responder
    try:
        if service_url:
            MicrosoftAppCredentials.trust_service_url(service_url)
    except Exception as e:
        log.warning("trust_service_url(%s) fallo: %s", service_url, e)

    async def aux(turn_context: TurnContext):
        try:
            await bot.on_turn(turn_context)
        except Exception as ex:
            log.error("[BOT ERROR] %s", ex, exc_info=True)
            raise

    await adapter.process_activity(activity, auth_header, aux)
    return Response(status_code=201)
