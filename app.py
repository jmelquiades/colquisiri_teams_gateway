# app.py — Teams Gateway con CloudAdapter (SDK 4.14.x)
# Ejecuta con: python app.py
# Requiere en requirements.txt:
#   aiohttp==3.8.5
#   botbuilder-core==4.14.7
#   botbuilder-schema==4.14.7
#   botbuilder-integration-aiohttp==4.14.7
#   botframework-connector==4.14.7
#   msal>=1.28
# (opcional para /diag/token) pyjwt>=2.8  -> si no lo tienes, el endpoint se desactiva solo.

import base64
import json
import logging
import os
from typing import Any, Dict

from aiohttp import web

from botbuilder.core import (
    ActivityHandler,
    CloudAdapter,
    TurnContext,
)
from botbuilder.schema import Activity

from botbuilder.integration.aiohttp.configuration_bot_framework_authentication import (
    ConfigurationBotFrameworkAuthentication,
)
from botbuilder.integration.aiohttp.configuration_service_client_credential_factory import (
    ConfigurationServiceClientCredentialFactory,
)

from botframework.connector.auth import MicrosoftAppCredentials

import msal

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
def env(name: str, default: str = "") -> str:
    """Lee variable tal cual (camelCase). No hace alias con MAYÚSCULAS."""
    return os.getenv(name, default)


def env_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    return default if v is None else v.lower() in ("1", "true", "yes", "y")


def public_env_snapshot() -> Dict[str, str]:
    keys = [
        "MicrosoftAppId",
        "MicrosoftAppPassword",
        "MicrosoftAppTenantId",
        "MicrosoftAppType",
        "ToChannelFromBotOAuthScope",
        "ChannelService",
        "PORT",
    ]
    out = {}
    for k in keys:
        out[k] = "SET(***masked***)" if os.getenv(k) else "MISSING"
    # Muestra también si están puestas las mayúsculas (para detectar “dobles”)
    for k in [
        "MICROSOFT_APP_ID",
        "MICROSOFT_APP_PASSWORD",
        "MICROSOFT_APP_TENANT_ID",
        "MICROSOFT_APP_TYPE",
    ]:
        out[k] = "SET(***masked***)" if os.getenv(k) else "MISSING"
    # Efectivo de lo que usaremos
    out["EFFECTIVE_APP_ID"] = env("MicrosoftAppId", "")
    return out


# =====================================
# Carga de credenciales (usar SOLO camelCase)
# =====================================
APP_ID = env("MicrosoftAppId", "")
APP_PASSWORD = env("MicrosoftAppPassword", "")
APP_TYPE = env("MicrosoftAppType", "")  # MultiTenant | SingleTenant | UserAssignedMSI
TENANT_ID = env("MicrosoftAppTenantId", "")
CHANNEL_SERVICE = env("ChannelService", "")  # público: vacío
TO_CHANNEL_SCOPE = env(
    "ToChannelFromBotOAuthScope", "https://api.botframework.com/.default"
)

if not APP_ID or not APP_PASSWORD:
    log.error(
        "MicrosoftAppId/MicrosoftAppPassword faltan. Revisa variables camelCase en Render."
    )

# =====================================
# BotFrameworkAuthentication explícita
#   - Pasamos una cred factory con AppId/Password exactos
#   - Y un 'configuration' con el resto de llaves (tipo, tenant, scope…)
# =====================================
config_map: Dict[str, Any] = {
    "MicrosoftAppType": APP_TYPE,
    "MicrosoftAppTenantId": TENANT_ID,
    "ToChannelFromBotOAuthScope": TO_CHANNEL_SCOPE,
    "ChannelService": CHANNEL_SERVICE,
    # Puedes agregar "BotOpenIdMetadata" / "OAuthApiEndpoint" si usas nubes soberanas
}

cred_factory = ConfigurationServiceClientCredentialFactory(
    {
        # OBLIGATORIO: que la cred factory tenga el par que valida el incoming token
        "MicrosoftAppId": APP_ID,
        "MicrosoftAppPassword": APP_PASSWORD,
        # (Opc) Si usas certificado en vez de secreto, aquí irían sus claves
    }
)

auth = ConfigurationBotFrameworkAuthentication(
    configuration=config_map,
    credentials_factory=cred_factory,
)

adapter = CloudAdapter(auth)

# Instancia del bot
bot: ActivityHandler = DataTalkBot()


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

    try:
        MicrosoftAppCredentials.trust_service_url(service_url)
    except Exception as e:
        log.warning("No se pudo registrar trust_service_url(%s): %s", service_url, e)

    async def aux(turn_context: TurnContext):
        await bot.on_turn(turn_context)

    # OJO: en CloudAdapter el orden es (auth_header, activity, callback)
    await adapter.process_activity(auth_header, activity, aux)
    return web.Response(status=201)


async def health(_: web.Request) -> web.Response:
    return web.json_response({"ok": True})


async def diag_env(_: web.Request) -> web.Response:
    return web.json_response(public_env_snapshot())


# --- Diagnóstico de token con MSAL (para validar secreto AAD) ---
TENANT = TENANT_ID or "organizations"
AUTH_TENANT = f"https://login.microsoftonline.com/{TENANT}"
AUTH_BF = "https://login.microsoftonline.com/botframework.com"
SCOPE = ["https://api.botframework.com/.default"]


async def diag_msal(_: web.Request) -> web.Response:
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
        return web.json_response(payload, status=200 if ok else 500)
    except Exception as e:
        return web.json_response({"ok": False, "exception": str(e)}, status=500)


async def diag_msal_bf(_: web.Request) -> web.Response:
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
        return web.json_response(payload, status=200 if ok else 500)
    except Exception as e:
        return web.json_response({"ok": False, "exception": str(e)}, status=500)


# --- (Opcional) Diagnóstico de Authorization: extrae appid/aud del JWT entrante ---
try:
    import jwt as pyjwt  # pyjwt

    async def diag_token(req: web.Request) -> web.Response:
        authz = req.headers.get("Authorization", "")
        if not authz.startswith("Bearer "):
            return web.json_response({"ok": False, "msg": "Sin header Bearer"}, status=400)
        token = authz.split(" ", 1)[1]
        # decode sin validar firma: solo para inspección
        # opciones para no verificar exp/iss/aud
        payload = pyjwt.decode(token, options={"verify_signature": False})
        return web.json_response({"ok": True, "claims": payload})
except Exception:
    async def diag_token(_: web.Request) -> web.Response:
        return web.json_response(
            {"ok": False, "msg": "pyjwt no instalado, omitiendo /diag/token"}, status=501
        )


# ==========
# App AIOHTTP
# ==========
app = web.Application()
app.router.add_post("/api/messages", messages)
app.router.add_get("/health", health)
app.router.add_get("/diag/env", diag_env)
app.router.add_get("/diag/msal", diag_msal)
app.router.add_get("/diag/msal-bf", diag_msal_bf)
app.router.add_get("/diag/token", diag_token)

# ==========
# Main
# ==========
if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    web.run_app(app, host="0.0.0.0", port=port)
