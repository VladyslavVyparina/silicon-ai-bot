import pytest
from httpx import AsyncClient

from app.services.delivery_rules import (
    DELIVERY_TYPE_ADDRESS,
    DELIVERY_TYPE_WAREHOUSE,
    build_recipient_name,
    validate_delivery_payload,
)


def test_warehouse_requires_branch_number():
    ok, errors = validate_delivery_payload({
        "delivery_type": DELIVERY_TYPE_WAREHOUSE,
        "recipient_last_name": "Петренко",
        "recipient_first_name": "Іван",
        "recipient_phone": "380501234567",
        "city": "Київ",
    })
    assert ok is False
    assert any("відділення" in e for e in errors)


def test_warehouse_ok():
    payload = {
        "delivery_type": DELIVERY_TYPE_WAREHOUSE,
        "recipient_last_name": "Петренко",
        "recipient_first_name": "Іван",
        "recipient_phone": "380501234567",
        "city": "Київ",
        "warehouse": "12",
    }
    ok, errors = validate_delivery_payload(payload)
    assert ok is True
    assert errors == []
    assert payload["recipient_name"] == "Петренко Іван"


def test_address_requires_patronymic():
    ok, errors = validate_delivery_payload({
        "delivery_type": DELIVERY_TYPE_ADDRESS,
        "recipient_last_name": "Петренко",
        "recipient_first_name": "Іван",
        "recipient_phone": "380501234567",
        "city": "Київ",
        "street_address": "вул. Хрещатик, 1",
    })
    assert ok is False
    assert "по батькові" in errors


def test_address_ok():
    payload = {
        "delivery_type": DELIVERY_TYPE_ADDRESS,
        "recipient_last_name": "Петренко",
        "recipient_first_name": "Іван",
        "recipient_patronymic": "Іванович",
        "recipient_phone": "380501234567",
        "city": "Київ",
        "street_address": "вул. Хрещатик, 1",
    }
    ok, errors = validate_delivery_payload(payload)
    assert ok is True
    assert build_recipient_name(payload) == "Петренко Іван Іванович"


