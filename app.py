# app.py — Teams Gateway (aiohttp + BotFrameworkAdapter, SDK 4.14.x)
# Fix: puentea variables de entorno a camelCase para el SDK (MicrosoftAppId/Password/TenantId/Type)

import os
from aiohttp import web
from botbuilder.core import TurnContext, BotFrameworkAdapter, BotFrameworkAdapterSettings
from botbuilder.schema import Activity
import msal

# Tu bot principal (debe exponer .on_turn)
from bot import DataTalkBot


# =========================
# Helpers de configuración
# =========================
def _env(name: str, fallback: str = "") -> str:
    """
    Lee primero MAYÚSCULAS; si no existe, intenta camelCase (por compatibilidad
    con variables ya creadas en Render). Si no, usa fallback.
    """
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
    """
    Asegura que si solo seteaste MICROSOFT_*,
    el SDK también vea las claves camelCase Microsoft*.
    """
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
    """
    Snapshot seguro del entorno: indica si existen variables críticas
    sin revelar sus valores. Útil para /diag/env.
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
        "PORT",
    ]
    out = {}
    for k in keys:
        v = os.getenv(k)
        out[k] = "SET(***masked***)" if v else "MISSING"
    return out


# Puentea variables antes de leerlas
bridge_env_vars()

# =====================================
# Credenciales (AppId / Password AAD)
# =====================================
APP_ID = _env("MICROSOFT_APP_ID", "")
APP_PASSWORD = _env("MICROSOFT_APP_PASSWORD", "")

# Adapter clásico (estable con SDK 4.14.x)
adapter = BotFrameworkAdapter(BotFrameworkAdapterSettings(APP_ID, APP_PASSWORD))

# Instancia del bot
bot = DataTalkBot()


# ==========================
# Manejo global de errores
# ==========================
async def on_error(context: TurnContext, error: Exception):
    print(f"[BOT ERROR] {error}", flush=True)
    try:
        await context.send_activity(
            "Ocurrió un error procesando tu mensaje. Estamos corrigiéndolo."
        )
    except Exception as e:
        print(f"[BOT ERROR][send_activity] {e}", flush=True)


adapter.on_turn_error = on_error


# ==========
# Handlers
# ==========
async def messages(req: web.Request) -> web.Response:
    # Acepta "application/json" y variantes con charset
    if "application/json" not in req.headers.get("Content-Type", ""):
        return web.Response(status=415, text="Content-Type must be application/json")

    body = await req.json()
    activity = Activity().deserialize(body)
    auth_header = req.headers.get("Authorization", "")

    async def aux_func(turn_context: TurnContext):
        await bot.on_turn(turn_context)

    # ORDEN CORRECTO: (activity, auth_header, callback)
    await adapter.process_activity(activity, auth_header, aux_func)
    return web.Response(status=201)


async def health(_: web.Request) -> web.Response:
    return web.json_response({"ok": True})


async def diag_env(_: web.Request) -> web.Response:
    return web.json_response(public_env_snapshot())


# Diagnóstico de token con MSAL (para validar credenciales AAD)
TENANT = _env("MICROSOFT_APP_TENANT_ID") or "organizations"
AUTHORITY = f"https://login.microsoftonline.com/{TENANT}"
SCOPE = ["https://api.botframework.com/.default"]


async def diag_msal(_: web.Request) -> web.Response:
    """
    Devuelve ok=True si MSAL logra obtener un access_token con tu AppId/Password/Tenant.
    Si hay error (p.ej. unauthorized_client / invalid_client / wrong tenant), verás el payload.
    """
    try:
        appc = msal.ConfidentialClientApplication(
            client_id=APP_ID,
            client_credential=APP_PASSWORD,
            authority=AUTHORITY,
        )
        token = appc.acquire_token_for_client(scopes=SCOPE)
        ok = "access_token" in token
        sdk_env_seen = {
            "MicrosoftAppId": bool(os.getenv("MicrosoftAppId")),
            "MicrosoftAppPassword": bool(os.getenv("MicrosoftAppPassword")),
            "MicrosoftAppTenantId": bool(os.getenv("MicrosoftAppTenantId")),
            "MicrosoftAppType": bool(os.getenv("MicrosoftAppType")),
        }
        payload = {"ok": ok, "keys": list(token.keys()), "sdk_env_seen": sdk_env_seen}
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


# ==========
# Main
# ==========
if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    print(f"Starting on :{port}", flush=True)
    web.run_app(app, host="0.0.0.0", port=port)
