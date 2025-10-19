# app.py — Teams Gateway (aiohttp + CloudAdapter, SDK 4.14.x)

import logging
import os
from aiohttp import web

from botbuilder.core import TurnContext
from botbuilder.schema import Activity
from botframework.connector.auth import MicrosoftAppCredentials

# ✅ IMPORTS CORRECTOS para CloudAdapter y Auth (paquete integration.aiohttp)
from botbuilder.integration.aiohttp import CloudAdapter
from botbuilder.integration.aiohttp.configuration_bot_framework_authentication import (
    ConfigurationBotFrameworkAuthentication,
)
from botbuilder.integration.aiohttp.configuration_service_client_credential_factory import (
    ConfigurationServiceClientCredentialFactory,
)

import msal

# ----------------------
# Tu bot (debe tener .on_turn)
# ----------------------
from bot import DataTalkBot


# ----------------------
# Logging
# ----------------------
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(levelname)s:%(name)s:%(message)s",
)
log = logging.getLogger("teams-gateway")


# =========================
# Helpers de entorno
# =========================
def resolve_env_preferring_camel(camel_key: str, upper_key: str, default: str = "") -> str:
    """
    Toma primero CAMELCASE; si falta, usa MAYÚSCULAS; y siempre fija CAMELCASE en os.environ
    para que el adapter las encuentre sin ambigüedad.
    """
    val = os.getenv(camel_key)
    if not val:
        val = os.getenv(upper_key, default)
        if val:
            os.environ[camel_key] = val
    return os.getenv(camel_key, default)


def enforce_defaults():
    # Scope hacia el Connector (con .default)
    os.environ.setdefault("ToChannelFromBotOAuthScope", "https://api.botframework.com/.default")
    # Recomendación para pruebas (puedes dejar SingleTenant si así es tu app)
    os.environ.setdefault("MicrosoftAppType", os.getenv("MicrosoftAppType", "MultiTenant"))


APP_ID = resolve_env_preferring_camel("MicrosoftAppId", "MICROSOFT_APP_ID")
APP_PASSWORD = resolve_env_preferring_camel("MicrosoftAppPassword", "MICROSOFT_APP_PASSWORD")
TENANT_ID = resolve_env_preferring_camel("MicrosoftAppTenantId", "MICROSOFT_APP_TENANT_ID")
APP_TYPE = resolve_env_preferring_camel("MicrosoftAppType", "MICROSOFT_APP_TYPE", "MultiTenant")
enforce_defaults()

# ==========================
# Auth + Adapter (CloudAdapter)
# ==========================
# Pasamos una cred factory explícita con tu AppId/Password (evita ambigüedades).
cred_factory = ConfigurationServiceClientCredentialFactory(
    {
        "MicrosoftAppId": APP_ID,
        "MicrosoftAppPassword": APP_PASSWORD,
        # Si usas SingleTenant, también ayuda tener tenant:
        "MicrosoftAppTenantId": TENANT_ID,
        "MicrosoftAppType": APP_TYPE,
    }
)

# La "configuration" es solo algo con .get(); usamos un wrapper simple sobre os.environ
class EnvConfiguration:
    def __init__(self, env): self._env = env
    def get(self, key: str, default=None): return self._env.get(key, default)

config = EnvConfiguration(os.environ)

auth = ConfigurationBotFrameworkAuthentication(configuration=config, credentials_factory=cred_factory)
adapter = CloudAdapter(auth)

# Tu bot
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
    if "application/json" not in (req.headers.get("Content-Type") or ""):
        return web.Response(status=415, text="Content-Type must be application/json")

    body = await req.json()
    activity: Activity = Activity().deserialize(body)
    auth_header = req.headers.get("Authorization", "")

    # Diagnóstico útil
    recipient_raw = getattr(activity.recipient, "id", "")
    channel_id = getattr(activity, "channel_id", "")
    service_url = getattr(activity, "service_url", "")

    normalized = recipient_raw
    if channel_id == "msteams" and isinstance(recipient_raw, str) and recipient_raw.startswith("28:"):
        normalized = recipient_raw.split("28:")[-1]

    log.info(
        "[DIAG] Our APP_ID=%s | activity.recipient.id(raw)=%s | channel=%s | serviceUrl=%s",
        APP_ID or "(empty)", recipient_raw, channel_id, service_url,
    )
    if channel_id == "msteams":
        log.info("[DIAG][msteams] normalized=%s", normalized)

    if normalized and APP_ID and normalized != APP_ID:
        log.error("[MISMATCH] Mensaje para botId=%s, pero proceso firma como=%s.", normalized, APP_ID)

    # Confiar serviceUrl por si el Connector lo requiere
    try:
        MicrosoftAppCredentials.trust_service_url(service_url)
    except Exception as e:
        log.warning("No se pudo registrar trust_service_url(%s): %s", service_url, e)

    async def aux(turn_context: TurnContext):
        await bot.on_turn(turn_context)

    # CloudAdapter: (auth_header, activity, callback)
    await adapter.process_activity(auth_header, activity, aux)
    return web.Response(status=201)


async def health(_: web.Request) -> web.Response:
    return web.json_response({"ok": True})


def public_env_snapshot() -> dict:
    keys = [
        # camelCase que DEBE usar el adapter
        "MicrosoftAppId", "MicrosoftAppPassword", "MicrosoftAppTenantId",
        "MicrosoftAppType", "ToChannelFromBotOAuthScope", "PORT",
        # para detectar residuos en MAYÚSCULAS
        "MICROSOFT_APP_ID", "MICROSOFT_APP_PASSWORD", "MICROSOFT_APP_TENANT_ID", "MICROSOFT_APP_TYPE",
    ]
    out = {}
    for k in keys:
        out[k] = "SET(***masked***)" if os.getenv(k) else "MISSING"
    out["EFFECTIVE_APP_ID"] = APP_ID or "(empty)"
    return out


async def diag_env(_: web.Request) -> web.Response:
    return web.json_response(public_env_snapshot())


# --- Diagnóstico de token con MSAL (para validar secreto AAD) ---
TENANT_FOR_TEST = TENANT_ID or "organizations"
AUTH_TENANT = f"https://login.microsoftonline.com/{TENANT_FOR_TEST}"
AUTH_BF = "https://login.microsoftonline.com/botframework.com"
SCOPE = ["https://api.botframework.com/.default"]

async def diag_msal(_: web.Request) -> web.Response:
    log.info("Initializing with Entra authority: %s", AUTH_TENANT)
    try:
        appc = msal.ConfidentialClientApplication(
            client_id=APP_ID or "",
            client_credential=APP_PASSWORD or "",
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
            client_id=APP_ID or "",
            client_credential=APP_PASSWORD or "",
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


# ==========
# App AIOHTTP
# ==========
app = web.Application()
app.router.add_post("/api/messages", messages)
app.router.add_get("/health", health)
app.router.add_get("/diag/env", diag_env)
app.router.add_get("/diag/msal", diag_msal)
app.router.add_get("/diag/msal-bf", diag_msal_bf)

# ==========
# Main
# ==========
if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    web.run_app(app, host="0.0.0.0", port=port)