@pytest.mark.asyncio
async def test_chat_greeting_oleg(client: AsyncClient):
    resp = await client.post("/api/chat/message", json={
        "chat_id": "oleg-1",
        "message": "/start",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["action"] == "greeting"
    assert "Олег" in data["response"]
    assert "Silicon" in data["response"]


@pytest.mark.asyncio
async def test_hello_does_not_wipe_quote(client: AsyncClient):
    chat_id = "hello-keep-1"
    await client.post("/api/chat/message", json={
        "chat_id": chat_id,
        "message": "силіконові браслети 12 мм 100 штук",
    })
    r = await client.post("/api/chat/message", json={
        "chat_id": chat_id,
        "message": "привіт",
    })
    assert r.json()["action"] != "greeting"
    assert "менеджер компанії Silicon" not in r.json()["response"]
    assert "Продовжуємо" in r.json()["response"]


@pytest.mark.asyncio
async def test_calculate_and_order_starts_consult_not_crm(client: AsyncClient):
    resp = await client.post("/api/chat/message", json={
        "chat_id": "consult-first-1",
        "message": "Прорахувати та замовити продукцію",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["action"] != "collect_order_data"
    assert "прізвище" not in data["response"].lower()


@pytest.mark.asyncio
async def test_full_name_on_surname_step(client: AsyncClient):
    chat_id = "name-parse-1"
    await client.post("/api/chat/message", json={"chat_id": chat_id, "message": "замовити"})
    r = await client.post("/api/chat/message", json={"chat_id": chat_id, "message": "Влад Тест"})
    assert r.json()["action"] == "collect_order_data"
    # ім'я+прізвище в одному повідомленні → далі телефон
    assert "телефон" in r.json()["response"].lower()


@pytest.mark.asyncio
async def test_multi_product_cart_and_payment(client: AsyncClient):
    chat_id = "multi-cart-1"
    r1 = await client.post("/api/chat/message", json={
        "chat_id": chat_id,
        "message": "силіконові браслети 100 штук",
    })
    assert r1.json()["action"] == "await_order_confirm"
    assert "1150 грн" in r1.json()["response"]
    assert "1150.0" not in r1.json()["response"]
    assert "Оформлюємо?" in r1.json()["response"]

    r2 = await client.post("/api/chat/message", json={
        "chat_id": chat_id,
        "message": "ще світловідбиваючі 500 штук",
    })
    assert r2.json()["action"] == "await_order_confirm"
    body = r2.json()["response"]
    assert "У замовленні зараз:" in body or "Разом:" in body
    assert "12000" in body.replace(" ", "")
    assert "13150" in body.replace(" ", "")  # 1150 + 12000

    await client.post("/api/chat/message", json={"chat_id": chat_id, "message": "так"})
    await client.post("/api/chat/message", json={"chat_id": chat_id, "message": "Іван Тест"})
    await client.post("/api/chat/message", json={"chat_id": chat_id, "message": "0961730098"})
    choice = await client.post("/api/chat/message", json={
        "chat_id": chat_id,
        "message": "відділення 12 Київ",
    })
    assert choice.json()["action"] == "collect_payment"
    pay = await client.post("/api/chat/message", json={"chat_id": chat_id, "message": "liqpay"})
    assert pay.json()["action"] == "payment_created"
    text = pay.json()["response"]
    assert "Оплата онлайн через сервіс LiqPay" in text
    assert "Посилання на оплату:" in text
    assert "скрін" not in text.lower()
    assert "повідомте мене" not in text.lower()
    assert "13150" in text.replace(" ", "")


@pytest.mark.asyncio
async def test_invoice_flow_numeric_ipn_and_pdf(client: AsyncClient):
    """ІПН з цифр не повинен плутатись з ЄДРПОУ; PDF рахунку створюється."""
    chat_id = "invoice-ipn-pdf-1"
    await client.post("/api/chat/message", json={
        "chat_id": chat_id, "message": "силіконові браслети 50 штук",
    })
    await client.post("/api/chat/message", json={"chat_id": chat_id, "message": "так"})
    await client.post("/api/chat/message", json={"chat_id": chat_id, "message": "Олег Новий"})
    await client.post("/api/chat/message", json={"chat_id": chat_id, "message": "0501112233"})
    choice = await client.post("/api/chat/message", json={
        "chat_id": chat_id, "message": "відділення 3 Київ",
    })
    assert choice.json()["action"] == "collect_payment"

    await client.post("/api/chat/message", json={"chat_id": chat_id, "message": "рахунок"})
    await client.post("/api/chat/message", json={
        "chat_id": chat_id, "message": "ТОВ Новий Клієнт 2026",
    })
    await client.post("/api/chat/message", json={"chat_id": chat_id, "message": "55667788"})
    # цифровий ІПН (раніше ігнорувався як «схожий на ЄДРПОУ»)
    r_ipn = await client.post("/api/chat/message", json={
        "chat_id": chat_id, "message": "9988776655",
    })
    assert r_ipn.json()["action"] == "collect_invoice_data"
    assert "адрес" in r_ipn.json()["response"].lower()

    final = await client.post("/api/chat/message", json={
        "chat_id": chat_id, "message": "м. Львів, вул. Городоцька, 10",
    })
    assert final.json()["action"] == "confirm_invoice_data"
    assert "все вірно" in final.json()["response"].lower()
    assert "львів" in final.json()["response"].lower()
    created = await client.post("/api/chat/message", json={
        "chat_id": chat_id, "message": "так, все вірно",
    })
    assert created.json()["action"] == "invoice_created"
    data = created.json().get("data") or {}
    assert data.get("counterparty_created") is True
    assert data.get("invoice_pdf_path")
    from pathlib import Path
    assert Path(data["invoice_pdf_path"]).exists()
    docs = created.json().get("documents") or []
    assert docs and docs[0]["path"] == data["invoice_pdf_path"]
    reply = created.json()["response"].lower()
    assert "скрін" in reply


@pytest.mark.asyncio
async def test_invoice_paid_notice_in_admin(client: AsyncClient, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "silicon-admin")
    monkeypatch.setenv("ADMIN_OWNER_EMAIL", "owner@silicon.local")
    monkeypatch.setenv("ADMIN_SECRET_KEY", "test-admin-secret-invoice")
    from app.config import get_settings

    get_settings.cache_clear()

    chat_id = "invoice-paid-notice-1"
    await client.post("/api/chat/message", json={
        "chat_id": chat_id, "message": "силіконові браслети 50 штук",
    })
    await client.post("/api/chat/message", json={"chat_id": chat_id, "message": "так"})
    await client.post("/api/chat/message", json={"chat_id": chat_id, "message": "Олег Новий"})
    await client.post("/api/chat/message", json={"chat_id": chat_id, "message": "0501112233"})
    await client.post("/api/chat/message", json={
        "chat_id": chat_id, "message": "відділення 3 Київ",
    })
    await client.post("/api/chat/message", json={"chat_id": chat_id, "message": "рахунок"})
    await client.post("/api/chat/message", json={
        "chat_id": chat_id, "message": "ТОВ Оплата Реквізити",
    })
    await client.post("/api/chat/message", json={"chat_id": chat_id, "message": "11223344"})
    await client.post("/api/chat/message", json={"chat_id": chat_id, "message": "немає"})
    pdf = await client.post("/api/chat/message", json={
        "chat_id": chat_id, "message": "м. Київ, вул. Хрещатик, 1",
    })
    assert pdf.json()["action"] == "confirm_invoice_data"
    issued = await client.post("/api/chat/message", json={
        "chat_id": chat_id, "message": "так, все вірно",
    })
    assert issued.json()["action"] == "invoice_created"
    assert "скрін" in issued.json()["response"].lower()

    paid = await client.post("/api/chat/message", json={
        "chat_id": chat_id, "message": "я оплатив за рахунком",
    })
    assert paid.json()["action"] == "invoice_payment_notice"
    assert "скрін" in paid.json()["response"].lower()
    assert "менеджер" in paid.json()["response"].lower()

    shot = await client.post("/api/chat/message", json={
        "chat_id": chat_id,
        "message": "[СКРІН ОПЛАТИ|/uploads/telegram/pay_test.jpg] ось квитанція",
    })
    assert shot.json()["action"] == "invoice_payment_notice"
    assert "отримав" in shot.json()["response"].lower() or "дякую" in shot.json()["response"].lower()

    await client.get("/admin/login")
    login = await client.post(
        "/admin/login",
        data={"identifier": "owner@silicon.local", "password": "silicon-admin"},
        follow_redirects=False,
    )
    token = login.headers["location"].split("token=")[1].split("&")[0]
    page = await client.get(f"/admin/invoice-payments?token={token}")
    assert page.status_code == 200
    assert "Оплати за рахунком" in page.text
    assert chat_id in page.text
    assert "Написати в чат" in page.text
    stats = await client.get(f"/admin/stats?token={token}")
    assert stats.json()["invoice_notices"] >= 1


@pytest.mark.asyncio
async def test_multi_product_one_message(client: AsyncClient):
    chat_id = "multi-one-msg-1"
    r = await client.post("/api/chat/message", json={
        "chat_id": chat_id,
        "message": (
            "силіконові браслети 12 мм цікавить 100 штук "
            "та світловідбиваючі браслети 300x30 500 штук"
        ),
    })
    assert r.json()["action"] == "await_order_confirm"
    body = r.json()["response"]
    compact = body.replace(" ", "")
    assert "1150" in compact
    assert "12000" in compact
    assert "13150" in compact


@pytest.mark.asyncio
async def test_silicone_correction_after_reflective(client: AsyncClient):
    chat_id = "correct-silicone-1"
    await client.post("/api/chat/message", json={
        "chat_id": chat_id,
        "message": "світловідбиваючі браслети 100 штук",
    })
    r = await client.post("/api/chat/message", json={
        "chat_id": chat_id,
        "message": "Так, я вище писав силіконові браслети 12 мм цікавить 100 штук",
    })
    assert r.json()["action"] == "await_order_confirm"
    body = r.json()["response"].lower()
    assert "силікон" in body
    assert "світловідб" not in body
    assert "1150" in r.json()["response"].replace(" ", "")


@pytest.mark.asyncio
async def test_company_who_we_are(client: AsyncClient):
    resp = await client.post("/api/chat/message", json={
        "chat_id": "company-1",
        "message": "Хто ви і що за компанія?",
    })
    text = resp.json()["response"]
    assert "Silicon" in text
    assert "силіконові" in text.lower() or "RFID" in text
    assert "чим можу допомогти" not in text.lower()


@pytest.mark.asyncio
async def test_reflective_after_silicone_keeps_context(client: AsyncClient):
    """Після силікону нове замовлення на світловідбиваючі не має рахуватись як силікон."""
    chat_id = "reflective-ctx-1"
    r0 = await client.post("/api/chat/message", json={
        "chat_id": chat_id,
        "message": "силіконові браслети 100 штук",
    })
    assert r0.json()["action"] == "await_order_confirm"
    assert "1150" in r0.json()["response"].replace(" ", "")

    await client.post("/api/chat/message", json={
        "chat_id": chat_id,
        "message": "хочу ще світловідбиваючі браслети",
    })
    r1 = await client.post("/api/chat/message", json={
        "chat_id": chat_id,
        "message": "1000 штук",
    })
    body = r1.json()["response"].replace(" ", "").lower()
    assert r1.json()["action"] == "await_order_confirm"
    assert "24000" in body
    assert "світловідб" in r1.json()["response"].lower() or "300" in r1.json()["response"]
    assert "8500" not in body  # не силікон 12 мм × 1000


@pytest.mark.asyncio
async def test_reflective_quote_and_liqpay(client: AsyncClient):
    chat_id = "reflective-pay-1"
    r1 = await client.post("/api/chat/message", json={
        "chat_id": chat_id,
        "message": "світловідбиваючі браслети 1000 штук",
    })
    assert r1.json()["action"] == "await_order_confirm"
    assert "24000" in r1.json()["response"].replace(" ", "") or "24 000" in r1.json()["response"]

    await client.post("/api/chat/message", json={"chat_id": chat_id, "message": "так"})
    await client.post("/api/chat/message", json={
        "chat_id": chat_id,
        "message": "Іван Іванов",
    })
    r = await client.post("/api/chat/message", json={
        "chat_id": chat_id,
        "message": "0961730098",
    })
    assert r.json()["action"] == "collect_delivery_data"
    assert "менеджер після прорахунку" not in r.json()["response"].lower()
    assert "24000" in r.json()["response"].replace(" ", "") or "24000" in str(r.json())

    r2 = await client.post("/api/chat/message", json={
        "chat_id": chat_id,
        "message": "45 відділення Київ",
    })
    assert r2.json()["action"] == "collect_payment"
    pay = await client.post("/api/chat/message", json={"chat_id": chat_id, "message": "liqpay"})
    assert pay.json()["action"] == "payment_created"
    assert "/pay/" in (pay.json().get("data") or {}).get("payment_url", "")
    assert "24000" in pay.json()["response"].replace(" ", "")


@pytest.mark.asyncio
async def test_order_confirm_tak_davayte(client: AsyncClient):
    chat_id = "confirm-tak-1"
    r1 = await client.post("/api/chat/message", json={
        "chat_id": chat_id,
        "message": "Скільки коштує 1000 шт силіконових браслетів 20 мм?",
    })
    assert r1.json()["action"] == "await_order_confirm"
    assert "17000" in r1.json()["response"] or "17" in r1.json()["response"]
    r2 = await client.post("/api/chat/message", json={
        "chat_id": chat_id,
        "message": "Так, давайте",
    })
    assert r2.json()["action"] in ("collect_order_data", "confirm_client_data")
    assert "прізвище" in r2.json()["response"].lower() or "ім" in r2.json()["response"].lower()


@pytest.mark.asyncio
async def test_full_chat_order_to_liqpay(client: AsyncClient):
    chat_id = "oleg-flow-1"

    r1 = await client.post("/api/chat/message", json={
        "chat_id": chat_id,
        "message": "Хочу замовити",
    })
    assert r1.json()["action"] == "collect_order_data"

    r2 = await client.post("/api/chat/message", json={
        "chat_id": chat_id,
        "message": (
            "Прізвище: Петренко\nІм'я: Іван\nТелефон: +380501234567\n"
            "Місто: Київ\nТовар: силіконовий браслет 12мм\n"
            "Кількість: 300\nЛоготип: є"
        ),
    })
    assert r2.status_code == 200
    assert r2.json()["action"] == "collect_delivery_data"
    assert r2.json()["data"]["order_id"] > 0
    assert "нова" in r2.json()["response"].lower() or "відділен" in r2.json()["response"].lower()
    assert "реквізит" not in r2.json()["response"].lower()

    r3 = await client.post("/api/chat/message", json={
        "chat_id": chat_id,
        "message": "1",
    })
    assert "відділення" in r3.json()["response"].lower() or "Прізвище" in r3.json()["response"] or "місто" in r3.json()["response"].lower()

    r4 = await client.post("/api/chat/message", json={
        "chat_id": chat_id,
        "message": (
            "Прізвище: Петренко\nІм'я: Іван\nТелефон: +380501234567\n"
            "Місто: Київ\nВідділення: 12"
        ),
    })
    # після доставки — вибір LiqPay або рахунок
    assert r4.json()["action"] == "collect_payment"
    assert "liqpay" in r4.json()["response"].lower() or "рахунок" in r4.json()["response"].lower()
    pay = await client.post("/api/chat/message", json={"chat_id": chat_id, "message": "liqpay"})
    assert pay.json()["action"] == "payment_created"
    pay_data = pay.json().get("data") or {}
    assert "payment_url" in pay_data
    assert "/pay/" in pay_data["payment_url"]
    assert "3150" in pay.json()["response"] or "liqpay" in pay.json()["response"].lower()


@pytest.mark.asyncio
async def test_chat_address_delivery_liqpay_only(client: AsyncClient):
    chat_id = "oleg-flow-2"

    await client.post("/api/chat/message", json={"chat_id": chat_id, "message": "замовити"})
    await client.post("/api/chat/message", json={
        "chat_id": chat_id,
        "message": (
            "Прізвище: Коваленко\nІм'я: Олена\nТелефон: +380671112233\n"
            "Місто: Львів\nТовар: світловідбиваючий\n"
            "Кількість: 100"
        ),
    })
    await client.post("/api/chat/message", json={"chat_id": chat_id, "message": "3"})
    r = await client.post("/api/chat/message", json={
        "chat_id": chat_id,
        "message": (
            "Прізвище: Коваленко\nІм'я: Олена\nПо батькові: Петрівна\n"
            "Телефон: +380671112233\nМісто: Львів\nАдреса: вул. Свободи, 5"
        ),
    })
    assert r.json()["action"] == "collect_payment"
    pay = await client.post("/api/chat/message", json={"chat_id": chat_id, "message": "liqpay"})
    assert pay.json()["action"] == "payment_created"
    assert "liqpay" in pay.json()["response"].lower()


@pytest.mark.asyncio
async def test_natural_payment_words_accepted(client: AsyncClient):
    """Якщо крок collect_payment ще активний — «на карту» / «ліг пей» → LiqPay."""
    chat_id = "oleg-pay-natural"
    await client.post("/api/chat/message", json={"chat_id": chat_id, "message": "замовити"})
    await client.post("/api/chat/message", json={
        "chat_id": chat_id,
        "message": (
            "Прізвище: Тест\nІм'я: Тест\nТелефон: +380501234567\n"
            "Місто: Київ\nТовар: силіконовий браслет 12мм\nКількість: 300"
        ),
    })
    await client.post("/api/chat/message", json={"chat_id": chat_id, "message": "відділення"})
    # штучно лишаємося на payment, якщо delivery already done — complete warehouse
    await client.post("/api/chat/message", json={
        "chat_id": chat_id,
        "message": "Відділення: 5",
    })
    # рахунок на реквізити — стартує збір даних / пошурку ЄДРПОУ в 1С
    inv = await client.post("/api/chat/message", json={
        "chat_id": chat_id + "-inv",
        "message": "рахунок на реквізити",
    })
    assert inv.json()["action"] in ("collect_invoice_data", "collect_edrpou")
    assert "компані" in inv.json()["response"].lower() or "єдрпоу" in inv.json()["response"].lower()


@pytest.mark.asyncio
async def test_product_hint_remembered_before_order(client: AsyncClient):
    chat_id = "hint-rfid-1"
    await client.post("/api/chat/message", json={
        "chat_id": chat_id,
        "message": "RFID-браслети формату 50–99 1000 штук",
    })
    r = await client.post("/api/chat/message", json={
        "chat_id": chat_id,
        "message": "замовити",
    })
    assert r.json()["action"] == "collect_order_data"
    # товар і кількість вже відомі — одразу прізвище, без повторного «який товар»
    assert "товар" not in r.json()["response"].lower()
    assert "кількість" not in r.json()["response"].lower()
    assert "прізвище" in r.json()["response"].lower()


@pytest.mark.asyncio
async def test_assortment_overview_instead_of_fallback(client: AsyncClient):
    """Запит «які товари / ціни / проконсультуй» дає огляд прайсу, не fallback-цикл."""
    for i, msg in enumerate([
        "Привіт, які у вас є товари напиши та ціну на них",
        "Я не знаю які у вас є товари, проконсультуй",
        "прайс",
    ]):
        r = await client.post("/api/chat/message", json={
            "chat_id": f"assort-{i}",
            "message": msg,
        })
        assert r.status_code == 200
        data = r.json()
        text = data["response"].lower()
        assert data["action"] == "consult"
        assert "який саме товар і кількість" not in text
        assert "силікон" in text
        assert "50 шт" not in text
        assert "17,50" not in data["response"]
        assert "11,50" not in data["response"]


@pytest.mark.asyncio
async def test_min_qty_silicone_10_quotes(client: AsyncClient):
    r = await client.post("/api/chat/message", json={
        "chat_id": "min-qty-1",
        "message": "силіконові браслети 10 штук",
    })
    assert r.json()["action"] == "await_order_confirm"
    assert "50" in r.json()["response"]


@pytest.mark.asyncio
async def test_caps_are_not_silicone_bracelets(client: AsyncClient):
    chat_id = "caps-1"
    ask = await client.post("/api/chat/message", json={
        "chat_id": chat_id, "message": "шапочки яка ціна на них",
    })
    body = ask.json()["response"].lower()
    assert "браслет 12" not in body
    assert "силіконовий браслет" not in body
    assert "шапоч" in body
    assert "400" in ask.json()["response"]
    one = await client.post("/api/chat/message", json={
        "chat_id": chat_id, "message": "Мені треба шапочки для плавання яка вартість 1 штуки",
    })
    t = one.json()["response"].lower()
    assert "силіконовий браслет" not in t
    assert "400" in one.json()["response"]
    assert "1150" not in one.json()["response"]
    hundred = await client.post("/api/chat/message", json={
        "chat_id": chat_id, "message": "шапочки мені 100 штук треба",
    })
    h = hundred.json()["response"].lower()
    assert "силіконовий браслет 12" not in h
    assert "1150" not in hundred.json()["response"]
    assert "шапоч" in h
    assert "300" in hundred.json()["response"]
    assert "× 1 шт" not in hundred.json()["response"]


@pytest.mark.asyncio
async def test_caps_after_assortment_like_screenshot(client: AsyncClient):
    chat_id = "caps-screen-1"
    await client.post("/api/chat/message", json={
        "chat_id": chat_id,
        "message": "можете будь ласка мені тоді написати який у вас є асортимент та який прайс",
    })
    r1 = await client.post("/api/chat/message", json={
        "chat_id": chat_id, "message": "шапочки яка ціна на них",
    })
    assert "400" in r1.json()["response"]
    assert "силіконовий браслет" not in r1.json()["response"].lower()
    r2 = await client.post("/api/chat/message", json={
        "chat_id": chat_id, "message": "а яка вартість?",
    })
    t2 = r2.json()["response"].lower()
    assert "не впевнений" not in t2
    assert "шапоч" in t2 or "400" in r2.json()["response"]
    assert "браслет 12" not in t2
    r3 = await client.post("/api/chat/message", json={
        "chat_id": chat_id, "message": "Мені треба шапочки для плавання яка вартість 1 штуки",
    })
    assert "400" in r3.json()["response"]
    assert "1150" not in r3.json()["response"]
    r4 = await client.post("/api/chat/message", json={
        "chat_id": chat_id, "message": "шапочки мені 100 штук треба",
    })
    assert "300" in r4.json()["response"]
    assert "1150" not in r4.json()["response"]
    assert "силіконовий браслет 12" not in r4.json()["response"].lower()
    assert "× 1 шт" not in r4.json()["response"]


@pytest.mark.asyncio
async def test_offtopic_during_quote_does_not_force_funnel(client: AsyncClient):
    chat_id = "offtopic-1"
    await client.post("/api/chat/message", json={
        "chat_id": chat_id, "message": "силіконові браслети 12 мм 100 штук",
    })
    r = await client.post("/api/chat/message", json={
        "chat_id": chat_id, "message": "що означає слово hello англійською?",
    })
    body = r.json()["response"].lower()
    assert r.json()["action"] == "await_order_confirm"
    assert "оформлюємо?" not in body
    assert "не впевнений, що саме порахувати" not in body


@pytest.mark.asyncio
async def test_rfid_does_not_use_silicone_price(client: AsyncClient):
    r = await client.post("/api/chat/message", json={
        "chat_id": "rfid-no-silicone-1",
        "message": "RFID браслети 1000 штук",
    })
    body = r.json()["response"].replace(" ", "")
    assert "8500" not in body
    assert r.json()["action"] != "await_order_confirm" or "силікон" not in r.json()["response"].lower()


@pytest.mark.asyncio
async def test_manager_handoff_from_chat(client: AsyncClient):
    r = await client.post("/api/chat/message", json={
        "chat_id": "mgr-1",
        "message": "зв'яжіть з менеджером",
    })
    assert r.json()["action"] == "manager_handoff"
    assert r.json().get("data", {}).get("ticket_id")
    assert "менеджер" in r.json()["response"].lower()


@pytest.mark.asyncio
async def test_payment_consult_mentions_invoice(client: AsyncClient):
    r = await client.post("/api/ai/consult", json={
        "question": "Які способи оплати?",
        "chat_id": "pay-consult-1",
    })
    text = r.json()["response"].lower()
    assert "liqpay" in text or "оплат" in text
    assert "немає" not in text or "рахунок" in text


@pytest.mark.asyncio
async def test_delivery_api_rejects_address_without_patronymic(client: AsyncClient):
    await client.post("/api/orders/register-client", json={
        "chat_id": "del-bad",
        "first_name": "Test",
        "phone": "+380501111111",
    })
    resp = await client.post("/api/orders/delivery", json={
        "chat_id": "del-bad",
        "delivery_type": "address",
        "recipient_last_name": "Тест",
        "recipient_first_name": "Тест",
        "recipient_phone": "380501111111",
        "city": "Київ",
        "street_address": "вул. 1",
    })
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_delivery_api_address_ok_and_ttn(client: AsyncClient):
    chat_id = "del-addr"
    await client.post("/api/orders/register-client", json={
        "chat_id": chat_id,
        "first_name": "Олена",
        "phone": "+380671111111",
    })
    order = await client.post("/api/orders/create", json={
        "chat_id": chat_id,
        "product_type": "силіконовий",
        "quantity": 100,
        "total_amount": 1150,
    })
    order_id = order.json()["order_id"]
    del_resp = await client.post("/api/orders/delivery", json={
        "chat_id": chat_id,
        "delivery_type": "address",
        "recipient_last_name": "Коваленко",
        "recipient_first_name": "Олена",
        "recipient_patronymic": "Іванівна",
        "recipient_phone": "380671111111",
        "city": "Київ",
        "street_address": "вул. Хрещатик, 10",
    })
    assert del_resp.status_code == 200
    assert del_resp.json()["delivery_type"] == "address"

    ttn = await client.post(f"/api/orders/{order_id}/ttn")
    assert ttn.status_code == 200
    assert ttn.json()["success"] is True


@pytest.mark.asyncio
async def test_qty_shtuka_keeps_product_and_size(client: AsyncClient):
    chat_id = "shtuka-20mm-1"
    await client.post("/api/chat/message", json={
        "chat_id": chat_id,
        "message": "силіконові браслети з друком лого цікавить 20 мм",
    })
    r = await client.post("/api/chat/message", json={
        "chat_id": chat_id,
        "message": "151 штука потрібна",
    })
    text = r.json()["response"].lower()
    assert r.json()["action"] == "await_order_confirm"
    assert "який саме товар" not in text
    assert "20" in r.json()["response"]
    assert "151" in r.json()["response"]


@pytest.mark.asyncio
async def test_invoice_request_asks_payer_requisites(client: AsyncClient):
    chat = "inv-ask-req-1"
    await client.post("/api/chat/message", json={
        "chat_id": chat,
        "message": "силіконові браслети 12 мм 30 штук",
    })
    r = await client.post("/api/chat/message", json={
        "chat_id": chat,
        "message": "Потрібно підготувати рахунок на оплату.",
    })
    body = r.json()["response"]
    low = body.lower()
    assert r.json()["action"] == "collect_invoice_data"
    assert "реквізити платника" in low
    assert "назва компанії" in low
    assert "єдрпоу" in low
    assert "іпн" in low
    assert "адреса" in low
    assert "pdf" in low
    assert "карткою онлайн" not in low
    assert "готівк" not in low
    again = await client.post("/api/chat/message", json={
        "chat_id": chat,
        "message": "Рахунок будь ласка",
    })
    again_low = again.json()["response"].lower()
    assert again.json()["action"] == "collect_invoice_data"
    assert "карткою онлайн" not in again_low
    assert "реквізити платника" in again_low or "назва компанії" in again_low


@pytest.mark.asyncio
async def test_invoice_from_quote_without_crm_sends_pdf(client: AsyncClient):
    chat_id = "invoice-from-quote-1"
    await client.post("/api/chat/message", json={
        "chat_id": chat_id,
        "message": "силіконові браслети 50 штук",
    })
    start = await client.post("/api/chat/message", json={
        "chat_id": chat_id,
        "message": "рахунок хочу відкрити",
    })
    assert start.json()["action"] == "collect_invoice_data"
    await client.post("/api/chat/message", json={
        "chat_id": chat_id, "message": "ТОВ Тестовий Рахунок",
    })
    ask = await client.post("/api/chat/message", json={
        "chat_id": chat_id,
        "message": "А де я його можу знайти бо не знаю",
    })
    assert ask.json()["action"] == "collect_invoice_data"
    assert "єдрпоу" in ask.json()["response"].lower()
    assert "виписц" in ask.json()["response"].lower() or "8 цифр" in ask.json()["response"]
    await client.post("/api/chat/message", json={
        "chat_id": chat_id, "message": "33445566",
    })
    await client.post("/api/chat/message", json={
        "chat_id": chat_id, "message": "ІПН немає",
    })
    final = await client.post("/api/chat/message", json={
        "chat_id": chat_id, "message": "вулиця Шевченка 65 м. Київ",
    })
    assert final.json()["action"] == "confirm_invoice_data"
    created = await client.post("/api/chat/message", json={
        "chat_id": chat_id, "message": "так, все вірно",
    })
    assert created.json()["action"] == "invoice_created"
    assert "1с" not in created.json()["response"].lower()
    docs = created.json().get("documents") or []
    assert docs and docs[0]["path"]
    from pathlib import Path
    assert Path(docs[0]["path"]).exists()


@pytest.mark.asyncio
async def test_invoice_address_creates_pdf_not_error(client: AsyncClient):
    chat_id = "invoice-crash-1"
    await client.post("/api/chat/message", json={
        "chat_id": chat_id, "message": "силіконові браслети 20 мм 151 штука",
    })
    await client.post("/api/chat/message", json={
        "chat_id": chat_id, "message": "рахунок хочу відкрити",
    })
    await client.post("/api/chat/message", json={
        "chat_id": chat_id, "message": "ТОВ Тесто",
    })
    await client.post("/api/chat/message", json={
        "chat_id": chat_id, "message": "388500355",
    })
    await client.post("/api/chat/message", json={
        "chat_id": chat_id, "message": "немає",
    })
    final = await client.post("/api/chat/message", json={
        "chat_id": chat_id, "message": "м. Київ вулиця Шевченка 0",
    })
    body = final.json()
    assert "не вийшло відповісти" not in body["response"].lower()
    assert body["action"] == "confirm_invoice_data"
    created = await client.post("/api/chat/message", json={
        "chat_id": chat_id, "message": "так, все вірно",
    })
    issued = created.json()
    assert issued["action"] == "invoice_created"
    docs = issued.get("documents") or []
    assert docs and docs[0]["path"]
    from pathlib import Path
    assert Path(docs[0]["path"]).exists()

    again = await client.post("/api/chat/message", json={
        "chat_id": chat_id, "message": "рахунок",
    })
    again_body = again.json()
    assert again_body["action"] == "confirm_saved_requisites"
    assert "такими реквізитами" in again_body["response"].lower()
    assert "іпн" not in again_body["response"].lower()
    resent = await client.post("/api/chat/message", json={
        "chat_id": chat_id, "message": "так",
    })
    assert resent.json()["action"] == "invoice_created"
    assert "не вийшло" not in resent.json()["response"].lower()


@pytest.mark.asyncio
async def test_crm_side_question_does_not_become_name(client: AsyncClient):
    chat_id = "crm-side-q-1"
    await client.post("/api/chat/message", json={
        "chat_id": chat_id, "message": "силіконові браслети 12 мм 100 штук",
    })
    await client.post("/api/chat/message", json={
        "chat_id": chat_id, "message": "так, оформлюємо",
    })
    r = await client.post("/api/chat/message", json={
        "chat_id": chat_id, "message": "а доставка як?",
    })
    body = r.json()["response"].lower()
    assert r.json()["action"] == "collect_order_data"
    assert "ім" in body or "прізвище" in body
    assert "пошт" in body or "самовивіз" in body or "достав" in body
    assert "а доставка як" not in body


@pytest.mark.asyncio
async def test_payment_how_is_not_liqpay(client: AsyncClient):
    chat_id = "pay-how-1"
    await client.post("/api/chat/message", json={"chat_id": chat_id, "message": "замовити"})
    await client.post("/api/chat/message", json={
        "chat_id": chat_id,
        "message": (
            "Прізвище: Тест\nІм'я: Тест\nТелефон: +380501234567\n"
            "Місто: Київ\nТовар: силіконовий браслет 12мм\nКількість: 300"
        ),
    })
    await client.post("/api/chat/message", json={"chat_id": chat_id, "message": "1"})
    await client.post("/api/chat/message", json={
        "chat_id": chat_id,
        "message": (
            "Прізвище: Тест\nІм'я: Тест\nТелефон: +380501234567\n"
            "Місто: Київ\nВідділення: 5"
        ),
    })
    r = await client.post("/api/chat/message", json={
        "chat_id": chat_id, "message": "оплату як?",
    })
    assert r.json()["action"] == "collect_payment"
    body = r.json()["response"].lower()
    assert "карт" in body or "рахунок" in body or "liqpay" in body
    assert "посилання на оплату" not in body


@pytest.mark.asyncio
async def test_quote_answers_side_questions_and_alt_qty(client: AsyncClient):
    chat_id = "side-q-1"
    await client.post("/api/chat/message", json={
        "chat_id": chat_id, "message": "силіконові браслети 12 мм 100 штук",
    })
    pay = await client.post("/api/chat/message", json={
        "chat_id": chat_id, "message": "оплату як робите?",
    })
    body = pay.json()["response"].lower()
    assert "liqpay" in body or "карт" in body or "рахунок" in body or "готівк" in body
    assert pay.json()["action"] == "await_order_confirm"

    faster = await client.post("/api/chat/message", json={
        "chat_id": chat_id, "message": "а можна швидше?",
    })
    fbody = faster.json()["response"].lower()
    assert "термін" in fbody or "швидш" in fbody or "завантаж" in fbody or "менеджер" in fbody

    alt = await client.post("/api/chat/message", json={
        "chat_id": chat_id, "message": "а якщо 200?",
    })
    assert alt.json()["action"] == "await_order_confirm"
    assert "200" in alt.json()["response"]
    compact = alt.json()["response"].replace(" ", "")
    assert "2200" in compact or "2300" in compact


@pytest.mark.asyncio
async def test_answers_question_before_funnel(client: AsyncClient):
    """Питання клієнта важливіше за «Оформлюємо?» / збір імені."""
    chat = "q-first-fabric"
    await client.post("/api/chat/message", json={
        "chat_id": chat, "message": "тканинні браслети",
    })
    r = await client.post("/api/chat/message", json={
        "chat_id": chat,
        "message": "200 шт Який кольори фіксаторів доступні у вас?",
    })
    body = r.json()["response"].lower()
    assert "фіксатор" in body
    assert "чорн" in body or "біл" in body
    assert "4800" in body.replace(" ", "")

    chat2 = "q-first-lanyard-fix"
    await client.post("/api/chat/message", json={
        "chat_id": chat2, "message": "шнурок для бейджа 15 мм 100 штук",
    })
    fix = await client.post("/api/chat/message", json={
        "chat_id": chat2,
        "message": "Прорахунок потрібно на шнурки для бейджа 20 мм, а не 15 мм. Виправте будь ласка.",
    })
    assert fix.json()["action"] == "await_order_confirm"
    fbody = fix.json()["response"]
    assert "20 мм" in fbody
    assert "collect_order" not in fix.json()["action"]
    assert "ім'я" not in fbody.lower() and "прізвище" not in fbody.lower()
    # не лишає дві ширини в кошику після виправлення
    assert "15 мм" not in fbody or fbody.count("15 мм") == 0

    cod = await client.post("/api/chat/message", json={
        "chat_id": "q-cod-1", "message": "можна наложкою на нову пошту?",
    })
    assert "не відправляємо" in cod.json()["response"].lower() or "накладен" in cod.json()["response"].lower()

    lan = await client.post("/api/chat/message", json={
        "chat_id": "q-lan-faq", "message": "які шнурки для бейджів у вас є?",
    })
    assert "10 мм" in lan.json()["response"]
    assert "15 мм" in lan.json()["response"]
    assert "20 мм" in lan.json()["response"]
    assert "карабін" in lan.json()["response"].lower()
    assert "40 мм" not in lan.json()["response"]

    # під час збору імені — спочатку відповідь на питання про кольори, не starting_quote
    chat3 = "q-first-name-colors"
    await client.post("/api/chat/message", json={
        "chat_id": chat3, "message": "силікон 50 шт",
    })
    await client.post("/api/chat/message", json={
        "chat_id": chat3, "message": "так оформлюємо",
    })
    colors = await client.post("/api/chat/message", json={
        "chat_id": chat3, "message": "а які кольори браслетів є?",
    })
    cbody = colors.json()["response"].lower()
    assert colors.json()["action"] == "collect_order_data"
    assert "колір" in cbody
    assert "silicon.com.ua" in cbody
    assert "напишіть тираж" not in cbody
    assert "350 грн" not in cbody

    # терміново ≠ звичайний строк 5–7 днів
    urgent = await client.post("/api/chat/message", json={
        "chat_id": "q-urgent-idle",
        "message": "Чи є можливість виготовити терміново?",
    })
    ubody = urgent.json()["response"].lower()
    assert "×2" in urgent.json()["response"] or "x2" in ubody or "термінов" in ubody
    assert "5–7" not in urgent.json()["response"] and "5-7" not in urgent.json()["response"]

    # світловідбиваючі з кольоровим лого — не 1 колір
    refl = await client.post("/api/chat/message", json={
        "chat_id": "q-refl-color",
        "message": "Яка вартість та термін виготовлення світловідбиваючі браслети 100 шт сині з кольоровим лого?",
    })
    rbody = refl.json()["response"]
    assert "кольоров" in rbody.lower()
    assert "3800" in rbody.replace(" ", "")
    assert "1 колір" not in rbody.lower() or "кольоровий" in rbody.lower()


@pytest.mark.asyncio
async def test_named_product_asks_qty_not_fallback(client: AsyncClient):
    """Скрін: товар без тиражу не має давати «не впевнений, що порахувати»."""
    chat_id = "clarify-silicone-20"
    r1 = await client.post("/api/chat/message", json={
        "chat_id": chat_id,
        "message": "силіконові браслети з друком лого давайте 20 мм",
    })
    t1 = r1.json()["response"].lower()
    assert "не впевнений" not in t1
    assert "20" in r1.json()["response"]
    assert "кількість" in t1 or "тираж" in t1

    r2 = await client.post("/api/chat/message", json={
        "chat_id": chat_id,
        "message": "силіконові браслети",
    })
    assert "не впевнений" not in r2.json()["response"].lower()

    r3 = await client.post("/api/chat/message", json={
        "chat_id": chat_id,
        "message": "Рахуй еко-чашки",
    })
    t3 = r3.json()["response"].lower()
    assert "не впевнений" not in t3
    assert "чашк" in t3
    assert "тираж" in t3 or "кількість" in t3


@pytest.mark.asyncio
async def test_eco_cups_keeps_qty_and_answers_nudge(client: AsyncClient):
    chat_id = "eco-loop-1"
    r1 = await client.post("/api/chat/message", json={
        "chat_id": chat_id, "message": "Мене цікавлять еко-чашки",
    })
    assert "тираж" in r1.json()["response"].lower()
    assert "точною ціну" not in r1.json()["response"].lower()

    r2 = await client.post("/api/chat/message", json={
        "chat_id": chat_id, "message": "Мені потрібно 101 штука",
    })
    t2 = r2.json()["response"].lower()
    assert "101" in r2.json()["response"]
    assert "чашк" in t2
    assert "сітки" not in t2
    assert "браслет 12" not in t2

    r3 = await client.post("/api/chat/message", json={
        "chat_id": chat_id, "message": "Мені потрібно порахувати, порахуйте",
    })
    t3 = r3.json()["response"].lower()
    assert "тираж" not in t3
    assert "101" in r3.json()["response"]
    assert "чашк" in t3

    r4 = await client.post("/api/chat/message", json={
        "chat_id": chat_id, "message": "???",
    })
    t4 = r4.json()["response"].lower()
    assert "тут" in t4
    assert "openai" not in t4
    assert "чашк" in t4 or "101" in r4.json()["response"]


@pytest.mark.asyncio
async def test_add_second_product_after_quote(client: AsyncClient):
    chat_id = "add-second-1"
    await client.post("/api/chat/message", json={
        "chat_id": chat_id,
        "message": "силіконові браслети 50 штук",
    })
    r = await client.post("/api/chat/message", json={
        "chat_id": chat_id,
        "message": "ще світловідбиваючі 100 штук",
    })
    assert r.json()["action"] == "await_order_confirm"
    body = r.json()["response"].lower()
    assert "силікон" in body
    assert "світловідб" in body


@pytest.mark.asyncio
async def test_remove_item_from_cart_list(client: AsyncClient):
    """«прибери Slap із списку» — зняти позицію, не питати ім'я."""
    chat = "remove-slap-1"
    await client.post("/api/chat/message", json={
        "chat_id": chat, "message": "slap браслети 100 штук",
    })
    await client.post("/api/chat/message", json={
        "chat_id": chat, "message": "ще шапочки для плавання 10 шт 4+4",
    })
    r = await client.post("/api/chat/message", json={
        "chat_id": chat,
        "message": "Я передумав прибери Slap-браслети із списку",
    })
    body = r.json()["response"]
    low = body.lower()
    assert r.json()["action"] == "await_order_confirm"
    assert "прибрав" in low
    assert "slap" in low or "слеп" in low
    assert "шапоч" in low
    assert "ім'я" not in low and "прізвище" not in low
    assert "18000" not in body.replace(" ", "")
    # slap більше не в залишку як окремий рядок списку
    assert "залишилось" in low
    # повторне підтвердження оформлення — вже без slap
    confirm = await client.post("/api/chat/message", json={
        "chat_id": chat, "message": "так оформлюємо",
    })
    cbody = confirm.json()["response"].lower()
    assert "ім'я" in cbody or "прізвищ" in cbody


@pytest.mark.asyncio
async def test_slap_answers_1_and_4_print_colors(client: AsyncClient):
    """Slap: 1 і 4 кольори друку — обидва варіанти, не лише 1 колір."""
    chat = "slap-colors-1"
    r1 = await client.post("/api/chat/message", json={
        "chat_id": chat,
        "message": (
            "Яка вартість та термін виготовлення силіконові слеп браслети "
            "з логотипом 1 колір та 4 кольори?"
        ),
    })
    t1 = r1.json()["response"]
    assert "65" in t1 and "95" in t1
    assert "1 колір" in t1 and "4 кольор" in t1
    assert "silicon.com.ua/silikonovi-slep-braslety" in t1

    r2 = await client.post("/api/chat/message", json={
        "chat_id": chat, "message": "100 штук",
    })
    t2 = r2.json()["response"]
    # 100 шт: 60 (1 кол) і 90 (4 кол)
    assert "60" in t2 and "90" in t2
    assert "1 колір" in t2 and "4 кольор" in t2

    r3 = await client.post("/api/chat/message", json={
        "chat_id": "slap-colors-4only",
        "message": "Яка вартість виготовлення силіконові слеп браслети з друком лого 4 кольори?",
    })
    t3 = r3.json()["response"]
    assert "95" in t3
    assert "1 колір" in t3 or "4 кольор" in t3


@pytest.mark.asyncio
async def test_add_product_russian_does_not_loop(client: AsyncClient):
    chat_id = "add-ru-1"
    q = await client.post("/api/chat/message", json={
        "chat_id": chat_id, "message": "силіконові браслети 12 мм 100 штук",
    })
    assert q.json()["action"] == "await_order_confirm"
    r = await client.post("/api/chat/message", json={
        "chat_id": chat_id, "message": "хочу добавить світловідбиваючі",
    })
    t = r.json()["response"].lower()
    assert r.json()["action"] == "await_order_confirm"
    assert "кількість" in t or "скільки" in t
    assert "що порахувати" not in t


@pytest.mark.asyncio
async def test_i_paid_does_not_ask_screenshot(client: AsyncClient):
    chat_id = "paid-nudge-1"
    r = await client.post("/api/chat/message", json={
        "chat_id": chat_id, "message": "я оплатив",
    })
    t = r.json()["response"].lower()
    assert "скрін" not in t
    assert "чек" not in t
    assert "не бачу" in t or "оплат" in t


@pytest.mark.asyncio
async def test_where_is_edrpou_answers_instead_of_looping(client: AsyncClient):
    r = await client.post("/api/chat/message", json={
        "chat_id": "edrpou-where-idle",
        "message": "де знайти єдрпоу",
    })
    body = r.json()["response"].lower()
    assert "виписц" in body or "договор" in body
    assert "потрібен код єдрпоу — 8 або 10" not in body


@pytest.mark.asyncio
async def test_lookup_edrpou_exits_on_where_question(client: AsyncClient):
    start = await client.post("/api/chat/message", json={
        "chat_id": "edrpou-lookup-exit",
        "message": "перевір єдрпоу",
    })
    assert start.json()["action"] == "collect_edrpou"
    r = await client.post("/api/chat/message", json={
        "chat_id": "edrpou-lookup-exit",
        "message": "а де його знайти",
    })
    body = r.json()["response"].lower()
    assert r.json()["action"] == "collect_edrpou"
    assert "виписц" in body or "договор" in body
    assert "потрібен код єдрпоу — 8 або 10" not in body
    digits = await client.post("/api/chat/message", json={
        "chat_id": "edrpou-lookup-exit",
        "message": "12345678",
    })
    assert digits.json()["action"] in ("edrpou_found", "edrpou_not_found")


@pytest.mark.asyncio
async def test_tak_vse_pidkhodyt_starts_order(client: AsyncClient):
    chat_id = "tak-vse-1"
    await client.post("/api/chat/message", json={
        "chat_id": chat_id, "message": "силіконові браслети 5 мм 105 штук",
    })
    r = await client.post("/api/chat/message", json={
        "chat_id": chat_id, "message": "Так все підходить",
    })
    assert r.json()["action"] in ("collect_order_data", "confirm_client_data")
    assert "оформлюємо?" not in r.json()["response"].lower()
    assert "ім" in r.json()["response"].lower() or "актуальн" in r.json()["response"].lower()


@pytest.mark.asyncio
async def test_tak_aktualni_does_not_reset_profile(client: AsyncClient):
    chat_id = "tak-aktual-1"
    await client.post("/api/chat/message", json={
        "chat_id": chat_id, "message": "силіконові браслети 100 штук",
    })
    await client.post("/api/chat/message", json={"chat_id": chat_id, "message": "так"})
    await client.post("/api/chat/message", json={"chat_id": chat_id, "message": "Владислав Випаріна"})
    await client.post("/api/chat/message", json={"chat_id": chat_id, "message": "0961730098"})
    await client.post("/api/chat/message", json={
        "chat_id": chat_id, "message": "відділення 30 Вінниця",
    })
    await client.post("/api/chat/message", json={"chat_id": chat_id, "message": "liqpay"})

    await client.post("/api/chat/message", json={
        "chat_id": chat_id, "message": "силіконові браслети 5 мм 105 штук",
    })
    await client.post("/api/chat/message", json={
        "chat_id": chat_id, "message": "Так все підходить",
    })
    r = await client.post("/api/chat/message", json={
        "chat_id": chat_id, "message": "Так актуальні",
    })
    body = r.json()["response"].lower()
    assert "введемо нові" not in body
    assert "ім'я та прізвище одним" not in body
    assert r.json()["action"] in (
        "collect_delivery_data", "collect_payment", "payment_created", "collect_order_data"
    )

    again = await client.post("/api/chat/message", json={
        "chat_id": chat_id, "message": "Я ж пишу що ті дані актуальні",
    })
    again_body = again.json()["response"].lower()
    assert "не впевнений, що саме порахувати" not in again_body
    assert "введемо нові" not in again_body


@pytest.mark.asyncio
async def test_za_rahunkom_and_na_poshtu(client: AsyncClient):
    chat_id = "pay-poshta-1"
    await client.post("/api/chat/message", json={
        "chat_id": chat_id, "message": "силіконові браслети 50 штук",
    })
    await client.post("/api/chat/message", json={"chat_id": chat_id, "message": "так"})
    await client.post("/api/chat/message", json={"chat_id": chat_id, "message": "Олег Тест"})
    await client.post("/api/chat/message", json={"chat_id": chat_id, "message": "0501112233"})
    dtype = await client.post("/api/chat/message", json={
        "chat_id": chat_id, "message": "на пошту",
    })
    body = dtype.json()["response"].lower()
    assert "на відділення, у поштомат чи за адресою зручніше" not in body
    assert dtype.json()["action"] == "collect_delivery_data"
    city = await client.post("/api/chat/message", json={
        "chat_id": chat_id, "message": "Вінниця",
    })
    assert city.json()["action"] == "collect_delivery_data"
    assert "карткою" not in city.json()["response"].lower()
    assert "відділен" in city.json()["response"].lower() or "номер" in city.json()["response"].lower()
    await client.post("/api/chat/message", json={
        "chat_id": chat_id, "message": "30",
    })
    pay = await client.post("/api/chat/message", json={
        "chat_id": chat_id, "message": "за рахунком",
    })
    assert pay.json()["action"] == "collect_invoice_data"
    assert "як зручніше оплатити" not in pay.json()["response"].lower()
    assert "єдрпоу" in pay.json()["response"].lower() or "назв" in pay.json()["response"].lower()


def test_live_phrasing_helpers():
    from app.services.conversation_flow import (
        _asks_if_bot,
        _is_order_confirm,
        _says_already_paid,
    )

    assert _says_already_paid("я вже кинув гроші")
    assert _says_already_paid("гроші скинув щойно")
    assert _says_already_paid("перевела на картку")
    assert _says_already_paid("оплатив")
    assert not _says_already_paid("як оплатити")
    assert not _says_already_paid("краще карткою")

    assert _asks_if_bot("ти бот?")
    assert _asks_if_bot("це аі агент?")
    assert _asks_if_bot("ви робот чи людина")
    assert _asks_if_bot("ти людина?")
    assert not _asks_if_bot("зв'яжіть з менеджером")

    assert _is_order_confirm("го можна")
    assert _is_order_confirm("давайте тоді")
    assert _is_order_confirm("так все підходить")
    assert _is_order_confirm("тоді давайте")
    assert _is_order_confirm("можемо оформлювати")
    assert _is_order_confirm("Та ні більше нічого не потрібно, можемо оформлювати")
    assert _is_order_confirm("я ж написав вище так")
    assert _is_order_confirm("більше не треба товару я ж написав шапочки замовляю і все")
    assert not _is_order_confirm("ще світловідбиваючі 500 штук")

    from app.services.conversation_flow import (
        CRM_FIELD_QUESTIONS,
        _ai_live,
        _declines_more_items,
        _individual_quote_reply,
        _presence_reply,
        _wants_add_to_cart,
    )

    assert _ai_live() is False
    assert "Дмитр" not in _individual_quote_reply("RFID", "100")
    assert "менеджер" in _individual_quote_reply("RFID", "100").lower()
    assert "Дмитр" not in _presence_reply(
        {"product_type": "RFID", "quantity": "10", "individual_quoted": True}
    )
    assert "зручніш" not in CRM_FIELD_QUESTIONS["phone"]
    assert "напишіть свій номер телефону" in CRM_FIELD_QUESTIONS["phone"].lower()
    assert _declines_more_items("більше нічого не потрібно")
    assert not _wants_add_to_cart("я ж написав вище так")
    assert _wants_add_to_cart("ще світловідбиваючі 500 штук")
    assert _wants_add_to_cart("хочу ще світловідбиваючі браслети")


@pytest.mark.asyncio
async def test_ai_live_answers_question_instead_of_funnel(client: AsyncClient, monkeypatch):
    from app.services import conversation_flow as cf

    monkeypatch.setattr(cf, "_ai_live", lambda: True)

    async def fake_consult(db, text, chat_id=None):
        return {
            "response": f"GPT: {text}",
            "source": "openai",
            "escalate_to_manager": False,
        }

    monkeypatch.setattr(cf.consultant, "consult", fake_consult)
    r = await client.post("/api/chat/message", json={
        "chat_id": "ai-first-1",
        "message": "а що за шапочки",
    })
    assert r.json()["response"] == "GPT: а що за шапочки"
    assert "тираж" not in r.json()["response"].lower()
    unknown = await client.post("/api/chat/message", json={
        "chat_id": "ai-first-2",
        "message": "скільки важить місяць Юпітера?",
    })
    assert unknown.json()["response"].startswith("GPT:")


@pytest.mark.asyncio
async def test_denies_being_a_bot(client: AsyncClient):
    r = await client.post("/api/chat/message", json={
        "chat_id": "bot-ask-1", "message": "ти бот?",
    })
    body = r.json()["response"].lower()
    assert r.json()["action"] != "manager_handoff"
    assert r.json()["response"].strip() == "Ні. Я реальний менеджер."
    assert "штучн" not in body
    assert "модель" not in body
    assert "агент" not in body
    assert "олег" not in body

    r2 = await client.post("/api/chat/message", json={
        "chat_id": "bot-ask-2", "message": "це аі агент?",
    })
    assert r2.json()["action"] != "manager_handoff"
    assert r2.json()["response"].strip() == "Ні. Я реальний менеджер."


@pytest.mark.asyncio
async def test_natural_confirm_and_exit_question(client: AsyncClient):
    chat_id = "natural-1"
    q = await client.post("/api/chat/message", json={
        "chat_id": chat_id, "message": "силіконові браслети 12 мм 100 штук",
    })
    assert q.json()["action"] == "await_order_confirm"
    days = await client.post("/api/chat/message", json={
        "chat_id": chat_id, "message": "а скільки днів робити?",
    })
    t = days.json()["response"].lower()
    assert "14" in days.json()["response"] or "дн" in t or "строк" in t
    go = await client.post("/api/chat/message", json={
        "chat_id": chat_id, "message": "давайте тоді",
    })
    assert go.json()["action"] in ("collect_order_data", "confirm_client_data")
    assert "оформлюємо цю позицію, чи ще" not in go.json()["response"].lower()


@pytest.mark.asyncio
async def test_full_natural_path_card_and_i_paid(client: AsyncClient):
    chat_id = "walk-pay-1"
    await client.post("/api/chat/message", json={
        "chat_id": chat_id, "message": "хочу силікон 12 мм 100 штук",
    })
    await client.post("/api/chat/message", json={
        "chat_id": chat_id, "message": "го можна",
    })
    await client.post("/api/chat/message", json={
        "chat_id": chat_id, "message": "Іван Петренко",
    })
    await client.post("/api/chat/message", json={
        "chat_id": chat_id, "message": "0501112233",
    })
    await client.post("/api/chat/message", json={
        "chat_id": chat_id, "message": "на відділення хай буде",
    })
    await client.post("/api/chat/message", json={
        "chat_id": chat_id, "message": "Київ 12",
    })
    pay = await client.post("/api/chat/message", json={
        "chat_id": chat_id, "message": "краще карткою",
    })
    assert pay.json()["action"] == "payment_created"
    pending = await client.post("/api/chat/message", json={
        "chat_id": chat_id, "message": "я вже кинув гроші",
    })
    assert pending.json()["action"] in ("payment_pending", "payment_info")
    assert "не бачу" in pending.json()["response"].lower()

    from app.services.liqpay import LiqPayService
    import base64, json
    url = pay.json()["data"]["payment_url"]
    payment_id = int(url.rstrip("/").split("/")[-1])
    # webhook як жива оплата
    # витягнемо liqpay_order_id з відповіді створення
    liqpay_order_id = pay.json()["data"]["liqpay_order_id"]
    service = LiqPayService()
    payload = {"order_id": liqpay_order_id, "status": "sandbox", "amount": 1150}
    data_b64 = base64.b64encode(json.dumps(payload).encode()).decode()
    signature = service._sign(data_b64)
    wh = await client.post("/api/webhooks/liqpay", data={"data": data_b64, "signature": signature})
    assert wh.json()["success"] is True
    done = await client.post("/api/chat/message", json={
        "chat_id": chat_id, "message": "оплатив",
    })
    assert done.json()["action"] == "payment_confirmed"
    assert "бачу" in done.json()["response"].lower()


@pytest.mark.asyncio
async def test_swim_cap_confirm_without_loop(client: AsyncClient):
    chat_id = "caps-loop-1"
    await client.post("/api/chat/message", json={
        "chat_id": chat_id, "message": "А що за шапочки",
    })
    mat = await client.post("/api/chat/message", json={
        "chat_id": chat_id, "message": "А з якого вона матеріалу?",
    })
    body = mat.json()["response"].lower()
    assert "відкритих джерел" not in body
    assert "naurok" not in body
    assert "силікон" in body
    quote = await client.post("/api/chat/message", json={
        "chat_id": chat_id, "message": "134 штуки",
    })
    assert quote.json()["action"] == "await_order_confirm"
    go = await client.post("/api/chat/message", json={
        "chat_id": chat_id,
        "message": "Та ні більше нічого не потрібно, можемо оформлювати",
    })
    assert go.json()["action"] in ("collect_order_data", "confirm_client_data")
    assert "який товар додати" not in go.json()["response"].lower()
    name = await client.post("/api/chat/message", json={
        "chat_id": chat_id, "message": "Тест Тестович",
    })
    assert "телефон" in name.json()["response"].lower()
    assert "зручніш" not in name.json()["response"].lower()


@pytest.mark.asyncio
async def test_restating_confirm_not_add_to_cart(client: AsyncClient):
    chat_id = "caps-loop-2"
    await client.post("/api/chat/message", json={
        "chat_id": chat_id, "message": "шапочка для плавання 134 штуки",
    })
    r = await client.post("/api/chat/message", json={
        "chat_id": chat_id, "message": "я ж написав вище так",
    })
    assert r.json()["action"] in ("collect_order_data", "confirm_client_data")
    assert "який товар додати" not in r.json()["response"].lower()


@pytest.mark.asyncio
async def test_payment_question_and_after_pay_contact(client: AsyncClient):
    chat_id = "pay-q-1"
    await client.post("/api/chat/message", json={
        "chat_id": chat_id, "message": "силікон 12 мм 100 штук",
    })
    await client.post("/api/chat/message", json={"chat_id": chat_id, "message": "так"})
    await client.post("/api/chat/message", json={"chat_id": chat_id, "message": "Іван Петренко"})
    await client.post("/api/chat/message", json={"chat_id": chat_id, "message": "0501112233"})
    await client.post("/api/chat/message", json={"chat_id": chat_id, "message": "на відділення"})
    await client.post("/api/chat/message", json={"chat_id": chat_id, "message": "Київ 12"})
    ask = await client.post("/api/chat/message", json={
        "chat_id": chat_id, "message": "оплата?",
    })
    assert ask.json()["action"] == "collect_payment"
    assert "не впевнений" not in ask.json()["response"].lower()
    assert "карт" in ask.json()["response"].lower() or "liqpay" in ask.json()["response"].lower()
    pay = await client.post("/api/chat/message", json={
        "chat_id": chat_id, "message": "карткою",
    })
    assert pay.json()["action"] == "payment_created"
    who = await client.post("/api/chat/message", json={
        "chat_id": chat_id, "message": "після оплати зі мною хтось звяжеться?",
    })
    assert "не впевнений" not in who.json()["response"].lower()
    assert "чат" in who.json()["response"].lower() or "напиш" in who.json()["response"].lower()
    broken = await client.post("/api/chat/message", json={
        "chat_id": chat_id, "message": "В мене посилання на оплату не працює",
    })
    assert "не впевнений" not in broken.json()["response"].lower()
    assert "рахунок" in broken.json()["response"].lower() or "посилан" in broken.json()["response"].lower()


@pytest.mark.asyncio
async def test_bracelet_term_and_price_question(client: AsyncClient):
    r = await client.post("/api/chat/message", json={
        "chat_id": "bracelet-term-1",
        "message": "Яка вартість та термін виготовлення браслетів?",
    })
    text = r.json()["response"]
    lower = text.lower()
    assert "14 робоч" not in lower
    assert "5" in text and "7" in text
    assert "робоч" in lower
    assert "індивідуаль" in lower or "макет" in lower


@pytest.mark.asyncio
async def test_rfid_what_is_it_explains_not_qty(client: AsyncClient):
    r = await client.post("/api/chat/message", json={
        "chat_id": "rfid-what-1",
        "message": "А що таке RFID?",
    })
    t = r.json()["response"].lower()
    assert "який тираж" not in t
    assert "говоримо про" not in t
    assert "чіп" in t or "радіо" in t or "зчитувач" in t or "доступ" in t
    r2 = await client.post("/api/chat/message", json={
        "chat_id": "rfid-what-1",
        "message": "А що означає саме RFID бо я не знаю",
    })
    t2 = r2.json()["response"].lower()
    assert "який тираж" not in t2
    assert "чіп" in t2 or "радіо" in t2 or "зчитувач" in t2


@pytest.mark.asyncio
async def test_catalog_after_rfid_does_not_repeat_individual(client: AsyncClient):
    chat_id = "rfid-catalog-1"
    await client.post("/api/chat/message", json={
        "chat_id": chat_id, "message": "А що таке RFID?",
    })
    await client.post("/api/chat/message", json={
        "chat_id": chat_id, "message": "104 штуки по якій ціні?",
    })
    listing = await client.post("/api/chat/message", json={
        "chat_id": chat_id,
        "message": "Можеш мені написати список товарів які є у вас і прайс відразу на них",
    })
    body = listing.json()["response"].lower()
    assert "104" not in body
    assert "еко-чаш" in body or "асортимент" in body or "силікон" in body or "чашк" in body


@pytest.mark.asyncio
async def test_switch_to_cups_from_not_bracelet(client: AsyncClient):
    chat_id = "switch-cups-1"
    await client.post("/api/chat/message", json={
        "chat_id": chat_id, "message": "RFID 104 штуки",
    })
    r = await client.post("/api/chat/message", json={
        "chat_id": chat_id,
        "message": "Мені не браслет потрібен а еко-чашки",
    })
    t = r.json()["response"].lower()
    assert "rfid" not in t or "чашк" in t
    price = await client.post("/api/chat/message", json={
        "chat_id": chat_id, "message": "Яка ціна на них?",
    })
    p = price.json()["response"].lower()
    assert "чашк" in p
    assert "індивідуально" not in p or "350" in price.json()["response"]


@pytest.mark.asyncio
async def test_invoice_blob_asks_edrpou_not_company_again(client: AsyncClient):
    chat_id = "invoice-blob-1"
    await client.post("/api/chat/message", json={
        "chat_id": chat_id, "message": "еко-чашки 1 штука",
    })
    await client.post("/api/chat/message", json={"chat_id": chat_id, "message": "так"})
    await client.post("/api/chat/message", json={"chat_id": chat_id, "message": "Тест Тестович"})
    await client.post("/api/chat/message", json={"chat_id": chat_id, "message": "0980184845"})
    await client.post("/api/chat/message", json={
        "chat_id": chat_id, "message": "відділення 46 Київ",
    })
    await client.post("/api/chat/message", json={"chat_id": chat_id, "message": "рахунок"})
    blob = await client.post("/api/chat/message", json={
        "chat_id": chat_id,
        "message": "Тов тест\nіпн: 383565674\nмісто Київ вулиця Шевченка 24",
    })
    t = blob.json()["response"].lower()
    assert "назву організації" not in t
    assert "єдрпоу" in t
    again = await client.post("/api/chat/message", json={
        "chat_id": chat_id, "message": "Вище написав",
    })
    a = again.json()["response"].lower()
    assert "вище написав" not in a
    assert "єдрпоу" in a


@pytest.mark.asyncio
async def test_invoice_ready_copy_is_human(client: AsyncClient):
    chat_id = "invoice-copy-1"
    await client.post("/api/chat/message", json={
        "chat_id": chat_id, "message": "еко-чашки 1 штука",
    })
    await client.post("/api/chat/message", json={"chat_id": chat_id, "message": "так"})
    await client.post("/api/chat/message", json={"chat_id": chat_id, "message": "Тест Тестович"})
    await client.post("/api/chat/message", json={"chat_id": chat_id, "message": "0980184845"})
    await client.post("/api/chat/message", json={
        "chat_id": chat_id, "message": "відділення 46 Київ",
    })
    await client.post("/api/chat/message", json={"chat_id": chat_id, "message": "рахунок"})
    await client.post("/api/chat/message", json={
        "chat_id": chat_id, "message": "ТОВ Тест Копія",
    })
    await client.post("/api/chat/message", json={"chat_id": chat_id, "message": "12345678"})
    await client.post("/api/chat/message", json={"chat_id": chat_id, "message": "немає"})
    final = await client.post("/api/chat/message", json={
        "chat_id": chat_id, "message": "м. Київ, вул. Шевченка, 24",
    })
    assert final.json()["action"] == "confirm_invoice_data"
    created = await client.post("/api/chat/message", json={
        "chat_id": chat_id, "message": "так, все вірно",
    })
    assert created.json()["action"] == "invoice_created"
    text = created.json()["response"]
    assert "готовий — PDF у вкладенні" not in text
    assert "напишіть «liqpay»" not in text.lower()
    assert "скрін" in text.lower()
    leftover = await client.post("/api/chat/message", json={
        "chat_id": chat_id, "message": "Київ",
    })
    assert "лишаємо" not in leftover.json()["response"].lower()
    assert "скрін" in leftover.json()["response"].lower()



@pytest.mark.asyncio
async def test_soft_hologram_escalate_keeps_ai_active(client: AsyncClient):
    """Після голограм бот НЕ має мовчати на наступні питання (human_mode лише при явному запиті менеджера)."""
    chat_id = "soft-hologram-1"
    await client.post("/api/chat/message", json={
        "chat_id": chat_id,
        "message": "Яка вартість виготовлення голограм з логотипом?",
    })
    h = await client.post("/api/chat/message", json={
        "chat_id": chat_id,
        "message": "100 штук, 20 мм коло з друком логотипу чорним кольором",
    })
    data = h.json()
    assert data["action"] == "manager_handoff"
    text = data["response"]
    assert "індивідуально" in text.lower() or "менеджер" in text.lower()
    assert "[ESCALATE]" not in text
    # не дублювати коротку фразу поверх розгорнутої передачі
    assert not (
        "передаю менеджеру" in text.lower()
        and "Передам менеджеру — він дасть відповідь" in text
    )
    follow = await client.post("/api/chat/message", json={
        "chat_id": chat_id,
        "message": "Яка вартість резинок для фітнесу 10 штук?",
    })
    assert follow.json()["action"] != "human_mode"
    assert follow.json()["response"]
    assert "239" in follow.json()["response"] or "фітнес" in follow.json()["response"].lower()


@pytest.mark.asyncio
async def test_carabiners_guide_reply(client: AsyncClient):
    r = await client.post("/api/chat/message", json={
        "chat_id": "carabiner-1",
        "message": "Можна у вас придбати карабіни?",
    })
    text = r.json()["response"]
    assert r.json()["action"] != "human_mode"
    assert "karabiny-metalevi" in text
    assert "кількість" in text.lower() or "вибрати" in text.lower()


@pytest.mark.asyncio
async def test_baking_mat_asks_color(client: AsyncClient):
    r = await client.post("/api/chat/message", json={
        "chat_id": "mat-1",
        "message": "Можна придбати 5 килимків для випічки?",
    })
    text = r.json()["response"].lower()
    assert r.json()["action"] != "human_mode"
    assert "колір" in text
    assert "silikonovij-kilimok-dlya-vypichki" in r.json()["response"]


@pytest.mark.asyncio
async def test_fitness_bands_price(client: AsyncClient):
    r = await client.post("/api/chat/message", json={
        "chat_id": "fitness-1",
        "message": "Яка вартість резинок для фітнесу 10 штук?",
    })
    text = r.json()["response"]
    assert "239" in text
    assert "2390" in text.replace(" ", "")
    brand = await client.post("/api/chat/message", json={
        "chat_id": "fitness-2",
        "message": "Чи можна друк логотипу на резинках для фітнесу?",
    })
    bt = brand.json()["response"].lower()
    assert "макет" in bt
    assert "7" in brand.json()["response"] and "9" in brand.json()["response"]


@pytest.mark.asyncio
async def test_explicit_manager_still_mutes_bot(client: AsyncClient):
    await client.post("/api/chat/message", json={
        "chat_id": "mute-explicit-1",
        "message": "зв'яжіть з менеджером",
    })
    resp = await client.post("/api/chat/message", json={
        "chat_id": "mute-explicit-1",
        "message": "А яка ціна силікону?",
    })
    assert resp.json()["action"] == "human_mode"
    assert resp.json()["response"] == ""


@pytest.mark.asyncio
async def test_tyvek_terms_not_silicone_template(client: AsyncClient):
    r = await client.post("/api/chat/message", json={
        "chat_id": "tyvek-terms-1",
        "message": "Яка вартість та термін виготовлення контрольні браслети tyvek з кольоровим і одноколірним лого 100 шт?",
    })
    text = r.json()["response"].lower()
    assert "силіконових браслетів" not in text
    assert "2.85" in r.json()["response"] or "2,85" in r.json()["response"] or "tyvek" in text or "контрольн" in text


@pytest.mark.asyncio
async def test_fabric_terms_not_silicone(client: AsyncClient):
    r = await client.post("/api/chat/message", json={
        "chat_id": "fabric-terms-1",
        "message": "Яка вартість та термін виготовлення тканинних браслетів?",
    })
    text = r.json()["response"].lower()
    assert "силіконових браслетів з одноколірним" not in text
    assert "тканин" in text or "15 мм" in text or "фіксатор" in text


@pytest.mark.asyncio
async def test_lanyard_general_faq_in_chat(client: AsyncClient):
    r = await client.post("/api/chat/message", json={
        "chat_id": "lanyard-faq-1",
        "message": "Яка вартість шнурків для бейджа?",
    })
    text = r.json()["response"]
    assert "гладенька" in text.lower()
    assert "репсова" in text.lower()
    assert "40 мм" in text
    assert "10 мм" not in text


@pytest.mark.asyncio
async def test_cart_keeps_silicone_when_correcting_lanyard(client: AsyncClient):
    chat = "cart-keep-1"
    await client.post("/api/chat/message", json={
        "chat_id": chat, "message": "силіконові браслети 12 мм 10 шт",
    })
    await client.post("/api/chat/message", json={
        "chat_id": chat, "message": "ще шнурок для бейджа 15 мм 100 шт",
    })
    fix = await client.post("/api/chat/message", json={
        "chat_id": chat,
        "message": "Прорахунок потрібно на шнурки для бейджа 20 мм, а не 15 мм. Виправте.",
    })
    body = fix.json()["response"].lower()
    assert "20 мм" in body
    assert "силікон" in body or "12 мм" in body


@pytest.mark.asyncio
async def test_cups_chat_both_sizes_and_manager_note(client: AsyncClient):
    """Скрін 13: 350+450 мл і 1/4 колір — обидва розміри + note про макет."""
    r = await client.post("/api/chat/message", json={
        "chat_id": "cups-both-1",
        "message": (
            "Яка вартість виготовлення з брендуванням екочашки 350 мл та 450 мл "
            "з логотипом 1 колір та з логотипом 4 кольори. Кількість 50 штук."
        ),
    })
    text = r.json()["response"]
    assert "350" in text and "450" in text
    assert "6000" in text.replace(" ", "") or "120" in text
    assert "макет" in text.lower()


@pytest.mark.asyncio
async def test_tyvek_chat_both_print_variants(client: AsyncClient):
    r = await client.post("/api/chat/message", json={
        "chat_id": "tyvek-both-1",
        "message": (
            "Яка вартість контрольні браслети з багатоколірним "
            "та одноколірним лого 100 шт?"
        ),
    })
    text = r.json()["response"].lower()
    assert "1 колір" in text or "одноколір" in text or "2.85" in text
    assert "кольор" in text and ("3.4" in text or "кольоровий" in text)


@pytest.mark.asyncio
async def test_payment_description_lists_cart_lines(client: AsyncClient):
    chat = "pay-lines-1"
    await client.post("/api/chat/message", json={
        "chat_id": chat, "message": "силіконові браслети 12 мм 10 шт",
    })
    await client.post("/api/chat/message", json={
        "chat_id": chat, "message": "ще шнурок для бейджа 15 мм 100 шт",
    })
    await client.post("/api/chat/message", json={"chat_id": chat, "message": "оформлюємо"})
    await client.post("/api/chat/message", json={"chat_id": chat, "message": "Іван Тест"})
    await client.post("/api/chat/message", json={"chat_id": chat, "message": "0501112233"})
    await client.post("/api/chat/message", json={
        "chat_id": chat, "message": "Новою поштою у відділення. Київ, відділення 1",
    })
    pay = await client.post("/api/chat/message", json={"chat_id": chat, "message": "карткою"})
    body = pay.json()["response"]
    assert pay.json()["action"] == "payment_created"
    assert "Силіконовий" in body or "силікон" in body.lower()
    assert "Шнурок" in body or "шнурок" in body.lower()
    assert "15 мм" in body


def test_split_qty_keeps_cup_sizes():
    from app.services.conversation_flow import _split_qty_and_question

    qty, q = _split_qty_and_question(
        "екочашки 350 мл та 450 мл 1 колір. Кількість 50 штук."
    )
    assert qty == "50"
    assert "350" in q and "450" in q


@pytest.mark.asyncio
async def test_thanks_asks_for_vector_logo(client: AsyncClient):
    await client.post("/api/chat/message", json={
        "chat_id": "thx-1", "message": "Яка вартість резинок для фітнесу?",
    })
    r = await client.post("/api/chat/message", json={"chat_id": "thx-1", "message": "Дякую"})
    body = r.json()["response"].lower()
    assert "вектор" in body and "макет" in body
    assert "резинк" not in body  # не повторювати інфо по товару


@pytest.mark.asyncio
async def test_thanks_plain(client: AsyncClient):
    r = await client.post("/api/chat/message", json={"chat_id": "thx-2", "message": "дякую вам велике"})
    assert "вектор" in r.json()["response"].lower()


@pytest.mark.asyncio
async def test_cardholder_not_mat(client: AsyncClient):
    chat = "card-1"
    await client.post("/api/chat/message", json={
        "chat_id": chat, "message": "Робите брендування килимків?",
    })
    r = await client.post("/api/chat/message", json={
        "chat_id": chat, "message": "Кардхолдер пластиковий 50 шт з друком лого. Яка вартість?",
    })
    body = r.json()["response"].lower()
    assert "килим" not in body
    assert "кардхолдер" in body


@pytest.mark.asyncio
async def test_black_carabiner_fixator_two_separate_links(client: AsyncClient):
    r = await client.post("/api/chat/message", json={
        "chat_id": "carab-1",
        "message": "Карабін чорний з фіксатором 50 шт можна зробити з друком логотипу?",
    })
    body = r.json()["response"].lower()
    assert "2 види" in body or "два види" in body
    extras = r.json().get("extra_messages") or []
    assert len(extras) == 2
    assert any("3330000045044" in e for e in extras)
    assert any("3330000045082" in e for e in extras)


@pytest.mark.asyncio
async def test_carabiner_fixator_pricelist(client: AsyncClient):
    r = await client.post("/api/chat/message", json={
        "chat_id": "carab-2", "message": "Прайс на друк на карабінах з фіксатором",
    })
    body = r.json()["response"]
    assert "54" in body and "48" in body and "36" in body


@pytest.mark.asyncio
async def test_generic_silicone_bracelet_guides_sizes(client: AsyncClient):
    r = await client.post("/api/chat/message", json={
        "chat_id": "silg-1", "message": "Скільки коштують силіконові браслети?",
    })
    body = r.json()["response"].lower()
    assert "202" in body and "180" in body and "160" in body
    assert "ширин" in body


@pytest.mark.asyncio
async def test_mat_sums_multiple_quantities(client: AsyncClient):
    chat = "mat-sum-1"
    await client.post("/api/chat/message", json={
        "chat_id": chat, "message": "Робите брендування килимків?",
    })
    r = await client.post("/api/chat/message", json={
        "chat_id": chat, "message": "2 шт чорний і 3 шт фіолетовий",
    })
    body = r.json()["response"]
    assert "5 шт" in body
    assert "855" in body.replace(" ", "")


@pytest.mark.asyncio
async def test_layout_screenshot_not_payment(client: AsyncClient):
    r = await client.post("/api/chat/message", json={
        "chat_id": "shot-layout-1", "message": "[СКРІН|/tmp/x.jpg] правки по макету",
    })
    body = r.json()["response"].lower()
    assert "оплат" not in body
    assert "менеджер" in body


@pytest.mark.asyncio
async def test_silicone_multi_age_defaults_12mm_three_lines(client: AsyncClient):
    r = await client.post("/api/chat/message", json={
        "chat_id": "sil-ages-1",
        "message": (
            "Потрібно з друком лого - 50 шт синій дитячий, "
            "50 шт червоний підлітковий, 50 шт зелений дорослий."
        ),
    })
    body = r.json()["response"]
    low = body.lower()
    assert "5 мм" in body and "12 мм" in body and "20 мм" in body
    assert "160" in body and "180" in body and "202" in body
    assert "2625" in body.replace(" ", "")
    assert "17,50" in body or "17.50" in body or "17.5" in body
    assert "20 мм × 100" not in low and "20 мм ×100" not in low
    assert "2300" not in body.replace(" ", "")


@pytest.mark.asyncio
async def test_remove_only_20mm_keeps_12mm(client: AsyncClient):
    chat = "sil-rm-20"
    await client.post("/api/chat/message", json={
        "chat_id": chat,
        "message": (
            "Потрібно з друком лого - 50 шт синій дитячий 160*12 мм, "
            "50 шт червоний підлітковий 180*12 мм, 50 шт зелений дорослий 202*20 мм."
        ),
    })
    r = await client.post("/api/chat/message", json={
        "chat_id": chat, "message": "Приберіть будь ласка звідси браслети 20 мм",
    })
    body = r.json()["response"].lower()
    assert "порожн" not in body
    assert "12" in body
    assert "прибрав" in body


@pytest.mark.asyncio
async def test_badge_pocket_guide_not_lanyard(client: AsyncClient):
    r = await client.post("/api/chat/message", json={
        "chat_id": "pocket-1", "message": "У вас є кишеньки прозорі?",
    })
    body = r.json()["response"]
    assert "65" in body and "115" in body
    assert "А6" in body or "а6" in body.lower()
    assert "шнурок" not in body.lower()
    assert "не продаємо" not in body.lower()


@pytest.mark.asyncio
async def test_badge_pocket_65x115_100pcs(client: AsyncClient):
    r = await client.post("/api/chat/message", json={
        "chat_id": "pocket-2",
        "message": "Кишенька для бейджа 65*115 мм - 100 шт",
    })
    body = r.json()["response"]
    assert "1200" in body.replace(" ", "")
    assert "кишеньк" in body.lower()
    assert "шнур" not in body.lower()
    assert "28.5" not in body and "28,5" not in body


@pytest.mark.asyncio
async def test_cardholder_guide_print_or_blank(client: AsyncClient):
    r = await client.post("/api/chat/message", json={
        "chat_id": "card-guide-1",
        "message": "Підійдуть кардхолдери під бейджі — порахувати?",
    })
    body = r.json()["response"].lower()
    assert "друком" in body or "друк" in body
    assert "без" in body
    assert "колір" in body


@pytest.mark.asyncio
async def test_cardholder_plastic_colors_separate_links(client: AsyncClient):
    r = await client.post("/api/chat/message", json={
        "chat_id": "card-colors-1",
        "message": "Які кольори пластикових кардхолдерів у вас доступно?",
    })
    extras = r.json().get("extra_messages") or []
    assert len(extras) >= 5
    joined = " ".join(extras)
    assert "3330000045396" in joined
    assert "kardholder-plastikoviy-chorniy-55-85mm" in joined


@pytest.mark.asyncio
async def test_lanyard_colors_10_15_20(client: AsyncClient):
    r = await client.post("/api/chat/message", json={
        "chat_id": "lan-col-1",
        "message": "Які кольори шнурків для бейджа є у наявності?",
    })
    body = r.json()["response"]
    assert "10 мм" in body and "15 мм" in body and "20 мм" in body
    assert "фастекс" not in body.lower()
    assert "40 мм" not in body


@pytest.mark.asyncio
async def test_two_products_get_two_answers(client: AsyncClient):
    r = await client.post("/api/chat/message", json={
        "chat_id": "two-prod-1",
        "message": "Яка вартість та термін виготовлення силіконові браслети з лого та шнурки для бейджа з лого?",
    })
    body = r.json()["response"].lower()
    assert "силікон" in body
    assert "5 мм" in body or "12 мм" in body
    assert "шнур" in body
    assert "гладенька" in body
    assert "репсова" in body
    assert "10 мм, 15 мм, 20 мм" not in r.json()["response"]


@pytest.mark.asyncio
async def test_lanyard_two_carabiners_not_metal_catalog(client: AsyncClient):
    r = await client.post("/api/chat/message", json={
        "chat_id": "lan-2car-1",
        "message": "20 мм з друком лого на 2 карабіни - 100 шт",
    })
    body = r.json()["response"]
    low = body.lower()
    assert "36.5" in body or "36,5" in body or "3650" in body.replace(" ", "")
    assert "карабін" in low
    assert "karabiny-metalevi" not in low
    assert "фіксатор" not in low


@pytest.mark.asyncio
async def test_lanyard_width_then_two_carabiners(client: AsyncClient):
    chat = "lan-fasten-1"
    r1 = await client.post("/api/chat/message", json={
        "chat_id": chat,
        "message": "Яка вартість та термін виготовлення шнурки для бейджа з логотипом?",
    })
    assert "гладенька" in r1.json()["response"].lower()
    r2 = await client.post("/api/chat/message", json={
        "chat_id": chat,
        "message": "20 мм гладенька 100 штук",
    })
    body2 = r2.json()["response"].lower()
    assert "карабін" in body2
    assert "3650" not in r2.json()["response"].replace(" ", "")
    r3 = await client.post("/api/chat/message", json={
        "chat_id": chat,
        "message": "Потрібно на 2 карабіни металеві без додаткових аксесуарів",
    })
    body3 = r3.json()["response"]
    assert "36.5" in body3 or "36,5" in body3 or "3650" in body3.replace(" ", "")
    assert "karabiny-metalevi" not in body3.lower()


@pytest.mark.asyncio
async def test_lanyard_print_not_stock_after_correction(client: AsyncClient):
    chat = "lan-print-1"
    await client.post("/api/chat/message", json={
        "chat_id": chat,
        "message": "Яка вартість шнурків для бейджа з лого?",
    })
    r = await client.post("/api/chat/message", json={
        "chat_id": chat,
        "message": "Запитна шнурки для бейджа з лого, а не ті, які є у наявності.",
    })
    body = r.json()["response"].lower()
    assert "гладенька" in body
    assert "репсова" in body
    assert "10 мм, 15 мм, 20 мм" not in r.json()["response"]
    follow = await client.post("/api/chat/message", json={
        "chat_id": chat, "message": "Потрібно з друком логотипу",
    })
    follow_body = follow.json()["response"].lower()
    assert "гладенька" in follow_body or "репсова" in follow_body
    assert "у наявності шнурки для бейджа\nпо ширині" not in follow.json()["response"].lower()


@pytest.mark.asyncio
async def test_new_product_replaces_previous_focus(client: AsyncClient):
    chat = "switch-prod-1"
    await client.post("/api/chat/message", json={
        "chat_id": chat, "message": "силіконові браслети 12 мм 50 шт",
    })
    r = await client.post("/api/chat/message", json={
        "chat_id": chat, "message": "У вас є кишеньки прозорі?",
    })
    body = r.json()["response"].lower()
    assert "кишеньк" in body
    assert "65" in body
    assert "17,50" not in body and "17.50" not in body
    assert "2625" not in body


@pytest.mark.asyncio
async def test_requisites_file_confirms_before_pdf(client: AsyncClient):
    chat = "req-file-1"
    await client.post("/api/chat/message", json={
        "chat_id": chat, "message": "силіконові браслети 50 штук",
    })
    await client.post("/api/chat/message", json={"chat_id": chat, "message": "так"})
    await client.post("/api/chat/message", json={"chat_id": chat, "message": "Олег Файл"})
    await client.post("/api/chat/message", json={"chat_id": chat, "message": "0501112233"})
    await client.post("/api/chat/message", json={
        "chat_id": chat, "message": "відділення 3 Київ",
    })
    start = await client.post("/api/chat/message", json={
        "chat_id": chat, "message": "рахунок",
    })
    assert "фото" in start.json()["response"].lower() or "pdf" in start.json()["response"].lower()
    shot = await client.post("/api/chat/message", json={
        "chat_id": chat,
        "message": (
            "[СКРІН|/tmp/requisites.pdf] ТОВ Файл Реквізити, ЄДРПОУ 44556677, "
            "ІПН немає, м. Одеса, вул. Дерибасівська, 5"
        ),
    })
    body = shot.json()
    assert body["action"] == "confirm_invoice_data"
    assert "все вірно" in body["response"].lower()
    assert "44556677" in body["response"]
    assert "одеса" in body["response"].lower()
    assert "іпн" not in body["response"].lower()
    assert not body.get("documents")
    created = await client.post("/api/chat/message", json={
        "chat_id": chat, "message": "так, все вірно",
    })
    assert created.json()["action"] == "invoice_created"
    assert created.json().get("documents")


@pytest.mark.asyncio
async def test_lanyard_invoice_keeps_full_address_price_and_hides_ipn(client: AsyncClient):
    chat = "lan-inv-addr-1"
    start = await client.post("/api/chat/message", json={
        "chat_id": chat,
        "message": "Підготуйте будь ласка рахунок на шнурки для бейджа білі 15 мм з одним карабіном 100 штук.",
    })
    assert start.json()["action"] == "collect_invoice_data"
    assert "реквізити платника" in start.json()["response"].lower()
    blob = (
        "ФОП Афонін Дмитро Вікторович\n"
        "3052600213\n"
        "47140, Тернопільська обл., Кременецький р-н, с. Бриків, вул.\n"
        "Київська, буд. 22"
    )
    confirm = await client.post("/api/chat/message", json={"chat_id": chat, "message": blob})
    body = confirm.json()["response"]
    assert confirm.json()["action"] == "confirm_invoice_data"
    assert "47140" in body
    assert "Кременецький" in body
    assert "Бриків" in body
    assert "вул. Київська" in body
    assert "іпн" not in body.lower()
    assert "немає" not in body.lower()
    created = await client.post("/api/chat/message", json={
        "chat_id": chat, "message": "так",
    })
    issued = created.json()
    assert issued["action"] == "invoice_created"
    from pathlib import Path
    from pypdf import PdfReader
    pdf_text = "\n".join(
        page.extract_text() or ""
        for page in PdfReader(issued["documents"][0]["path"]).pages
    )
    assert "Кременецький" in pdf_text
    assert "Бриків" in pdf_text
    assert "ІПН:" not in pdf_text
    assert "2 850,00" in pdf_text or "2850" in pdf_text
    assert Path(issued["documents"][0]["path"]).exists()


@pytest.mark.asyncio
async def test_new_payer_block_replaces_stale_and_pdf_is_filled(client: AsyncClient):
    """Новий блок реквізитів замінює старий ЄДРПОУ, а клієнту йде заповнений PDF, не пуста форма 1С."""
    chat = "replace-payer-pdf-1"
    first = (
        "ФОП Афонін Дмитро Вікторович\n"
        "3052600213\n"
        "47140, Тернопільська обл., Кременецький р-н, с. Бриків, вул.\n"
        "Київська, буд. 22"
    )
    await client.post("/api/chat/message", json={
        "chat_id": chat,
        "message": "Підготуйте будь ласка рахунок на шнурки для бейджа білі 15 мм з одним карабіном 100 штук.",
    })
    await client.post("/api/chat/message", json={"chat_id": chat, "message": first})
    await client.post("/api/chat/message", json={"chat_id": chat, "message": "так"})
    await client.post("/api/chat/message", json={
        "chat_id": chat, "message": "Підготуйте будь ласка рахунок",
    })
    fresh = (
        "ФОП Афонін Дмитро Вікторович\n"
        "3052600345\n"
        "47140, Тернопільська обл., Кременецький р-н, с. Бриків, вул.\n"
        "Київська, буд. 22"
    )
    confirm = await client.post("/api/chat/message", json={"chat_id": chat, "message": fresh})
    body = confirm.json()["response"]
    assert confirm.json()["action"] == "confirm_invoice_data"
    assert "3052600345" in body
    assert "3052600213" not in body
    assert "47140" in body
    assert "Кременецький" in body
    assert "Бриків" in body
    assert "вул. Київська" in body
    created = await client.post("/api/chat/message", json={"chat_id": chat, "message": "так"})
    issued = created.json()
    assert issued["action"] == "invoice_created"
    doc = issued["documents"][0]
    assert not doc["path"].endswith("_1c.pdf")
    from pypdf import PdfReader
    pdf_text = "\n".join(page.extract_text() or "" for page in PdfReader(doc["path"]).pages)
    assert "Постачальник" in pdf_text
    assert "ФОП Афонін Дмитро Вікторович" in pdf_text
    assert "3052600345" in pdf_text
    assert "ПРИВАТБАНК" in pdf_text
    assert "Кременецький" in pdf_text
    assert "Бриків" in pdf_text
    assert "Договір" in pdf_text
    assert "Рахунок" in pdf_text
    assert "Виписав" in pdf_text
    assert "2 850,00" in pdf_text or "2850" in pdf_text
    assert "Служба у справах дітей" not in pdf_text


@pytest.mark.asyncio
async def test_full_address_omits_ipn_and_reuses_payer(client: AsyncClient):
    chat = "full-addr-payer-1"
    blob = (
        "ФОП Афонін Дмитро Вікторович\n"
        "3052600213\n"
        "47140, Тернопільська обл., Кременецький р-н, с. Бриків, вул.\n"
        "Київська, буд. 22"
    )
    await client.post("/api/chat/message", json={
        "chat_id": chat, "message": "шнурки для бейджа 15 мм 100 штук",
    })
    start = await client.post("/api/chat/message", json={
        "chat_id": chat,
        "message": "Підготуйте будь ласка рахунок на шнурки",
    })
    assert start.json()["action"] == "collect_invoice_data"
    confirm = await client.post("/api/chat/message", json={"chat_id": chat, "message": blob})
    body = confirm.json()["response"]
    assert confirm.json()["action"] == "confirm_invoice_data"
    assert "47140" in body
    assert "Кременецький" in body
    assert "Бриків" in body
    assert "Київська" in body
    assert "непає" not in body.lower()
    assert "іпн" not in body.lower()
    created = await client.post("/api/chat/message", json={
        "chat_id": chat, "message": "так, все вірно",
    })
    issued = created.json()
    assert issued["action"] == "invoice_created"
    from pathlib import Path
    from pypdf import PdfReader
    pdf_path = Path(issued["documents"][0]["path"])
    pdf_text = "\n".join(page.extract_text() or "" for page in PdfReader(pdf_path).pages)
    assert "Кременецький" in pdf_text
    assert "Бриків" in pdf_text
    assert "непає" not in pdf_text.lower()
    assert "ІПН:" not in pdf_text
    await client.post("/api/chat/message", json={
        "chat_id": chat, "message": "силіконові браслети 12 мм 30 штук",
    })
    repeat = await client.post("/api/chat/message", json={
        "chat_id": chat, "message": "Підготуйте будь ласка рахунок",
    })
    offer = repeat.json()["response"]
    assert repeat.json()["action"] == "confirm_saved_requisites"
    assert "такими реквізитами" in offer.lower()
    assert "3052600213" in offer
    assert "Бриків" in offer
    assert "іпн" not in offer.lower()
    second = await client.post("/api/chat/message", json={"chat_id": chat, "message": "так"})
    second_body = second.json()
    assert second_body["action"] == "invoice_created"
    assert second_body.get("data", {}).get("counterparty_created") is False
