# app.py — Teams Gateway (aiohttp + BotFrameworkAdapter, SDK 4.14.7)
import json
import logging
import os
from typing import Dict, Any

from aiohttp import web
import requests
import jwt  # PyJWT para inspección de claims (sin validar firma)
import msal

from botbuilder.core import (
    TurnContext,
    BotFrameworkAdapter,
    BotFrameworkAdapterSettings,
)
from botbuilder.schema import Activity
from botframework.connector.auth import MicrosoftAppCredentials

# Tu bot (debe exponer on_turn)
from bot import DataTalkBot

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(levelname)s:%(name)s:%(message)s",
)
log = logging.getLogger("teams-gateway")


def _env(name: str, fallback: str = "") -> str:
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

APP_ID = _env("MICROSOFT_APP_ID", "")
APP_PASSWORD = _env("MICROSOFT_APP_PASSWORD", "")
TENANT = _env("MICROSOFT_APP_TENANT_ID", "")           # déjalo vacío si no estás 100% seguro
APP_TYPE = _env("MICROSOFT_APP_TYPE", "")              # informativo
CUSTOM_SCOPE = _env("TO_CHANNEL_FROM_BOT_OAUTH_SCOPE", "")
SCOPE = [CUSTOM_SCOPE or "https://api.botframework.com/.default"]

adapter = BotFrameworkAdapter(BotFrameworkAdapterSettings(APP_ID, APP_PASSWORD))
bot = DataTalkBot()

def public_env_snapshot() -> dict:
    keys = [
        "MICROSOFT_APP_ID","MICROSOFT_APP_PASSWORD","MICROSOFT_APP_TENANT_ID",
        "MICROSOFT_APP_TYPE","TO_CHANNEL_FROM_BOT_OAUTH_SCOPE",
        "MicrosoftAppId","MicrosoftAppPassword","MicrosoftAppTenantId","MicrosoftAppType",
        "ToChannelFromBotOAuthScope","APPLICATIONINSIGHTS_CONNECTION_STRING","PORT",
    ]
    out = {}
    for k in keys:
        v = _env(k)
        out[k] = "SET(***masked***)" if v else "MISSING"
    out["EFFECTIVE_APP_ID"] = APP_ID
    out["EFFECTIVE_TENANT"] = TENANT or ""
    out["EFFECTIVE_APP_TYPE"] = APP_TYPE or ""
    out["EFFECTIVE_SCOPE"] = SCOPE[0]
    return out

def _peek_jwt(auth_header: str) -> Dict[str, Any]:
    if not auth_header or not auth_header.lower().startswith("bearer "):
        return {}
    token = auth_header.split(" ", 1)[1].strip()
    try:
        claims = jwt.decode(token, options={"verify_signature": False, "verify_aud": False})
        log.info(
            "[JWT] iss=%s | aud=%s | azp=%s | appid=%s | tid=%s | ver=%s",
            claims.get("iss"), claims.get("aud"), claims.get("azp"),
            claims.get("appid"), claims.get("tid"), claims.get("ver"),
        )
        return claims
    except Exception as e:
        log.info("[JWT] decode_error=%s", e)
        return {"decode_error": str(e)}

def _msal_client(authority: str) -> msal.ConfidentialClientApplication:
    return msal.ConfidentialClientApplication(
        client_id=APP_ID, client_credential=APP_PASSWORD, authority=authority
    )

def _manual_reply(activity: Activity, text: str) -> Dict[str, Any]:
    su = (activity.service_url or "").rstrip("/")
    conv_id = getattr(getattr(activity, "conversation", None), "id", None)
    act_id = getattr(activity, "id", None)
    if not (su and conv_id and act_id):
        return {"ok": False, "why": "missing_parts", "serviceUrl": su, "conv": conv_id, "act": act_id}

    # Token contra botframework.com (scope correcto para el Connector)
    authority = "https://login.microsoftonline.com/botframework.com"
    token = _msal_client(authority).acquire_token_for_client(scopes=SCOPE)
    if "access_token" not in token:
        return {"ok": False, "why": "no_token", "msal_keys": list(token.keys())}

    url = f"{su}/v3/conversations/{conv_id}/activities/{act_id}"
    headers = {"Authorization": f"Bearer {token['access_token']}", "Content-Type": "application/json"}
    body = {
        "type": "message",
        "text": text,
        "from": {"id": getattr(getattr(activity, 'recipient', None), 'id', None)},
        "recipient": {"id": getattr(getattr(activity, 'from_property', None), 'id', None)},
        "replyToId": act_id,
    }
    try:
        r = requests.post(url, headers=headers, data=json.dumps(body), timeout=10)
        return {"ok": (200 <= r.status_code < 300), "status": r.status_code, "text": r.text}
    except Exception as e:
        return {"ok": False, "why": "exception", "error": str(e)}

