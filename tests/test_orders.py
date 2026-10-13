import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_register_client(client: AsyncClient):
    resp = await client.post("/api/orders/register-client", json={
        "chat_id": "tg-100",
        "first_name": "Іван",
        "last_name": "Петренко",
        "phone": "+380501234567",
        "email": "ivan@test.com",
        "city": "Київ",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["onebox_contact_id"] is not None
    assert data["client_id"] > 0


@pytest.mark.asyncio
async def test_register_client_with_edrpou(client: AsyncClient):
    resp = await client.post("/api/orders/register-client", json={
        "chat_id": "tg-101",
        "first_name": "Олена",
        "last_name": "Коваленко",
        "phone": "+380671234567",
        "edrpou": "12345678",
        "company_name": "ТОВ Сілікон",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["onec_counterparty_id"] == "1c-001"
    assert data["onec_created"] is False


@pytest.mark.asyncio
async def test_create_order(client: AsyncClient):
    await client.post("/api/orders/register-client", json={
        "chat_id": "tg-200",
        "first_name": "Марія",
        "phone": "+380931234567",
    })
    resp = await client.post("/api/orders/create", json={
        "chat_id": "tg-200",
        "product_type": "силіконовий браслет 12мм",
        "quantity": 100,
        "unit_price": 11.50,
        "total_amount": 1150.0,
        "production_days": 14,
        "comment": "Червоний колір, логотип 1 колір",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["order_id"] > 0
    assert data["status"] == "new"


@pytest.mark.asyncio
async def test_save_delivery_info(client: AsyncClient):
    await client.post("/api/orders/register-client", json={
        "chat_id": "tg-300",
        "first_name": "Андрій",
        "phone": "+380991234567",
    })
    resp = await client.post("/api/orders/delivery", json={
        "chat_id": "tg-300",
        "delivery_type": "warehouse",
        "recipient_last_name": "Шевченко",
        "recipient_first_name": "Андрій",
        "recipient_phone": "380991234567",
        "city": "Київ",
        "warehouse": "5",
        "weight_kg": 2.5,
        "seats_amount": 1,
        "description": "Браслети Silicon",
    })
    assert resp.status_code == 200
    assert resp.json()["success"] is True


@pytest.mark.asyncio
async def test_full_order_flow(client: AsyncClient):
    chat_id = "tg-full-flow"
    await client.post("/api/orders/register-client", json={
        "chat_id": chat_id,
        "first_name": "Тест",
        "last_name": "Клієнт",
        "phone": "+380501111111",
        "email": "test@silicon.ua",
        "city": "Львів",
    })
    order_resp = await client.post("/api/orders/create", json={
        "chat_id": chat_id,
        "product_type": "силіконовий",
        "quantity": 500,
        "total_amount": 4600.0,
        "production_days": 14,
    })
    order_id = order_resp.json()["order_id"]

    await client.post("/api/orders/delivery", json={
        "chat_id": chat_id,
        "delivery_type": "warehouse",
        "recipient_last_name": "Клієнт",
        "recipient_first_name": "Тест",
        "recipient_phone": "380501111111",
        "city": "Львів",
        "warehouse": "1",
    })

    ttn_resp = await client.post(f"/api/orders/{order_id}/ttn")
    assert ttn_resp.status_code == 200
    ttn_data = ttn_resp.json()
    assert ttn_data["success"] is True
    assert ttn_data["ttn_number"].startswith("2045")

    payment_resp = await client.post(f"/api/orders/{order_id}/payment")
    assert payment_resp.status_code == 200
    assert "payment_url" in payment_resp.json()
