# app.py — Teams Gateway (aiohttp + CloudAdapter, SDK 4.16.x)

import json
import logging
import os
from aiohttp import web

from botbuilder.core import TurnContext
from botbuilder.integration.aiohttp.cloud_adapter import CloudAdapter
from botbuilder.integration.aiohttp.configuration_bot_framework_authentication import (
    ConfigurationBotFrameworkAuthentication,
)
from botbuilder.schema import Activity
from botframework.connector.auth import MicrosoftAppCredentials

import msal
import jwt  # PyJWT para diagnosticar los claims entrantes

# ----------------------
# Tu bot (debe tener .on_turn)
# ----------------------
from bot import DataTalkBot

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
    Lee MAYÚSCULAS; si no existe, intenta camelCase (compat Render). Si no, usa fallback.
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

def public_env_snapshot() -> dict:
    keys = [
        "MICROSOFT_APP_ID",
        "MICROSOFT_APP_PASSWORD",
        "MICROSOFT_APP_TENANT_ID",
        "MICROSOFT_APP_TYPE",
        "TO_CHANNEL_FROM_BOT_OAUTH_SCOPE",
        "APPLICATIONINSIGHTS_CONNECTION_STRING",
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
    # extras útiles
    out["EFFECTIVE_APP_ID"] = _env("MICROSOFT_APP_ID")
    out["EFFECTIVE_TENANT"] = _env("MICROSOFT_APP_TENANT_ID")
    out["EFFECTIVE_APP_TYPE"] = _env("MICROSOFT_APP_TYPE", "MultiTenant") or "MultiTenant"
    return out

APP_ID = _env("MICROSOFT_APP_ID", "")
APP_PASSWORD = _env("MICROSOFT_APP_PASSWORD", "")
TENANT = _env("MICROSOFT_APP_TENANT_ID")  # vacío si MultiTenant
APP_TYPE = _env("MICROSOFT_APP_TYPE", "MultiTenant") or "MultiTenant"
# Scope por defecto del canal BF
OAUTH_SCOPE = _env("TO_CHANNEL_FROM_BOT_OAUTH_SCOPE", "https://api.botframework.com")

# ----------------------
# CloudAdapter + Auth config-based
# ----------------------
class SimpleConfig:
    """
    Implementa .get(key, default) para ConfigurationBotFrameworkAuthentication.
    """
    def __init__(self):
        self._d = {
            "MicrosoftAppId": APP_ID,
            "MicrosoftAppPassword": APP_PASSWORD,
            "MicrosoftAppType": APP_TYPE,  # "SingleTenant" | "MultiTenant" | "UserAssignedMSI"
            "MicrosoftAppTenantId": TENANT or "",
            "ToChannelFromBotOAuthScope": OAUTH_SCOPE,
        }

    def get(self, key, default=None):
        return self._d.get(key, default)

auth = ConfigurationBotFrameworkAuthentication(SimpleConfig())
adapter = CloudAdapter(auth)

# Instancia del bot
bot = DataTalkBot()

# ==========================
# Manejo global de errores
# ==========================
async def on_error(context: TurnContext, error: Exception):
    log.error("[BOT ERROR] %s", error, exc_info=True)
    try:
        await context.send_activity("Ocurrió un error procesando tu mensaje. Estamos corrigiéndolo.")
    except Exception as e:
        log.error("[BOT ERROR][send_activity] %s", e, exc_info=True)

adapter.on_turn_error = on_error

# ==========
# Utils diag
# ==========
def _peek_jwt(auth_header: str) -> dict:
    """
    Decodifica el JWT del header (sin validar firma) para ver claims básicos.
    """
    try:
        if not auth_header or not auth_header.lower().startswith("bearer "):
            return {}
        token = auth_header.split(" ", 1)[1]
        claims = jwt.decode(token, options={"verify_signature": False, "verify_exp": False})
        return {
            "iss": claims.get("iss"),
            "aud": claims.get("aud"),
            "appid": claims.get("appid") or claims.get("azp"),
            "tid": claims.get("tid"),
            "ver": claims.get("ver"),
        }
    except Exception:
        return {}

# ==========
# Handlers
# ==========
async def messages(req: web.Request) -> web.Response:
    if "application/json" not in req.headers.get("Content-Type", ""):
        return web.Response(status=415, text="Content-Type must be application/json")

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
        APP_ID,
        recipient_raw,
        channel_id,
        service_url,
    )
    if channel_id == "msteams":
        log.info("[DIAG][msteams] normalized=%s", normalized)

    # Ver los claims que llegan del canal (ayuda a diagnosticar Invalid AppId)
    claims = _peek_jwt(auth_header)
    if claims:
        log.info("[JWT] iss=%s | aud=%s | appid=%s | tid=%s | ver=%s",
                 claims.get("iss"), claims.get("aud"), claims.get("appid"),
                 claims.get("tid"), claims.get("ver"))

    # Confiar el serviceUrl para poder responder
    try:
        MicrosoftAppCredentials.trust_service_url(service_url)
    except Exception as e:
        log.warning("trust_service_url error: %s", e)

    async def aux(turn_context: TurnContext):
        try:
            await bot.on_turn(turn_context)
        except Exception:
            raise  # que lo capture on_turn_error

    # Orden correcto (auth_header, activity, callback)
    await adapter.process_activity(auth_header, activity, aux)
    return web.Response(status=201)

