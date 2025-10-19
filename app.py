# app.py — Teams Gateway (aiohttp + CloudAdapter, SDK 4.16.x)
import logging
import os
from typing import Dict, Any

from aiohttp import web

from botbuilder.core import CloudAdapter, TurnContext
from botbuilder.schema import Activity
from botbuilder.integration.aiohttp import ConfigurationBotFrameworkAuthentication

import jwt  # solo para inspeccionar claims sin validar firma
import msal  # para diags de token hacia BF

# Tu bot
from bot import DataTalkBot
from conectores.bf_msft_comandos import acquire_bf_token, trust_service_url

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
    Lee primero MAYÚSCULAS; si no existe, intenta camelCase (compat con Render).
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
        "ToChannelFromBotOAuthScope",
        "PORT",
    ]
    out = {}
    for k in keys:
        v = _env(k)
        out[k] = "SET(***masked***)" if v else "MISSING"

    app_id = _env("MICROSOFT_APP_ID")
    app_type = (_env("MICROSOFT_APP_TYPE") or "SingleTenant").strip()
    tenant = _env("MICROSOFT_APP_TENANT_ID")

    out["EFFECTIVE_APP_ID"] = app_id or "(none)"
    out["EFFECTIVE_TENANT"] = tenant or "(none)"
    out["EFFECTIVE_APP_TYPE"] = app_type or "(none)"
    return out


# =====================================
# Credenciales (AppId / Password / Tenant)
# =====================================
APP_ID = _env("MICROSOFT_APP_ID")
APP_PASSWORD = _env("MICROSOFT_APP_PASSWORD")
APP_TENANT = _env("MICROSOFT_APP_TENANT_ID")  # requerido si SingleTenant
APP_TYPE = _env("MICROSOFT_APP_TYPE") or "SingleTenant"
OAUTH_SCOPE = _env("TO_CHANNEL_FROM_BOT_OAUTH_SCOPE") or "https://api.botframework.com"

# Config dict explícito para ConfigurationBotFrameworkAuthentication
auth_config: Dict[str, str] = {
    "MicrosoftAppId": APP_ID or "",
    "MicrosoftAppPassword": APP_PASSWORD or "",
    "MicrosoftAppType": APP_TYPE,
    "ToChannelFromBotOAuthScope": OAUTH_SCOPE,
}
if APP_TYPE.lower().startswith("single") and APP_TENANT:
    auth_config["MicrosoftAppTenantId"] = APP_TENANT

# Adapter moderno (CloudAdapter)
auth = ConfigurationBotFrameworkAuthentication(configuration=auth_config)
adapter = CloudAdapter(auth)

# Instancia del bot
bot = DataTalkBot()

# Últimas claims vistas (para /diag/claims)
LAST_CLAIMS: Dict[str, Any] = {}


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

    # ---- Diagnóstico útil en cada llegada ----
    recipient_raw = getattr(activity.recipient, "id", "")
    channel_id = getattr(activity, "channel_id", "")
    service_url = getattr(activity, "service_url", "")

    normalized = recipient_raw
    if channel_id == "msteams" and isinstance(recipient_raw, str) and recipient_raw.startswith("28:"):
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

    # Confiar serviceUrl para respuestas salientes
    trust_service_url(service_url)

    # Inspeccionar claims del Authorization: Bearer ... (solo lectura, sin validar)
    try:
        token = (auth_header or "").split("Bearer ", 1)[-1].strip()
        if token:
            claims = jwt.decode(token, options={"verify_signature": False, "verify_aud": False})
            LAST_CLAIMS.clear()
            for k in ("iss", "aud", "appid", "azp", "tid", "ver"):
                LAST_CLAIMS[k] = claims.get(k)
            log.info(
                "[JWT] iss=%s | aud=%s | appid=%s | azp=%s | tid=%s | ver=%s",
                LAST_CLAIMS.get("iss"),
                LAST_CLAIMS.get("aud"),
                LAST_CLAIMS.get("appid"),
                LAST_CLAIMS.get("azp"),
                LAST_CLAIMS.get("tid"),
                LAST_CLAIMS.get("ver"),
            )
    except Exception as e:
        log.warning("No se pudieron leer claims JWT: %s", e)

    async def aux(turn_context: TurnContext):
        await bot.on_turn(turn_context)

    # Orden: auth_header, activity, callback
    await adapter.process_activity(auth_header, activity, aux)
    return web.Response(status=202)


async def health(_: web.Request) -> web.Response:
    return web.json_response({"ok": True})


async def diag_env(_: web.Request) -> web.Response:
    return web.json_response(public_env_snapshot())


# --- Diagnóstico de token con MSAL (para validar credenciales AAD/BF) ---
SCOPE = ["https://api.botframework.com/.default"]


async def diag_msal(_: web.Request) -> web.Response:
    # Token contra TU tenant (SingleTenant) o 'organizations' (MultiTenant)
    tenant = APP_TENANT or "organizations"
    authority = f"https://login.microsoftonline.com/{tenant}"
    try:
        appc = msal.ConfidentialClientApplication(
            client_id=APP_ID,
            client_credential=APP_PASSWORD,
            authority=authority,
        )
        token = appc.acquire_token_for_client(scopes=SCOPE)
        ok = "access_token" in token
        payload = {"ok": ok, "keys": [k for k in token.keys()], "authority": authority}
        if not ok:
            payload["error"] = token
        return web.json_response(payload, status=200 if ok else 500)
    except Exception as e:
        return web.json_response({"ok": False, "exception": str(e)}, status=500)


async def diag_bf(_: web.Request) -> web.Response:
    # Atajo con helper (igual objetivo que /diag/msal)
    tenant = APP_TENANT or "organizations"
    res = acquire_bf_token(APP_ID, APP_PASSWORD, tenant)
    return web.json_response(res, status=200 if res.get("has_access_token") else 500)


async def diag_claims(_: web.Request) -> web.Response:
    return web.json_response({"last_claims": LAST_CLAIMS or "(none)"})


# ==========
# App AIOHTTP
# ==========
app = web.Application()
app.router.add_post("/api/messages", messages)
app.router.add_get("/health", health)
app.router.add_get("/diag/env", diag_env)
app.router.add_get("/diag/msal", diag_msal)
app.router.add_get("/diag/bf", diag_bf)
app.router.add_get("/diag/claims", diag_claims)

# ==========
# Main
# ==========
if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    web.run_app(app, host="0.0.0.0", port=port)