async def on_error(context: TurnContext, error: Exception):
    log.error("[BOT ERROR] %s", error, exc_info=True)
    try:
        await context.send_activity("Ocurrió un error procesando tu mensaje. Probando fallback…")
    except Exception as e1:
        log.error("[BOT ERROR][send_activity] %s", e1, exc_info=True)
        res = _manual_reply(context.activity, "Fallback manual: recibí tu mensaje ✅")
        log.error("[FALLBACK_MANUAL][result] %s", res)

adapter.on_turn_error = on_error

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
    if channel_id == "msteams" and isinstance(recipient_raw, str) and recipient_raw.startswith("28:"):
        normalized = recipient_raw.split("28:")[-1]

    log.info("[DIAG] Our APP_ID=%s | activity.recipient.id(raw)=%s | channel=%s | serviceUrl=%s",
             APP_ID, recipient_raw, channel_id, service_url)
    if channel_id == "msteams":
        log.info("[DIAG][msteams] normalized=%s", normalized)

    _ = _peek_jwt(auth_header)

    target = normalized if channel_id in ("msteams", "skype") else recipient_raw
    if target and APP_ID and target != APP_ID:
        log.error("[MISMATCH] Mensaje para botId=%s, pero proceso firma como=%s. Revisa AppId/secret/manifest.",
                  target, APP_ID)

    try:
        if service_url:
            MicrosoftAppCredentials.trust_service_url(service_url)
    except Exception as e:
        log.warning("trust_service_url(%s) error: %s", service_url, e)

    async def aux(turn_context: TurnContext):
        try:
            await bot.on_turn(turn_context)
        except Exception as ex:
            log.error("[BOT ERROR] %s", ex, exc_info=True)
            raise

    await adapter.process_activity(activity, auth_header, aux)
    return web.Response(status=201)

async def health(_: web.Request) -> web.Response:
    return web.json_response({"ok": True})

async def diag_env(_: web.Request) -> web.Response:
    return web.json_response(public_env_snapshot())

def _authority_for(tenant: str) -> str:
    return f"https://login.microsoftonline.com/{tenant or 'organizations'}"

async def diag_msal(_: web.Request) -> web.Response:
    authority = _authority_for(TENANT)
    log.info("Initializing with Entra authority: %s", authority)
    try:
        appc = _msal_client(authority)
        token = appc.acquire_token_for_client(scopes=SCOPE)
        ok = "access_token" in token
        payload = {"ok": ok, "keys": list(token.keys()), "authority": authority}
        if not ok:
            payload["error"] = token
        return web.json_response(payload, status=200 if ok else 500)
    except Exception as e:
        return web.json_response({"ok": False, "exception": str(e)}, status=500)

async def diag_msal_bf(_: web.Request) -> web.Response:
    authority = "https://login.microsoftonline.com/botframework.com"
    log.info("Initializing with Entra authority: %s", authority)
    try:
        appc = _msal_client(authority)
        token = appc.acquire_token_for_client(scopes=SCOPE)
        ok = "access_token" in token
        payload = {"ok": ok, "keys": list(token.keys()), "authority": authority}
        if not ok:
            payload["error"] = token
        return web.json_response(payload, status=200 if ok else 500)
    except Exception as e:
        return web.json_response({"ok": False, "exception": str(e)}, status=500)

app = web.Application()
app.router.add_post("/api/messages", messages)
app.router.add_get("/health", health)
app.router.add_get("/diag/env", diag_env)
app.router.add_get("/diag/msal", diag_msal)
app.router.add_get("/diag/msal-bf", diag_msal_bf)

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))  # Render te la inyecta como env
    web.run_app(app, host="0.0.0.0", port=port)
