from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import (
    ClientRegisterRequest,
    DeliveryRequest,
    InvoiceRequest,
    OrderCreateRequest,
)
from app.database.session import get_db
from app.services.order_service import OrderService

router = APIRouter(prefix="/orders", tags=["Orders"])
order_service = OrderService()


@router.post("/register-client")
async def register_client(request: ClientRegisterRequest, db: AsyncSession = Depends(get_db)):
    result = await order_service.register_client_in_crm(
        db, request.chat_id, request.model_dump()
    )
    return result


@router.post("/create")
async def create_order(request: OrderCreateRequest, db: AsyncSession = Depends(get_db)):
    order = await order_service.create_order(db, request.chat_id, request.model_dump())
    from sqlalchemy import select
    from app.database.models import Client

    client_result = await db.execute(select(Client).where(Client.id == order.client_id))
    client = client_result.scalar_one()
    return {
        "success": True,
        "order_id": order.id,
        "status": order.status.value,
        "total_amount": order.total_amount,
        "onebox_deal_id": client.onebox_deal_id,
    }


@router.post("/delivery")
async def save_delivery(request: DeliveryRequest, db: AsyncSession = Depends(get_db)):
    try:
        delivery = await order_service.save_delivery_info(db, request.chat_id, request.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "success": True,
        "delivery_id": delivery.id,
        "delivery_type": delivery.delivery_type,
        "recipient_name": delivery.recipient_name,
    }


@router.post("/{order_id}/ttn")
async def create_ttn(order_id: int, db: AsyncSession = Depends(get_db)):
    result = await order_service.create_ttn(db, order_id)
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return result


@router.post("/{order_id}/payment")
async def create_payment(order_id: int, db: AsyncSession = Depends(get_db)):
    result = await order_service.create_payment(db, order_id)
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return result


@router.post("/invoice")
async def create_invoice(request: InvoiceRequest, db: AsyncSession = Depends(get_db)):
    result = await order_service.create_invoice(db, request.order_id, request.model_dump())
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return result
