import base64
import json

import pytest
from httpx import AsyncClient

from app.services.liqpay import LiqPayService


def test_create_payment():
    service = LiqPayService()
    result = service.create_payment("42", 1150.0, "Тестове замовлення")
    assert result["success"] is True
    assert "payment_url" in result
    assert "data=" in result["payment_url"]
    assert "signature=" in result["payment_url"]
    assert result["payment_url"].startswith("https://www.liqpay.ua/api/3/checkout?")
    assert result["liqpay_order_id"].startswith("silicon-42-")
    assert result.get("data")
    assert result.get("signature")
    payload = json.loads(base64.b64decode(result["data"]))
    assert "server_url" not in payload
    assert "result_url" not in payload


def test_http_ip_urls_are_not_sent_to_liqpay():
    service = LiqPayService()
    result = service.create_payment(
        "42",
        1150.0,
        "Тест",
        result_url="http://173.242.60.134:8000/pay/3",
        liqpay_order_id="silicon-42-reuse",
    )
    assert result["liqpay_order_id"] == "silicon-42-reuse"
    payload = json.loads(base64.b64decode(result["data"]))
    assert payload["order_id"] == "silicon-42-reuse"
    assert "result_url" not in payload
    assert "server_url" not in payload


def test_webhook_signature_roundtrip():
    service = LiqPayService()
    payment = service.create_payment("99", 500.0, "Test")
    data = payment["data"]
    signature = payment["signature"]
    assert service.verify_webhook(data, signature) is True
    assert service.verify_webhook(data, "invalid") is False


def test_status_paid_helper():
    service = LiqPayService()
    assert service.is_paid_status("success") is True
    assert service.is_paid_status("sandbox") is True
    assert service.is_paid_status("wait_accept") is False
    mock = service.fetch_status("silicon-1-abc")
    assert mock["success"] is True
    assert mock["status"] == "wait_accept"
    service = LiqPayService()
    payload = {"order_id": "silicon-1-abc", "status": "success", "amount": 100}
    data_b64 = base64.b64encode(json.dumps(payload).encode()).decode()
    decoded = service.decode_webhook(data_b64)
    assert decoded["status"] == "success"


@pytest.mark.asyncio
async def test_liqpay_webhook_success(client: AsyncClient):
    chat_id = "tg-liqpay"
    await client.post("/api/orders/register-client", json={
        "chat_id": chat_id,
        "first_name": "Pay",
        "phone": "+380501234567",
    })
    order_resp = await client.post("/api/orders/create", json={
        "chat_id": chat_id,
        "product_type": "силіконовий",
        "quantity": 100,
        "total_amount": 1150.0,
    })
    order_id = order_resp.json()["order_id"]
    payment_resp = await client.post(f"/api/orders/{order_id}/payment")
    liqpay_order_id = payment_resp.json()["liqpay_order_id"]

    service = LiqPayService()
    payload = {"order_id": liqpay_order_id, "status": "sandbox", "amount": 1150}
    data_b64 = base64.b64encode(json.dumps(payload).encode()).decode()
    signature = service._sign(data_b64)

    webhook_resp = await client.post(
        "/api/webhooks/liqpay",
        data={"data": data_b64, "signature": signature},
    )
    assert webhook_resp.status_code == 200
    assert webhook_resp.json()["success"] is True


@pytest.mark.asyncio
async def test_liqpay_short_pay_page(client: AsyncClient):
    chat_id = "tg-pay-page"
    await client.post("/api/orders/register-client", json={
        "chat_id": chat_id,
        "first_name": "Pay",
        "phone": "+380501234567",
    })
    order_resp = await client.post("/api/orders/create", json={
        "chat_id": chat_id,
        "product_type": "силіконовий",
        "quantity": 100,
        "total_amount": 1150.0,
    })
    order_id = order_resp.json()["order_id"]
    payment_resp = await client.post(f"/api/orders/{order_id}/payment")
    assert payment_resp.status_code == 200
    url = payment_resp.json()["payment_url"]
    again = await client.post(f"/api/orders/{order_id}/payment")
    assert again.json()["payment_url"] == url
    assert "/pay/" in url
    payment_id = int(url.rstrip("/").split("/")[-1])
    page = await client.get(f"/pay/{payment_id}")
    assert page.status_code == 200
    assert "liqpay.ua/api/3/checkout" in page.text
    assert 'name="data"' in page.text
    assert 'name="signature"' in page.text
    assert "силіконовий" in page.text
    assert "100 шт" in page.text
    assert "1150.00" in page.text


