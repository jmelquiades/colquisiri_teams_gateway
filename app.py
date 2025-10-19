# app.py — Teams Gateway (aiohttp + BotFrameworkAdapter, SDK 4.14.x)
# Normaliza recipient.id (strip "28:") y añade trazas útiles.

import os
import logging
from aiohttp import web
from botbuilder.core import TurnContext, BotFrameworkAdapter, BotFrameworkAdapterSettings
from botbuilder.schema import Activity
import msal
from bot import DataTalkBot

logging.basicConfig(level=logging.INFO)
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
            }.get(name, ""),
            fallback,
        ),
    )

def bridge_env_vars():
    mapping = [
        ("MICROSOFT_APP_ID", "MicrosoftAppId"),
        ("MICROSOFT_APP_PASSWORD", "MicrosoftAppPassword"),
        ("MICROSOFT_APP_TENANT_ID", "MicrosoftAppTenantId"),
        ("MICROSOFT_APP_TYPE", "MicrosoftAppType"),
    ]
    for upper, camel in mapping:
        if os.getenv(upper) and not os.getenv(camel):
            os.environ[camel] = os.getenv(upper)

def public_env_snapshot() -> dict:
    keys = [
        "MICROSOFT_APP_ID","MICROSOFT_APP_PASSWORD","MICROSOFT_APP_TENANT_ID","MICROSOFT_APP_TYPE",
        "MicrosoftAppId","MicrosoftAppPassword","MicrosoftAppTenantId","MicrosoftAppType",
        "PORT",
    ]
    return {k: ("SET(***masked***)" if os.getenv(k) else "MISSING") for k in keys}

bridge_env_vars()

APP_ID = _env("MICROSOFT_APP_ID", "").strip()
APP_PASSWORD = _env("MICROSOFT_APP_PASSWORD", "").strip()

adapter = BotFrameworkAdapter(BotFrameworkAdapterSettings(APP_ID, APP_PASSWORD))
bot = DataTalkBot()

async def on_error(context: TurnContext, error: Exception):
    log.error("[BOT ERROR] %s", error, exc_info=True)
    try:
        await context.send_activity("Ocurrió un error procesando tu mensaje. Estamos corrigiéndolo.")
    except Exception as e:
        log.error("[BOT ERROR][send_activity] %s", e, exc_info=True)

adapter.on_turn_error = on_error

def _normalize_bot_id(raw_id: str | None) -> str | None:
    if not raw_id:
        return raw_id
    return raw_id[3:] if raw_id.startswith("28:") else raw_id

async def messages(req: web.Request) -> web.Response:
    if "application/json" not in req.headers.get("Content-Type", ""):
        return web.Response(status=415, text="Content-Type must be application/json")

    body = await req.json()
    activity: Activity = Activity().deserialize(body)
    auth_header = req.headers.get("Authorization", "")

    rec_id_raw = getattr(activity.recipient, "id", None)
    rec_id = _normalize_bot_id(rec_id_raw)
    svc_url = getattr(activity, "service_url", None)
    chan_id = getattr(activity, "channel_id", None)

    log.info("[DIAG] Our APP_ID=%s | activity.recipient.id(raw)=%s | normalized=%s | channel=%s | serviceUrl=%s",
             APP_ID, rec_id_raw, rec_id, chan_id, svc_url)

    # Solo alertamos si tras normalizar aún difiere
    if rec_id and APP_ID and rec_id != APP_ID:
        log.error("[MISMATCH] Mensaje para botId=%s, pero proceso firma como=%s. Revisa AppId/secret/manifest.",
                  rec_id, APP_ID)

    async def aux_func(turn_context: TurnContext):
        await bot.on_turn(turn_context)

    # Orden correcto: (activity, auth_header, callback)
    await adapter.process_activity(activity, auth_header, aux_func)
    return web.Response(status=201)

async def health(_: web.Request) -> web.Response:
    return web.json_response({"ok": True})

async def diag_env(_: web.Request) -> web.Response:
    return web.json_response(public_env_snapshot())

TENANT = _env("MICROSOFT_APP_TENANT_ID") or "organizations"
AUTHORITY_ORG = f"https://login.microsoftonline.com/{TENANT}"
AUTHORITY_BF  = "https://login.microsoftonline.com/botframework.com"
SCOPE = ["https://api.botframework.com/.default"]

def _try_token(authority: str) -> dict:
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
    return payload

async def diag_msal(_: web.Request) -> web.Response:
    return web.json_response(_try_token(AUTHORITY_ORG), status=200)

async def diag_msal_bf(_: web.Request) -> web.Response:
    return web.json_response(_try_token(AUTHORITY_BF), status=200)

app = web.Application()
app.router.add_post("/api/messages", messages)
app.router.add_get("/health", health)
app.router.add_get("/diag/env", diag_env)
app.router.add_get("/diag/msal", diag_msal)
app.router.add_get("/diag/msal-bf", diag_msal_bf)

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    log.info("Starting on :%s", port)
    web.run_app(app, host="0.0.0.0", port=port)
