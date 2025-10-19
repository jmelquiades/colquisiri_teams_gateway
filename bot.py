# bot.py
from botbuilder.core import ActivityHandler, TurnContext, MessageFactory

class DataTalkBot(ActivityHandler):
    async def on_message_activity(self, turn_context: TurnContext):
        text = (turn_context.activity.text or "").strip()
        await turn_context.send_activity(
            MessageFactory.text(f"Recibido 👋: `{text}` (gateway OK)")
        )
