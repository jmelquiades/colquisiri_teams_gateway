# app.py — Teams Gateway (aiohttp + CloudAdapter, SDK 4.14.x)

import logging
import os
from aiohttp import web

from botbuilder.core import TurnContext
from botbuilder.schema import Activity
from botframework.connector.auth import MicrosoftAppCredentials

# CloudAdapter y Auth (aiohttp)
from botbuilder.integration.aiohttp import CloudAdapter
from botbuilder.integration.aiohttp.configuration_bot_framework_authentication import (
    ConfigurationBotFrameworkAuthentication,
)

import msal
from bot import DataTalkBot  # tu bot con .on_turn

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(levelname)s:%(name)s:%(message)s",
)
log = logging.getLogger("teams-gateway")

# =========================
# Helpers de configuración
# =========================
def _force_env_aliases():
    """
    Forzamos que las variables en MAYÚSCULAS sean la fuente de verdad y
    sobre-escriban las camelCase que lee el adapter. Así evitamos mezcla
    de valores antiguos.
    """
    pairs = [
        ("MICROSOFT_APP_ID", "MicrosoftAppId"),
        ("MICROSOFT_APP_PASSWORD", "MicrosoftAppPassword"),
        ("MICROSOFT_APP_TENANT_ID", "MicrosoftAppTenantId"),
        ("MICROSOFT_APP_TYPE", "MicrosoftAppType"),
    ]
    for upper, camel in pairs:
        v = os.getenv(upper)
        if v:
            os.environ[camel] = v  # pisamos SIEMPRE la camelCase
    # Scope correcto hacia el Connector
    os.environ.setdefault("ToChannelFromBotOAuthScope", "https://api.botframework.com/.default")
    # Recomendación para Teams
    os.environ.setdefault("MicrosoftAppType", "MultiTenant")

_force_env_aliases()

APP_ID = os.getenv("MicrosoftAppId") or os.getenv("MICROSOFT_APP_ID", "")
bot = DataTalkBot()

# ==========================
# Adapter (CloudAdapter) + Auth
# ==========================
# ConfigurationBotFrameworkAuthentication leerá de os.environ (camelCase ya forzadas)
class EnvConfiguration:
    def __init__(self, env): self._env = env
    def get(self, key: str, default=None): return self._env.get(key, default)

config = EnvConfiguration(os.environ)
auth = ConfigurationBotFrameworkAuthentication(config)
adapter = CloudAdapter(auth)

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

    recipient_raw = getattr(activity.recipient, "id", "")
    channel_id = getattr(activity, "channel_id", "")
    service_url = getattr(activity, "service_url", "")

    normalized = recipient_raw
    if channel_id == "msteams" and isinstance(recipient_raw, str) and recipient_raw.startswith("28:"):
        normalized = recipient_raw.split("28:")[-1]

    log.info(
        "[DIAG] Our APP_ID=%s | activity.recipient.id(raw)=%s | channel=%s | serviceUrl=%s",
        APP_ID, recipient_raw, channel_id, service_url,
    )
    if channel_id == "msteams":
        log.info("[DIAG][msteams] normalized=%s", normalized)

    # Si el Teams AppId (recipient) no coincide con nuestro APP_ID → avisa
    target = normalized if channel_id in ("msteams", "skype") else recipient_raw
    if target and APP_ID and target != APP_ID:
        log.error("[MISMATCH] Mensaje para botId=%s, pero proceso firma como=%s.", target, APP_ID)

    # Confiar serviceUrl (por si acaso)
    try:
        MicrosoftAppCredentials.trust_service_url(service_url)
    except Exception as e:
        log.warning("No se pudo registrar trust_service_url(%s): %s", service_url, e)

    async def aux_func(turn_context: TurnContext):
        await bot.on_turn(turn_context)

    await adapter.process_activity(auth_header, activity, aux_func)
    return web.Response(status=201)

async def health(_: web.Request) -> web.Response:
    return web.json_response({"ok": True})

def public_env_snapshot() -> dict:
    keys = [
        "MICROSOFT_APP_ID", "MICROSOFT_APP_PASSWORD", "MICROSOFT_APP_TENANT_ID", "MICROSOFT_APP_TYPE",
        "MicrosoftAppId", "MicrosoftAppPassword", "MicrosoftAppTenantId", "MicrosoftAppType",
        "ToChannelFromBotOAuthScope", "PORT",
    ]
    out = {}
    for k in keys:
        out[k] = "SET(***masked***)" if os.getenv(k) else "MISSING"
    # imprime cuál cree el proceso que es su AppId efectivo
    out["EFFECTIVE_APP_ID"] = APP_ID or "(empty)"
    return out

async def diag_env(_: web.Request) -> web.Response:
    return web.json_response(public_env_snapshot())

# --- Diagnóstico de token con MSAL ---
TENANT = os.getenv("MICROSOFT_APP_TENANT_ID") or os.getenv("MicrosoftAppTenantId") or "organizations"
AUTH_TENANT = f"https://login.microsoftonline.com/{TENANT}"
AUTH_BF = "https://login.microsoftonline.com/botframework.com"
SCOPE = ["https://api.botframework.com/.default"]

async def diag_msal(_: web.Request) -> web.Response:
    log.info("Initializing with Entra authority: %s", AUTH_TENANT)
    try:
        appc = msal.ConfidentialClientApplication(
            client_id=APP_ID,
            client_credential=os.getenv("MicrosoftAppPassword") or os.getenv("MICROSOFT_APP_PASSWORD", ""),
            authority=AUTH_TENANT,
        )
        token = appc.acquire_token_for_client(scopes=SCOPE)
        ok = "access_token" in token
        payload = {"ok": ok, "keys": list(token.keys()), "authority": AUTH_TENANT}
        if not ok: payload["error"] = token
        return web.json_response(payload, status=200 if ok else 500)
    except Exception as e:
        return web.json_response({"ok": False, "exception": str(e)}, status=500)

async def diag_msal_bf(_: web.Request) -> web.Response:
    log.info("Initializing with Entra authority: %s", AUTH_BF)
    try:
        appc = msal.ConfidentialClientApplication(
            client_id=APP_ID,
            client_credential=os.getenv("MicrosoftAppPassword") or os.getenv("MICROSOFT_APP_PASSWORD", ""),
            authority=AUTH_BF,
        )
        token = appc.acquire_token_for_client(scopes=SCOPE)
        ok = "access_token" in token
        payload = {"ok": ok, "keys": list(token.keys()), "authority": AUTH_BF}
        if not ok: payload["error"] = token
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
