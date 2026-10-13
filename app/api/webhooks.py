from fastapi import APIRouter, Depends, Form, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import WebhookOneboxRequest
from app.config import get_settings
from app.database.session import get_db
from app.services.order_service import OrderService

router = APIRouter(prefix="/webhooks", tags=["Webhooks"])
order_service = OrderService()
settings = get_settings()


@router.post("/liqpay")
async def liqpay_webhook(
    data: str = Form(...),
    signature: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    result = await order_service.handle_liqpay_webhook(db, data, signature)
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return result


@router.post("/onebox/order-completed")
async def onebox_order_completed(
    request: WebhookOneboxRequest,
    db: AsyncSession = Depends(get_db),
):
    if settings.onebox_webhook_secret and request.secret != settings.onebox_webhook_secret:
        raise HTTPException(status_code=403, detail="Invalid webhook secret")

    if request.status.lower() not in ("completed", "виконано", "done"):
        return {"success": True, "message": "Status ignored", "status": request.status}

    result = await order_service.handle_order_completed(db, request.deal_id)
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return result
