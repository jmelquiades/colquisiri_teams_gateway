# app.py — Teams Gateway (aiohttp + BotFrameworkAdapter, SDK 4.14.7)
import json
import logging
import os
from typing import Dict, Any

from aiohttp import web

from botbuilder.core import (
    TurnContext,
    BotFrameworkAdapter,
    BotFrameworkAdapterSettings,
)
from botbuilder.schema import Activity
from botframework.connector.auth import MicrosoftAppCredentials

import jwt  # PyJWT, solo para inspección sin validar firma
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
        "APPLICATIONINSIGHTS_CONNECTION_STRING",
        "PORT",
    ]
    out = {}
    for k in keys:
        v = _env(k)
        out[k] = "SET(***masked***)" if v else "MISSING"
    # Diagnóstico efectivo que usa esta app
    out["EFFECTIVE_APP_ID"] = APP_ID
    out["EFFECTIVE_TENANT"] = TENANT or ""
    out["EFFECTIVE_APP_TYPE"] = APP_TYPE or ""
    return out


# =====================================
# Credenciales (AppId / Password AAD)
# =====================================
APP_ID = _env("MICROSOFT_APP_ID", "")
APP_PASSWORD = _env("MICROSOFT_APP_PASSWORD", "")
TENANT = _env("MICROSOFT_APP_TENANT_ID", "")  # si estás en single tenant, llénalo
APP_TYPE = _env("MICROSOFT_APP_TYPE", "")     # "SingleTenant" o "MultiTenant" (no obligatorio aquí)

# BotFrameworkAdapter (estable con SDK 4.14.x)
adapter = BotFrameworkAdapter(BotFrameworkAdapterSettings(APP_ID, APP_PASSWORD))

# Instancia del bot
bot = DataTalkBot()


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
# Utils
# ==========
def _peek_jwt(auth_header: str) -> Dict[str, Any]:
    """
    Devuelve claims del JWT sin validar firma (solo diagnóstico).
    """
    if not auth_header or not auth_header.lower().startswith("bearer "):
        return {}
    token = auth_header.split(" ", 1)[1].strip()
    try:
        claims = jwt.decode(token, options={"verify_signature": False, "verify_aud": False})
        # Log compacto clave para soporte:
        log.info(
            "[JWT] iss=%s | aud=%s | azp=%s | appid=%s | tid=%s | ver=%s",
            claims.get("iss"),
            claims.get("aud"),
            claims.get("azp"),
            claims.get("appid"),
            claims.get("tid"),
            claims.get("ver"),
        )
        return claims
    except Exception as e:
        log.info("[JWT] decode_error=%s", e)
        return {"decode_error": str(e)}


# ==========
# Handlers
# ==========
async def messages(req: web.Request) -> web.Response:
    # Acepta "application/json" y variantes con charset
    if "application/json" not in req.headers.get("Content-Type", ""):
        return web.Response(status=415, text="Content-Type must be application/json")

    body = await req.json()
    activity: Activity = Activity().deserialize(body)
    auth_header = req.headers.get("Authorization", "")

    # ---- Diagnóstico útil en cada llegada ----
    recipient_raw = getattr(activity.recipient, "id", "")
    channel_id = getattr(activity, "channel_id", "")
    service_url = getattr(activity, "service_url", "")

    # Normalización para Teams: en Teams el recipient suele ser "28:{appId}"
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

    # Log de claims entrantes (ayuda a identificar por qué valida o no)
    _ = _peek_jwt(auth_header)

    # Comprobación de mismatch (evita que un bot se intente usar con otro AppId)
    target = normalized if channel_id in ("msteams", "skype") else recipient_raw
    if target and APP_ID and target != APP_ID:
        log.error(
            "[MISMATCH] Mensaje para botId=%s, pero proceso firma como=%s. Revisa AppId/secret/manifest.",
            target,
            APP_ID,
        )

    # Confiar en el serviceUrl antes de enviar respuestas
    try:
        if service_url:
            MicrosoftAppCredentials.trust_service_url(service_url)
    except Exception as e:
        log.warning("No se pudo registrar trust_service_url(%s): %s", service_url, e)

    async def aux_func(turn_context: TurnContext):
        try:
            await bot.on_turn(turn_context)
        except Exception as ex:
            log.error("[BOT ERROR] %s", ex, exc_info=True)
            raise

    # Orden correcto (activity, auth_header, callback)
    await adapter.process_activity(activity, auth_header, aux_func)
    return web.Response(status=201)


async def health(_: web.Request) -> web.Response:
    return web.json_response({"ok": True})


async def diag_env(_: web.Request) -> web.Response:
    snap = public_env_snapshot()
    return web.json_response(snap)


SCOPE = ["https://api.botframework.com/.default"]
def _authority_for(tenant: str) -> str:
    return f"https://login.microsoftonline.com/{tenant or 'organizations'}"


async def diag_msal(_: web.Request) -> web.Response:
    # Token contra TU tenant (útil para comprobar secreto)
    authority = _authority_for(TENANT)
    log.info("Initializing with Entra authority: %s", authority)
    try:
        appc = msal.ConfidentialClientApplication(
            client_id=APP_ID,
            client_credential=APP_PASSWORD,
            authority=authority,
        )
        token = appc.acquire_token_for_client(scopes=SCOPE)
        ok = "access_token" in token
        payload = {"ok": ok, "keys": list(token.keys()), "authority": authority}
        if not ok:
            payload["error"] = token
        return web.json_response(payload, status=200 if ok else 500)
    except Exception as e:
        return web.json_response({"ok": False, "exception": str(e)}, status=500)


async def diag_msal_bf(_: web.Request) -> web.Response:
    # Token contra botframework.com (el que usa el conector saliente)
    authority = "https://login.microsoftonline.com/botframework.com"
    log.info("Initializing with Entra authority: %s", authority)
    try:
        appc = msal.ConfidentialClientApplication(
            client_id=APP_ID,
            client_credential=APP_PASSWORD,
            authority=authority,
        )
        token = appc.acquire_token_for_client(scopes=SCOPE)
        ok = "access_token" in token
        payload = {"ok": ok, "keys": list(token.keys()), "authority": authority}
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
