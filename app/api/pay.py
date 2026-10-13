"""Публічна сторінка оплати LiqPay (коротке посилання → POST form)."""
from html import escape

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import Order, Payment
from app.database.session import get_db
from app.services.liqpay import LiqPayService
from app.services.order_service import OrderService

router = APIRouter(tags=["Pay"])


@router.get("/pay/{payment_id}", response_class=HTMLResponse)
async def pay_redirect(payment_id: int, db: AsyncSession = Depends(get_db)):
    """
    Коротке посилання для Telegram.
    Завжди збирає data/signature з актуального замовлення (товар, кількість, сума).
    """
    payment = (
        await db.execute(select(Payment).where(Payment.id == payment_id))
    ).scalar_one_or_none()
    if not payment:
        raise HTTPException(status_code=404, detail="Платіж не знайдено")

    order = (
        await db.execute(select(Order).where(Order.id == payment.order_id))
    ).scalar_one_or_none()

    product = (order.product_type if order else None) or "Замовлення Silicon"
    qty = order.quantity if order and order.quantity else None
    amount = float(
        (order.total_amount if order and order.total_amount else None)
        or payment.amount
        or 0
    )
    if amount <= 0:
        raise HTTPException(status_code=400, detail="Немає суми платежу")

    desc = OrderService.payment_description(order) if order else f"Silicon #{payment.order_id}"

    # той самий order_id LiqPay, без result_url на http://IP — інакше checkout падає
    rebuilt = LiqPayService().create_payment(
        order_id=str(payment.order_id),
        amount=amount,
        description=desc,
        liqpay_order_id=payment.liqpay_payment_id or None,
    )
    if not rebuilt.get("success"):
        raise HTTPException(status_code=400, detail="Не вдалося створити LiqPay data")
    data_b64 = rebuilt["data"]
    signature = rebuilt["signature"]
    payment.liqpay_data = data_b64
    payment.liqpay_signature = signature
    payment.amount = amount
    await db.commit()

    checkout = LiqPayService.CHECKOUT_URL
    data_esc = escape(data_b64, quote=True)
    sig_esc = escape(signature, quote=True)
    product_esc = escape(str(product))
    qty_line = f"<p>Кількість: <strong>{int(qty)} шт</strong></p>" if qty else ""
    desc_esc = escape(desc)

    html = f"""<!DOCTYPE html>
<html lang="uk">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Оплата Silicon — LiqPay</title>
  <style>
    body {{ font-family: system-ui, sans-serif; background:#f4f6f8; margin:0;
      display:flex; min-height:100vh; align-items:center; justify-content:center; }}
    .box {{ background:#fff; padding:24px 28px; border-radius:12px; box-shadow:0 8px 24px rgba(0,0,0,.08);
      max-width:420px; text-align:center; }}
    button {{ background:#77cc5e; color:#fff; border:0; border-radius:8px; padding:12px 20px;
      font-size:16px; font-weight:600; cursor:pointer; }}
  </style>
</head>
<body>
  <div class="box">
    <h1>Оплата замовлення</h1>
    <p>Товар: <strong>{product_esc}</strong></p>
    {qty_line}
    <p>{desc_esc}</p>
    <p>Сума: <strong>{amount:.2f} грн</strong></p>
    <p>Зараз відкриється захищена сторінка LiqPay…</p>
    <form id="liqpay" method="POST" action="{checkout}" accept-charset="utf-8">
      <input type="hidden" name="data" value="{data_esc}" />
      <input type="hidden" name="signature" value="{sig_esc}" />
      <button type="submit">Перейти до оплати</button>
    </form>
  </div>
  <script>
    setTimeout(function() {{ document.getElementById('liqpay').submit(); }}, 400);
  </script>
</body>
</html>"""
    return HTMLResponse(content=html)
