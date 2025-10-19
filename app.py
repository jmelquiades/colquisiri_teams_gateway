# app.py (bloque de auth/adapter)

import os
from aiohttp import web
from botbuilder.core import TurnContext
from botbuilder.core.cloud_adapter import CloudAdapter
from botbuilder.core.bot_framework_authentication import ConfigurationBotFrameworkAuthentication
from botbuilder.schema import Activity
from bot import DataTalkBot

def _env(name, fallback=""):
    # Acepta MAYÚSCULAS y camelCase (por si en Render quedaron camelCase)
    return os.getenv(name, os.getenv({
        "MICROSOFT_APP_ID": "MicrosoftAppId",
        "MICROSOFT_APP_PASSWORD": "MicrosoftAppPassword",
        "MICROSOFT_APP_TENANT_ID": "MicrosoftAppTenantId",
        "MICROSOFT_APP_TYPE": "MicrosoftAppType",
    }.get(name, ""), fallback))

CONFIG = {
    "MicrosoftAppId": _env("MICROSOFT_APP_ID"),
    "MicrosoftAppPassword": _env("MICROSOFT_APP_PASSWORD"),
    "MicrosoftAppType": _env("MICROSOFT_APP_TYPE", "SingleTenant"),
    "MicrosoftAppTenantId": _env("MICROSOFT_APP_TENANT_ID"),  # tu GUID
}

auth = ConfigurationBotFrameworkAuthentication(CONFIG)
adapter = CloudAdapter(auth)
bot = DataTalkBot()

async def on_error(context: TurnContext, error: Exception):
    print(f"[BOT ERROR] {error}", flush=True)
    await context.send_activity("Ocurrió un error de autenticación. Ya lo estamos corrigiendo.")
adapter.on_turn_error = on_error

async def messages(req: web.Request) -> web.Response:
    if "application/json" not in req.headers.get("Content-Type", ""):
        return web.Response(status=415, text="Content-Type must be application/json")
    body = await req.json()
    activity = Activity().deserialize(body)
    auth_header = req.headers.get("Authorization", "")
    await adapter.process_activity(auth_header, activity, bot.on_turn)
    return web.Response(status=201)

# Manejo de errores de adapter
async def on_error(context: TurnContext, error: Exception):
    await context.send_activity(f"Oops, ocurrió un error: {error}")
adapter.on_turn_error = on_error

# --- Rutas ---
async def messages(req: web.Request) -> web.Response:
    if "application/json" not in req.headers.get("Content-Type", ""):
        return web.Response(status=415, text="Content-Type must be application/json")

    body = await req.json()
    activity = Activity().deserialize(body)

    auth_header = req.headers.get("Authorization", "")

    async def aux_func(turn_context: TurnContext):
        await bot.on_turn(turn_context)

    await adapter.process_activity(activity, auth_header, aux_func)
    return web.Response(status=201)

async def health(_: web.Request) -> web.Response:
    return web.json_response({"ok": True})

async def diag_env(_: web.Request) -> web.Response:
    return web.json_response(public_env_snapshot())

app = web.Application()
app.router.add_post("/api/messages", messages)
app.router.add_get("/health", health)
app.router.add_get("/diag/env", diag_env)

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    web.run_app(app, host="0.0.0.0", port=port)

import msal

TENANT = _env("MICROSOFT_APP_TENANT_ID") or "organizations"
AUTHORITY = f"https://login.microsoftonline.com/{TENANT}"
SCOPE = ["https://api.botframework.com/.default"]

async def diag_msal(_: web.Request) -> web.Response:
    try:
        app = msal.ConfidentialClientApplication(
            client_id=_env("MICROSOFT_APP_ID"),
            client_credential=_env("MICROSOFT_APP_PASSWORD"),
            authority=AUTHORITY,
        )
        token = app.acquire_token_for_client(scopes=SCOPE)
        ok = "access_token" in token
        payload = {"ok": ok, "keys": list(token.keys())}
        if not ok:
            payload["error"] = token
        return web.json_response(payload, status=200 if ok else 500)
    except Exception as e:
        return web.json_response({"ok": False, "exception": str(e)}, status=500)

app.router.add_get("/diag/msal", diag_msal)
