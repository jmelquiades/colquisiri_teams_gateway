# app.py — Teams Gateway con CloudAdapter (aiohttp, SDK 4.17.x) + Diagnóstico y App Insights
import os
import json
import base64
import logging
from aiohttp import web

from botbuilder.core import TurnContext, MessageFactory, TelemetryLoggerMiddleware
from botbuilder.schema import Activity
from botbuilder.integration.aiohttp.cloud_adapter import CloudAdapter
from botbuilder.integration.aiohttp.configuration_bot_framework_authentication import (
    ConfigurationBotFrameworkAuthentication,
)

# Telemetría (Application Insights)
# Docs: ApplicationInsightsTelemetryClient + TelemetryLoggerMiddleware
# https://learn.microsoft.com/python/api/botbuilder-applicationinsights
from botbuilder.applicationinsights import ApplicationInsightsTelemetryClient, bot_telemetry_processor

import msal

# Tu bot
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
def _get_env(name: str, fallback: str = "") -> str:
    # Acepta MAYÚSCULAS y camelCase (compat Render)
    return os.getenv(name, os.getenv({
        "MICROSOFT_APP_ID": "MicrosoftAppId",
        "MICROSOFT_APP_PASSWORD": "MicrosoftAppPassword",
        "MICROSOFT_APP_TENANT_ID": "MicrosoftAppTenantId",
        "MICROSOFT_APP_TYPE": "MicrosoftAppType",
        "TO_CHANNEL_SCOPE": "ToChannelFromBotOAuthScope",
        "APPLICATIONINSIGHTS_CONNECTION_STRING": "APPLICATIONINSIGHTS_CONNECTION_STRING",
    }.get(name, ""), fallback))


def public_env_snapshot() -> dict:
    keys = [
        "MICROSOFT_APP_ID", "MICROSOFT_APP_PASSWORD", "MICROSOFT_APP_TENANT_ID", "MICROSOFT_APP_TYPE",
        "MicrosoftAppId", "MicrosoftAppPassword", "MicrosoftAppTenantId", "MicrosoftAppType",
        "ToChannelFromBotOAuthScope", "APPLICATIONINSIGHTS_CONNECTION_STRING", "PORT",
    ]
    out = {}
    for k in keys:
        v = _get_env(k)
        out[k] = "SET(***masked***)" if v else "MISSING"
    out["EFFECTIVE_APP_ID"] = _get_env("MICROSOFT_APP_ID")
    out["EFFECTIVE_TENANT"] = _get_env("MICROSOFT_APP_TENANT_ID")
    out["EFFECTIVE_APP_TYPE"] = _get_env("MICROSOFT_APP_TYPE", "SingleTenant")
    return out


# ==========================
# CloudAdapter + Auth
# ==========================
APP_ID = _get_env("MICROSOFT_APP_ID")
APP_PASSWORD = _get_env("MICROSOFT_APP_PASSWORD")
APP_TENANT = _get_env("MICROSOFT_APP_TENANT_ID")   # REQUERIDO si SingleTenant
APP_TYPE = _get_env("MICROSOFT_APP_TYPE", "SingleTenant")
TO_BF_SCOPE = _get_env("TO_CHANNEL_SCOPE", "https://api.botframework.com/.default")

config = {
    "MicrosoftAppId": APP_ID,
    "MicrosoftAppPassword": APP_PASSWORD,
    "MicrosoftAppTenantId": APP_TENANT,
    "MicrosoftAppType": APP_TYPE,  # SingleTenant | MultiTenant | UserAssignedMSI
    "ToChannelFromBotOAuthScope": TO_BF_SCOPE,
}

auth = ConfigurationBotFrameworkAuthentication(configuration=config)
adapter = CloudAdapter(auth)
bot = DataTalkBot()

