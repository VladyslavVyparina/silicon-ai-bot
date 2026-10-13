from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import ConsultRequest, ConsultResponse
from app.database.session import get_db
from app.services.ai_consultant import AIConsultant

router = APIRouter(prefix="/ai", tags=["AI"])
consultant = AIConsultant()


@router.post("/consult", response_model=ConsultResponse)
async def consult(request: ConsultRequest, db: AsyncSession = Depends(get_db)):
    result = await consultant.consult(db, request.question, request.chat_id)
    return ConsultResponse(**result)