async def health(_: web.Request) -> web.Response:
    return web.json_response({"ok": True})

async def diag_env(_: web.Request) -> web.Response:
    return web.json_response(public_env_snapshot())

SCOPE = ["https://api.botframework.com/.default"]
AUTH_TENANT = f"https://login.microsoftonline.com/{TENANT or 'organizations'}"
AUTH_BF = "https://login.microsoftonline.com/botframework.com"

async def diag_msal(_: web.Request) -> web.Response:
    log.info("Initializing with Entra authority: %s", AUTH_TENANT)
    try:
        appc = msal.ConfidentialClientApplication(
            client_id=APP_ID, client_credential=APP_PASSWORD, authority=AUTH_TENANT
        )
        token = appc.acquire_token_for_client(scopes=SCOPE)
        ok = "access_token" in token
        payload = {"ok": ok, "keys": list(token.keys()), "authority": AUTH_TENANT}
        if not ok:
            payload["error"] = token
        return web.json_response(payload, status=200 if ok else 500)
    except Exception as e:
        return web.json_response({"ok": False, "exception": str(e)}, status=500)

async def diag_msal_bf(_: web.Request) -> web.Response:
    log.info("Initializing with Entra authority: %s", AUTH_BF)
    try:
        appc = msal.ConfidentialClientApplication(
            client_id=APP_ID, client_credential=APP_PASSWORD, authority=AUTH_BF
        )
        token = appc.acquire_token_for_client(scopes=SCOPE)
        ok = "access_token" in token
        payload = {"ok": ok, "keys": list(token.keys()), "authority": AUTH_BF}
        if not ok:
            payload["error"] = token
        return web.json_response(payload, status=200 if ok else 500)
    except Exception as e:
        return web.json_response({"ok": False, "exception": str(e)}, status=500)

# ==========
# App AIOHTTP
# ==========
app = web.Application()
app.router.add_post("/api/messages", messages)
app.router.add_get("/health", health)
app.router.add_get("/", lambda _: web.Response(status=404, text="ok"))
app.router.add_get("/diag/env", diag_env)
app.router.add_get("/diag/msal", diag_msal)
app.router.add_get("/diag/msal-bf", diag_msal_bf)

# ==========
# Main
# ==========
if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))  # Render inyecta PORT (10000). En local usa 8000.
    web.run_app(app, host="0.0.0.0", port=port)
