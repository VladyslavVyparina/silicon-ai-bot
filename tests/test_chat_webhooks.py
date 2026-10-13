import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_chat_consult(client: AsyncClient):
    resp = await client.post("/api/chat/message", json={
        "chat_id": "chat-1",
        "message": "Розкажіть про RFID браслети",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert "response" in data
    assert data["action"] in ("consult", "escalate")


@pytest.mark.asyncio
async def test_chat_pricing(client: AsyncClient):
    resp = await client.post("/api/chat/message", json={
        "chat_id": "chat-2",
        "message": "Скільки коштує 300 шт силіконових браслетів 12 мм?",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["action"] in ("pricing", "await_order_confirm")
    assert "10.50" in data["response"] or (data.get("data") or {}).get("unit_price") == 10.50


@pytest.mark.asyncio
async def test_chat_order_intent(client: AsyncClient):
    resp = await client.post("/api/chat/message", json={
        "chat_id": "chat-3",
        "message": "Хочу замовити браслети",
    })
    assert resp.status_code == 200
    assert resp.json()["action"] == "collect_order_data"
    assert "прізвище" in resp.json()["response"].lower()


@pytest.mark.asyncio
async def test_chat_delivery_intent(client: AsyncClient):
    resp = await client.post("/api/chat/message", json={
        "chat_id": "chat-4",
        "message": "Як оформити доставку Новою поштою?",
    })
    assert resp.status_code == 200
    assert resp.json()["action"] == "collect_delivery_data"
    assert "відділення" in resp.json()["response"].lower() or "1" in resp.json()["response"]


@pytest.mark.asyncio
async def test_chat_invoice_intent(client: AsyncClient):
    resp = await client.post("/api/chat/message", json={
        "chat_id": "chat-5",
        "message": "Потрібен рахунок на реквізити, ЄДРПОУ",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["action"] in ("collect_invoice_data", "collect_edrpou")
    assert "єдрпоу" in data["response"].lower() or "компані" in data["response"].lower()


@pytest.mark.asyncio
async def test_chat_edrpou_lookup(client: AsyncClient):
    resp = await client.post("/api/chat/message", json={
        "chat_id": "chat-edrpou-1",
        "message": "Хочу знайти ФОП по єдрпоу 12345678",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["action"] == "edrpou_found"
    assert "сілікон" in data["response"].lower() or "1с" in data["response"].lower()


@pytest.mark.asyncio
async def test_onebox_completed_webhook(client: AsyncClient):
    chat_id = "tg-completed"
    reg = await client.post("/api/orders/register-client", json={
        "chat_id": chat_id,
        "first_name": "Done",
        "phone": "+380501234567",
    })
    assert reg.json()["onebox_deal_id"] is not None

    order_resp = await client.post("/api/orders/create", json={
        "chat_id": chat_id,
        "product_type": "силіконовий",
        "quantity": 100,
        "total_amount": 1150.0,
    })
    deal_id = order_resp.json().get("onebox_deal_id") or reg.json()["onebox_deal_id"]

    await client.post("/api/orders/delivery", json={
        "chat_id": chat_id,
        "delivery_type": "warehouse",
        "recipient_last_name": "Done",
        "recipient_first_name": "Client",
        "recipient_phone": "380501234567",
        "city": "Київ",
        "warehouse": "1",
    })

    resp = await client.post("/api/webhooks/onebox/order-completed", json={
        "deal_id": deal_id,
        "status": "completed",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["ttn_number"].startswith("2045")