# ==========
# Telemetría opcional a App Insights
# ==========
AI_CONN = _get_env("APPLICATIONINSIGHTS_CONNECTION_STRING")
if AI_CONN:
    try:
        ai_client = ApplicationInsightsTelemetryClient(connection_string=AI_CONN, telemetry_processor=bot_telemetry_processor)
        # Loguea actividades entrantes/salientes sin PII
        adapter.use(TelemetryLoggerMiddleware(ai_client, log_personal_information=False))
        log.info("[AI] Application Insights habilitado")
    except Exception as e:
        log.warning("[AI] No se pudo inicializar App Insights: %s", e)


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
# Handlers
# ==========
async def messages(req: web.Request) -> web.Response:
    if "application/json" not in req.headers.get("Content-Type", ""):
        return web.Response(status=415, text="Content-Type must be application/json")

    body = await req.json()
    activity: Activity = Activity().deserialize(body)
    auth_header = req.headers.get("Authorization", "")

    # ---- Diagnóstico útil
    recipient_raw = getattr(activity.recipient, "id", "")
    channel_id = getattr(activity, "channel_id", "")
    service_url = getattr(activity, "service_url", "")

    normalized = recipient_raw
    if channel_id == "msteams" and recipient_raw.startswith("28:"):
        normalized = recipient_raw.split("28:")[-1]

    log.info("[DIAG] Our APP_ID=%s | activity.recipient.id(raw)=%s | channel=%s | serviceUrl=%s",
             APP_ID, recipient_raw, channel_id, service_url)
    if channel_id == "msteams":
        log.info("[DIAG][msteams] normalized=%s", normalized)

    # ---- Dump mínimo de claims del JWT entrante
    if auth_header.startswith("Bearer "):
        try:
            token = auth_header.split(" ", 1)[1]
            parts = token.split(".")
            if len(parts) == 3:
                padded = parts[1] + "=="
                payload = json.loads(base64.urlsafe_b64decode(padded.encode("utf-8")))
                iss = payload.get("iss")
                aud = payload.get("aud")
                appid_claim = payload.get("appid") or payload.get("azp")
                tid = payload.get("tid")
                ver = payload.get("ver")
                log.info("[JWT] iss=%s | aud=%s | appid=%s | tid=%s | ver=%s", iss, aud, appid_claim, tid, ver)
        except Exception as e:
            log.warning("[JWT] No se pudieron decodificar claims: %s", e)

    async def aux(turn_context: TurnContext):
        await bot.on_turn(turn_context)

    # Orden CloudAdapter: (auth_header, activity, callback)
    await adapter.process_activity(auth_header, activity, aux)
    return web.Response(status=201)


async def health(_: web.Request) -> web.Response:
    return web.json_response({"ok": True})


async def diag_env(_: web.Request) -> web.Response:
    return web.json_response(public_env_snapshot())


# --- Diagnóstico de token MSAL (para validar secreto) ---
SCOPE = ["https://api.botframework.com/.default"]
TENANT_FOR_TEST = _get_env("MICROSOFT_APP_TENANT_ID") or "organizations"
AUTH_TENANT = f"https://login.microsoftonline.com/{TENANT_FOR_TEST}"
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


# --- Diagnóstico clave: ¿el adapter considera válido tu AppId? ---
# Esto detecta de inmediato si el password está vacío/typo o si el factory no carga la config.
from botbuilder.integration.aiohttp.configuration_service_client_credential_factory import \
    ConfigurationServiceClientCredentialFactory

async def diag_authcfg(_: web.Request) -> web.Response:
    try:
        factory = ConfigurationServiceClientCredentialFactory(configuration={
            "MicrosoftAppId": APP_ID,
            "MicrosoftAppPassword": APP_PASSWORD,
            "MicrosoftAppTenantId": APP_TENANT,
            "MicrosoftAppType": APP_TYPE,
        })
        is_valid = await factory.is_valid_app_id(APP_ID)
        pwd_len = len(APP_PASSWORD or "")
        return web.json_response({
            "is_valid_app_id": bool(is_valid),
            "app_id_matches_env": APP_ID is not None,
            "password_len": pwd_len,
            "app_type": APP_TYPE,
            "tenant": APP_TENANT or "(none)",
        })
    except Exception as e:
        return web.json_response({"ok": False, "exception": str(e)}, status=500)


# ==========
# App AIOHTTP
# ==========
app = web.Application()
app.router.add_post("/api/messages", messages)
app.router.add_get("/health", health)
app.router.add_get("/diag/env", diag_env)
app.router.add_get("/diag/msal", diag_msal)
app.router.add_get("/diag/msal-bf", diag_msal_bf)
app.router.add_get("/diag/authcfg", diag_authcfg)

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    web.run_app(app, host="0.0.0.0", port=port)
