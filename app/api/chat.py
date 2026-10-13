from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import ChatMessageRequest, ChatMessageResponse
from app.database.session import get_db
from app.services.chat_handler import process_message

router = APIRouter(prefix="/chat", tags=["Chat"])


@router.post("/message", response_model=ChatMessageResponse)
async def handle_message(request: ChatMessageRequest, db: AsyncSession = Depends(get_db)):
    return await process_message(db, request.chat_id, request.message)
