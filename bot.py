# bot.py — Bot mínimo para validar auth (responde un eco)
from botbuilder.core import ActivityHandler, TurnContext, MessageFactory


class DataTalkBot(ActivityHandler):
    async def on_message_activity(self, turn_context: TurnContext):
        text = (turn_context.activity.text or "").strip()
        if not text:
            await turn_context.send_activity("Hola, te escucho 👋")
            return
        await turn_context.send_activity(MessageFactory.text(f"ECO: {text}"))
