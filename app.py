import os
from aiohttp import web
from botbuilder.core import BotFrameworkAdapter, BotFrameworkAdapterSettings, TurnContext
from botbuilder.schema import Activity
from settings import MICROSOFT_APP_ID, MICROSOFT_APP_PASSWORD, public_env_snapshot
from bot import DataTalkBot

# --- Adapter y Bot ---
settings = BotFrameworkAdapterSettings(MICROSOFT_APP_ID, MICROSOFT_APP_PASSWORD)
adapter = BotFrameworkAdapter(settings)
bot = DataTalkBot()

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
