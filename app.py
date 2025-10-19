# app.py — Teams Gateway (aiohttp + CloudAdapter, SDK 4.15.x)

import os
import json
import logging
from typing import Optional

from aiohttp import web

# --- Bot Framework SDK (4.15.x) ---
from botbuilder.core import CloudAdapter, TurnContext, MessageFactory
from botbuilder.integration.aiohttp import ConfigurationBotFrameworkAuthentication
from botbuilder.schema import Activity
from botframework.connector.auth import MicrosoftAppCredentials

# --- Tu bot (debe exponer on_turn / on_message_activity) ---
from bot import DataTalkBot

# =========================
# Logging
# =========================
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
    Lee primero MAYÚSCULAS; si no existe, intenta camelCase (compat Render/Azure).
    """
    camel_map = {
        "MICROSOFT_APP_ID": "MicrosoftAppId",
        "MICROSOFT_APP_PASSWORD": "MicrosoftAppPassword",
        "MICROSOFT_APP_TENANT_ID": "MicrosoftAppTenantId",
        "MICROSOFT_APP_TYPE": "MicrosoftAppType",
        "TO_CHANNEL_FROM_BOT_OAUTH_SCOPE": "ToChannelFromBotOAuthScope",
        "APPLICATIONINSIGHTS_CONNECTION_STRING": "APPLICATIONINSIGHTS_CONNECTION_STRING",
    }
    return os.getenv(name, os.getenv(camel_map.get(name, ""), fallback))


def public_env_snapshot() -> dict:
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
        v = _env(k)
        out[k] = "SET(***masked***)" if v else "MISSING"
    out["EFFECTIVE_APP_ID"] = _env("MICROSOFT_APP_ID", "")
    out["EFFECTIVE_TENANT"] = _env("MICROSOFT_APP_TENANT_ID", "")
    out["EFFECTIVE_APP_TYPE"] = _env("MICROSOFT_APP_TYPE", "")
    return out


# =========================
# CloudAdapter + Auth (explícito)
# =========================
APP_ID = _env("MICROSOFT_APP_ID", "")
APP_PASSWORD = _env("MICROSOFT_APP_PASSWORD", "")
TENANT_ID = _env("MICROSOFT_APP_TENANT_ID", "")
APP_TYPE = (_env("MICROSOFT_APP_TYPE", "") or "SingleTenant").strip()
OAUTH_SCOPE = _env("TO_CHANNEL_FROM_BOT_OAUTH_SCOPE", "") or "https://api.botframework.com"

# Construimos un objeto "config" para el auth del SDK que lea de nuestro _env
class _Cfg(dict):
    def get(self, key: str, default: Optional[str] = None):  # SDK lo usa
        m = {
            "MicrosoftAppId": APP_ID,
            "MicrosoftAppPassword": APP_PASSWORD,
            "MicrosoftAppType": APP_TYPE,
            "MicrosoftAppTenantId": TENANT_ID,
            "ToChannelFromBotOAuthScope": OAUTH_SCOPE,
        }
        return m.get(key, default)

CONFIG = _Cfg()
auth = ConfigurationBotFrameworkAuthentication(CONFIG)  # lee del _Cfg, no del env del proceso
adapter = CloudAdapter(auth)

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


# ==========================
# Handlers HTTP (aiohttp)
# ==========================
async def messages(req: web.Request) -> web.Response:
    if "application/json" not in req.headers.get("Content-Type", ""):
        return web.Response(status=415, text="Content-Type must be application/json")

    body = await req.json()
    activity: Activity = Activity().deserialize(body)
    auth_header = req.headers.get("Authorization", "")

    # Diagnóstico mínimo de entrada
    rec_raw = getattr(activity.recipient, "id", "")
    channel_id = getattr(activity, "channel_id", "")
    service_url = getattr(activity, "service_url", "")
    normalized = rec_raw.split("28:")[-1] if channel_id == "msteams" and rec_raw.startswith("28:") else rec_raw

    log.info(
        "[DIAG] Our APP_ID=%s | activity.recipient.id(raw)=%s | channel=%s | serviceUrl=%s",
        APP_ID, rec_raw, channel_id, service_url
    )
    if channel_id == "msteams":
        log.info("[DIAG][msteams] normalized=%s", normalized)

    # Confiar en el serviceUrl antes de responder (Teams exige esto)
    try:
        MicrosoftAppCredentials.trust_service_url(service_url)
    except Exception as e:
        log.warning("trust_service_url(%s) falló: %s", service_url, e)

    async def aux(turn_context: TurnContext):
        await bot.on_turn(turn_context)

    # Orden correcto (auth_header, activity, callback)
    await adapter.process_activity(auth_header, activity, aux)
    return web.Response(status=201)


async def health(_: web.Request) -> web.Response:
    return web.json_response({"ok": True})


async def diag_env(_: web.Request) -> web.Response:
    return web.json_response(public_env_snapshot())


# --- authcfg: confirma si el adapter reconoce tu AppId ---
async def diag_authcfg(_: web.Request) -> web.Response:
    """
    Usa el mismo objeto de autenticación que CloudAdapter para verificar
    si 'aud' (AppId) sería reconocido como válido.
    """
    try:
        # El SDK expone el provider internamente; hacemos una comprobación lo más parecida posible
        provider = auth._inner._credentials_factory.credential_provider  # type: ignore
        is_valid = await provider.is_valid_appid(APP_ID) if APP_ID else False
        payload = {
            "is_valid_app_id": bool(is_valid),
            "app_id_matches_env": True,
            "app_id": APP_ID,
            "app_type": APP_TYPE,
            "tenant": TENANT_ID or "(none)",
            "password_len": len(APP_PASSWORD or ""),
            "oauth_scope": OAUTH_SCOPE,
        }
        return web.json_response(payload)
    except Exception as e:
        return web.json_response({"ok": False, "exception": str(e)}, status=500)


# --- Diagnóstico simple del backend N2SQL (para descartar NLU) ---
import aiohttp
N2SQL_URL = os.getenv("N2SQL_URL", "")

async def diag_nlu(_: web.Request) -> web.Response:
    url = (N2SQL_URL or os.getenv("N2SQL_URL".upper(), "") or "").rstrip("/")
    if not url:
        return web.json_response({"ok": False, "error": "N2SQL_URL missing"}, status=500)
    try:
        async with aiohttp.ClientSession() as s:
            for path in ("/health", "/"):
                try:
                    async with s.get(f"{url}{path}", timeout=10) as r:
                        body = await r.text()
                        return web.json_response({"ok": r.status < 400, "status": r.status, "path": path, "body": body[:2000]})
                except Exception:
                    continue
        return web.json_response({"ok": False, "error": "No responde /health ni /"}, status=502)
    except Exception as e:
        return web.json_response({"ok": False, "exception": str(e)}, status=500)


# ==========
# App AIOHTTP
# ==========
app = web.Application()
app.router.add_post("/api/messages", messages)
app.router.add_get("/health", health)
app.router.add_get("/diag/env", diag_env)
app.router.add_get("/diag/authcfg", diag_authcfg)
app.router.add_get("/diag/nlu", diag_nlu)

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    web.run_app(app, host="0.0.0.0", port=port)
