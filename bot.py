import json
from typing import Dict, Any
from botbuilder.core import TurnContext, MessageFactory, ActivityHandler
from n2sql_client import N2SQLClient
from presenters import to_markdown_table

class DataTalkBot(ActivityHandler):
    def __init__(self):
        self.n2sql = N2SQLClient()

    async def on_message_activity(self, turn_context: TurnContext):
        user_text = (turn_context.activity.text or "").strip()
        if not user_text:
            await turn_context.send_activity("No recibí texto.")
            return

        try:
            result: Dict[str, Any] = self.n2sql.query_from_text(user_text)
            md = to_markdown_table(result)
            await turn_context.send_activity(MessageFactory.text(md))
        except Exception as ex:
            await turn_context.send_activity(f"Ocurrió un error resolviendo tu consulta: `{ex}`")

    async def on_members_added_activity(self, members_added, turn_context: TurnContext):
        for _ in members_added:
            await turn_context.send_activity("Hola, soy CRITERIA DataTalk. Escríbeme qué necesitas consultar.")
