from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import ChatMessageResponse
from app.services import chat_history
from app.services.conversation_flow import process_message as process_conversation


async def process_message(db: AsyncSession, chat_id: str, text: str) -> ChatMessageResponse:
    """Точка входу чату — історія + сценарій Олега / режим менеджера."""
    chat_id = str(chat_id)
    text = (text or "").strip()
    await chat_history.save_message(db, chat_id, "user", text)

    if await chat_history.is_human_mode(db, chat_id):
        # AI мовчить: повідомлення лише в історію, відповідає менеджер з адмінки
        return ChatMessageResponse(response="", action="human_mode")

    result = await process_conversation(db, chat_id, text)
    if result.response:
        await chat_history.save_message(db, chat_id, "bot", result.response)
    return result
