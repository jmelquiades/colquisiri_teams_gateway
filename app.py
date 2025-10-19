# app.py — Teams Gateway (aiohttp + CloudAdapter, SDK 4.14.x)

import json
import logging
import os
from aiohttp import web

from botbuilder.core import CloudAdapter, TurnContext
from botbuilder.schema import Activity
from botframework.connector.auth import (
    ConfigurationBotFrameworkAuthentication,
    ConfigurationServiceClientCredentialFactory,
)
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
_CAMEL_BY_UPPER = {
    "MICROSOFT_APP_ID": "MicrosoftAppId",
    "MICROSOFT_APP_PASSWORD": "MicrosoftAppPassword",
    "MICROSOFT_APP_TENANT_ID": "MicrosoftAppTenantId",
    "MICROSOFT_APP_TYPE": "MicrosoftAppType",
    "TOCHANNELFROMBOTOAUTHSCOPE": "ToChannelFromBotOAuthScope",
    "TOCHANNELFROMBOTOAUTHSCOPE": "ToChannelFromBotOAuthScope",
}

def _env(name: str, fallback: str = "") -> str:
    """
    Lee primero MAYÚSCULAS; si no existe, intenta camelCase (compat Render).
    """
    if name in os.environ and os.environ.get(name):
        return os.environ.get(name, fallback)
    # compat: aceptar camelCase si el upper no está
    alt = _CAMEL_BY_UPPER.get(name, "")
    if alt and os.environ.get(alt):
        return os.environ.get(alt, fallback)
    return fallback


def public_env_snapshot() -> dict:
    """
    Muestra si variables críticas están SET/MISSING (sin valores).
    Útil para /diag/env.
    """
    keys = [
        "MICROSOFT_APP_ID",
        "MICROSOFT_APP_PASSWORD",
        "MICROSOFT_APP_TENANT_ID",
        "MICROSOFT_APP_TYPE",
        "MicrosoftAppId",
        "MicrosoftAppPassword",
        "MicrosoftAppTenantId",
        "MicrosoftAppType",
        "ToChannelFromBotOAuthScope",
        "PORT",
    ]
    out = {}
    for k in keys:
        v = os.getenv(k)
        out[k] = "SET(***masked***)" if v else "MISSING"
    # por conveniencia, qué AppId estamos usando efectivamente
    out["EFFECTIVE_APP_ID"] = _env("MICROSOFT_APP_ID") or _env("MicrosoftAppId") or ""
    return out


# =====================================
# Credenciales (AppId / Password AAD)
# =====================================
APP_ID = _env("MICROSOFT_APP_ID")
APP_PASSWORD = _env("MICROSOFT_APP_PASSWORD")
APP_TYPE = _env("MICROSOFT_APP_TYPE", "MultiTenant")  # si tu bot es single-tenant, usa "SingleTenant"
TENANT_ID = _env("MICROSOFT_APP_TENANT_ID") or None
OAUTH_SCOPE = os.getenv("ToChannelFromBotOAuthScope", "https://api.botframework.com")

# Factory de credenciales explícita (evita que se “pierda” el AppId)
cred_factory = ConfigurationServiceClientCredentialFactory(
    app_id=APP_ID or None,
    password=APP_PASSWORD or None,
    tenant_id=TENANT_ID,
    app_type=APP_TYPE,
)

# ConfigurationBotFrameworkAuthentication requiere 'configuration' posicional; pasamos {}.
auth = ConfigurationBotFrameworkAuthentication(configuration={}, credentials_factory=cred_factory)

# Adapter moderno
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
# Handlers
# ==========
async def messages(req: web.Request) -> web.Response:
    # Requerimos application/json (admitimos charset)
    ctype = req.headers.get("Content-Type", "")
    if "application/json" not in ctype:
        return web.Response(status=415, text="Content-Type must be application/json")

    body = await req.json()
    activity: Activity = Activity().deserialize(body)
    auth_header = req.headers.get("Authorization", "")

    # ---- Diagnóstico útil en cada llegada ----
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
    if APP_ID and normalized and normalized != APP_ID:
        log.error("[MISMATCH] Mensaje para botId=%s, pero proceso firma como=%s. Revisa AppId/secret/manifest.", normalized, APP_ID)

    async def aux(turn_context: TurnContext):
        await bot.on_turn(turn_context)

    # CloudAdapter valida el JWT y luego ejecuta el callback
    await adapter.process_activity(auth_header, activity, aux)
    return web.Response(status=201)


async def health(_: web.Request) -> web.Response:
    return web.json_response({"ok": True})


async def diag_env(_: web.Request) -> web.Response:
    return web.json_response(public_env_snapshot())


# --- Diagnóstico de token con MSAL (para validar secreto/permiso) ---
TENANT_FOR_TEST = TENANT_ID or "organizations"
AUTH_TENANT = f"https://login.microsoftonline.com/{TENANT_FOR_TEST}"
AUTH_BF = "https://login.microsoftonline.com/botframework.com"
SCOPE = ["https://api.botframework.com/.default"]

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

# --- Diagnóstico crítico: qué AppId ve el adapter y si lo valida ---
async def diag_authcfg(_: web.Request) -> web.Response:
    try:
        is_valid = await cred_factory.is_valid_app_id(APP_ID or "")
    except Exception as e:
        is_valid = False
        log.error("cred_factory.is_valid_app_id() lanzó: %s", e, exc_info=True)

    payload = {
        "app_id_env": APP_ID or "",
        "app_id_factory": getattr(cred_factory, "app_id", None),
        "tenant_id_factory": getattr(cred_factory, "tenant_id", None),
        "app_type": APP_TYPE,
        "oauth_scope": OAUTH_SCOPE,
        "factory_considers_appid_valid": is_valid,
    }
    return web.json_response(payload)

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

# ==========
# Main
# ==========
if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    web.run_app(app, host="0.0.0.0", port=port)