@pytest.mark.asyncio
async def test_payment_url_uses_public_base(client: AsyncClient, monkeypatch):
    from app.config import get_settings

    monkeypatch.setenv("PUBLIC_BASE_URL", "https://bot.silicon.com.ua")
    get_settings.cache_clear()
    try:
        chat_id = "tg-pay-domain"
        await client.post("/api/orders/register-client", json={
            "chat_id": chat_id,
            "first_name": "Pay",
            "phone": "+380501234567",
        })
        order_resp = await client.post("/api/orders/create", json={
            "chat_id": chat_id,
            "product_type": "силіконовий",
            "quantity": 100,
            "total_amount": 1150.0,
        })
        order_id = order_resp.json()["order_id"]
        payment_resp = await client.post(f"/api/orders/{order_id}/payment")
        url = payment_resp.json()["payment_url"]
        assert url.startswith("https://bot.silicon.com.ua/pay/")
        assert "173." not in url
        assert "127.0.0.1" not in url
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_create_invoice(client: AsyncClient):
    chat_id = "tg-invoice"
    await client.post("/api/orders/register-client", json={
        "chat_id": chat_id,
        "first_name": "Invoice",
        "phone": "+380501234567",
        "company_name": "Нова Компанія",
        "edrpou": "99999999",
    })
    order_resp = await client.post("/api/orders/create", json={
        "chat_id": chat_id,
        "product_type": "силіконовий",
        "quantity": 200,
        "total_amount": 2200.0,
    })
    order_id = order_resp.json()["order_id"]

    resp = await client.post("/api/orders/invoice", json={
        "order_id": order_id,
        "edrpou": "99999999",
        "company_name": "Нова Компанія",
        "ipn": "1234567890",
        "company_address": "м. Київ, вул. Тестова, 1",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["invoice_number"].startswith("INV-")
    assert data.get("invoice_pdf_path")
    from pathlib import Path
    assert Path(data["invoice_pdf_path"]).exists()


def test_liqpay_watch_defaults():
    from app.config import get_settings

    settings = get_settings()
    assert settings.liqpay_watch_seconds == 900
    assert settings.liqpay_watch_interval_seconds == 20


@pytest.mark.asyncio
async def test_watch_pending_payments_self_check(db_session, monkeypatch):
    from datetime import datetime, timedelta

    from app.database.models import Client, Order, Payment, PaymentStatus
    from app.services.order_service import OrderService

    client = Client(chat_id="watch-pay-1", first_name="Іван", phone="0501112233")
    db_session.add(client)
    await db_session.flush()
    order = Order(
        client_id=client.id,
        product_type="силіконовий",
        quantity=100,
        total_amount=1150.0,
        liqpay_order_id="silicon-watch-1",
    )
    db_session.add(order)
    await db_session.flush()
    pay = Payment(
        order_id=order.id,
        amount=1150.0,
        status=PaymentStatus.PENDING,
        liqpay_payment_id="silicon-watch-1",
        payment_url="https://www.liqpay.ua/api/3/checkout?x=1",
        created_at=datetime.utcnow(),
    )
    db_session.add(pay)
    await db_session.commit()

    service = OrderService()
    monkeypatch.setattr(
        service.liqpay,
        "fetch_status",
        lambda oid: {"success": True, "status": "wait_accept"},
    )
    assert await service.watch_pending_payments(db_session) == 0
    await db_session.refresh(pay)
    assert pay.status == PaymentStatus.PENDING

    monkeypatch.setattr(
        service.liqpay,
        "fetch_status",
        lambda oid: {"success": True, "status": "sandbox"},
    )
    assert await service.watch_pending_payments(db_session) == 1
    await db_session.refresh(pay)
    assert pay.status == PaymentStatus.SUCCESS

    old = Payment(
        order_id=order.id,
        amount=100.0,
        status=PaymentStatus.PENDING,
        liqpay_payment_id="silicon-watch-old",
        created_at=datetime.utcnow() - timedelta(seconds=901),
    )
    db_session.add(old)
    await db_session.commit()
    monkeypatch.setattr(
        service.liqpay,
        "fetch_status",
        lambda oid: {"success": True, "status": "success"},
    )
    assert await service.watch_pending_payments(db_session) == 0
    await db_session.refresh(old)
    assert old.status == PaymentStatus.PENDING
