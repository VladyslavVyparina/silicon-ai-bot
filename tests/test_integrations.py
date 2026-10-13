import pytest
import httpx

from app.config import get_settings
from app.services.onec import OneCService, reset_mock_counterparties
from app.services.nova_poshta import NovaPoshtaService


@pytest.fixture(autouse=True)
def reset_onec():
    reset_mock_counterparties()
    get_settings.cache_clear()
    yield
    reset_mock_counterparties()
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_find_by_edrpou():
    service = OneCService()
    result = await service.find_counterparty(edrpou="12345678")
    assert result is not None
    assert result["id"] == "1c-001"


@pytest.mark.asyncio
async def test_find_by_name():
    service = OneCService()
    result = await service.find_counterparty(name="ТОВ Сілікон")
    assert result is not None
    assert result["edrpou"] == "12345678"


@pytest.mark.asyncio
async def test_no_duplicate_on_create():
    service = OneCService()
    result = await service.create_counterparty({
        "edrpou": "12345678",
        "name": "ТОВ Сілікон",
    })
    assert result["created"] is False
    assert result["counterparty_id"] == "1c-001"


@pytest.mark.asyncio
async def test_create_new_counterparty():
    service = OneCService()
    result = await service.create_counterparty({
        "edrpou": "11111111",
        "name": "ТОВ Новий Клієнт",
    })
    assert result["created"] is True
    assert result["counterparty_id"].startswith("1c-")


@pytest.mark.asyncio
async def test_find_by_name_no_duplicate():
    service = OneCService()
    result = await service.create_counterparty({"name": "ПП Тест Компані"})
    assert result["created"] is False
    assert result["counterparty_id"] == "1c-002"


@pytest.mark.asyncio
async def test_create_invoice():
    service = OneCService()
    cp = await service.create_counterparty({"edrpou": "11111111", "name": "ТОВ Test"})
    invoice = await service.create_invoice(cp["counterparty_id"], {"total_amount": 5000})
    assert invoice["success"] is True
    assert invoice["invoice_number"].startswith("INV-")
    assert invoice.get("invoice_id")
    assert invoice.get("mock") is True


def test_normalize_found_false():
    from app.services.onec import _normalize_counterparty

    assert _normalize_counterparty({"found": False}) is None
    found = _normalize_counterparty({
        "found": True,
        "id": "guid-9",
        "name": "ТОВ Федейс",
        "code": "12345678",
        "inn": "1234567890",
        "address": "м. Київ",
    })
    assert found["id"] == "guid-9"
    assert found["edrpou"] == "12345678"
    assert found["ipn"] == "1234567890"
    from app.services.onec import _api_base

    assert _api_base("http://localhost/silikon") == "http://localhost/silikon/hs/bot-api/v1"
    assert _api_base("http://localhost/silikon/hs/bot-api/v1") == "http://localhost/silikon/hs/bot-api/v1"
    assert (
        _api_base("http://185.161.208.178/FOPAfonin/odata/standard.odata")
        == "http://185.161.208.178/FOPAfonin/hs/bot-api/v1"
    )


class _FakeResponse:
    def __init__(self, json_data=None, status=200, content=b"", content_type="application/json"):
        self._json = json_data
        self.status_code = status
        self.content = content
        self.headers = {"content-type": content_type}
        self.text = "" if json_data is None else str(json_data)

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            request = httpx.Request("GET", "http://test")
            raise httpx.HTTPStatusError("err", request=request, response=httpx.Response(self.status_code))


class _FakeClient:
    def __init__(self, handler):
        self._handler = handler

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, url, params=None, auth=None):
        return self._handler("GET", url, params, None, auth)

    async def post(self, url, json=None, auth=None):
        return self._handler("POST", url, None, json, auth)


@pytest.mark.asyncio
async def test_real_1c_search_uses_code_query(monkeypatch):
    import httpx as httpx_mod

    monkeypatch.setenv("MOCK_EXTERNAL_APIS", "false")
    monkeypatch.setenv("ONEC_API_URL", "http://localhost/silikon")
    get_settings.cache_clear()
    captured: dict = {}

    def handler(method, url, params, json_body, auth):
        captured.update(method=method, url=url, params=params)
        return _FakeResponse({"id": "guid-1", "name": "ТОВ Федейс", "code": "12345678"})

    monkeypatch.setattr(httpx_mod, "AsyncClient", lambda **kwargs: _FakeClient(handler))
    service = OneCService()
    found = await service.find_counterparty(edrpou="12345678")
    assert captured["method"] == "GET"
    assert captured["url"] == "http://localhost/silikon/hs/bot-api/v1/counterparty"
    assert captured["params"] == {"code": "12345678"}
    assert found["id"] == "guid-1"
    assert found["edrpou"] == "12345678"
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_real_1c_create_counterparty_and_invoice_and_pdf(monkeypatch, tmp_path):
    import httpx as httpx_mod

    monkeypatch.setenv("MOCK_EXTERNAL_APIS", "false")
    monkeypatch.setenv("ONEC_API_URL", "http://localhost/silikon")
    get_settings.cache_clear()
    calls: list[tuple] = []

    def handler(method, url, params, json_body, auth):
        calls.append((method, url, params, json_body))
        if method == "GET" and url.endswith("/counterparty"):
            return _FakeResponse(status=404)
        if method == "POST" and url.endswith("/counterparty"):
            return _FakeResponse({"id": "cp-77", "name": json_body["name"], "edrpou": json_body["edrpou"]})
        if method == "POST" and url.endswith("/invoice"):
            return _FakeResponse({"id": "inv-88", "number": "ФО00-000587"})
        if method == "GET" and url.endswith("/pdf"):
            return _FakeResponse(json_data=None, content=b"%PDF-1.4 fake", content_type="application/pdf")
        return _FakeResponse(status=500)

    monkeypatch.setattr(httpx_mod, "AsyncClient", lambda **kwargs: _FakeClient(handler))
    service = OneCService()
    created = await service.create_counterparty({
        "edrpou": "55667788",
        "name": "ТОВ Новий",
        "phone": "+380501112233",
    })
    assert created["created"] is True
    assert created["counterparty_id"] == "cp-77"
    assert calls[1][3]["phone"] == "+380501112233"
    assert calls[1][3]["code"] == "55667788"
    assert calls[1][3]["inn"] == calls[1][3]["ipn"]

    invoice = await service.create_invoice("cp-77", {
        "total_amount": 20280,
        "description": "Замовлення Silicon #1",
        "items": [{"product": "Шнурок", "qty": 600, "unit_price": 33.8, "amount": 20280}],
    })
    assert invoice["invoice_id"] == "inv-88"
    assert invoice["invoice_number"] == "ФО00-000587"
    assert calls[2][1].endswith("/hs/bot-api/v1/invoice")
    assert calls[2][3]["source"] == "telegram-bot"
    assert "Telegram-ботом" in calls[2][3]["comment"] or "Замовлення" in calls[2][3]["description"]
    assert calls[2][3]["items"][0]["quantity"] == 600
    assert calls[2][3]["items"][0]["product"] == "Шнурок"

    pdf = await service.download_invoice_pdf("inv-88", order_id=1)
    assert pdf is not None
    assert pdf.read_bytes().startswith(b"%PDF")
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_real_1c_existing_counterparty_409_reuses_id(monkeypatch):
    import httpx as httpx_mod

    monkeypatch.setenv("MOCK_EXTERNAL_APIS", "false")
    monkeypatch.setenv("ONEC_API_URL", "http://185.161.208.178/FOPAfonin")
    monkeypatch.setenv("ONEC_API_USER", "Bot")
    monkeypatch.setenv("ONEC_API_PASSWORD", "secret")
    get_settings.cache_clear()

    def handler(method, url, params, json_body, auth):
        if method == "GET":
            return _FakeResponse({"found": False})
        return _FakeResponse(
            {"success": False, "message": "Counterparty with this code already exists 6e9c5007-8225-11e9-8f8c-4ccc6a93cd13"},
            status=409,
        )

    monkeypatch.setattr(httpx_mod, "AsyncClient", lambda **kwargs: _FakeClient(handler))
    service = OneCService()
    created = await service.create_counterparty({"edrpou": "3052600213", "name": "ФОП Тест"})
    assert created["created"] is False
    assert created["counterparty_id"] == "6e9c5007-8225-11e9-8f8c-4ccc6a93cd13"
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_real_1c_invoice_http_error_is_not_a_local_number(monkeypatch):
    import httpx as httpx_mod

    monkeypatch.setenv("MOCK_EXTERNAL_APIS", "false")
    monkeypatch.setenv("ONEC_API_URL", "http://185.161.208.178/FOPAfonin")
    monkeypatch.setenv("ONEC_API_USER", "bot")
    monkeypatch.setenv("ONEC_API_PASSWORD", "secret")
    get_settings.cache_clear()

    def handler(method, url, params, json_body, auth):
        return _FakeResponse(status=401)

    monkeypatch.setattr(httpx_mod, "AsyncClient", lambda **kwargs: _FakeClient(handler))
    service = OneCService()
    invoice = await service.create_invoice("cp-1", {"total_amount": 100, "description": "тест"})
    assert invoice["success"] is False
    assert invoice["posted_to_1c"] is False
    assert invoice["invoice_number"] is None
    assert invoice["mock"] is False
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_real_1c_search_found_false_and_true(monkeypatch):
    import httpx as httpx_mod

    monkeypatch.setenv("MOCK_EXTERNAL_APIS", "false")
    monkeypatch.setenv("ONEC_API_URL", "http://185.161.208.178/FOPAfonin")
    get_settings.cache_clear()

    def handler(method, url, params, json_body, auth):
        code = (params or {}).get("code")
        if code == "00000000":
            return _FakeResponse({"found": False})
        return _FakeResponse({
            "found": True,
            "id": "c6c91c53-aba6-11f1-811a-d5b35a947f41",
            "name": "ТОВ Тест Silicon Bot",
            "code": "99887766",
            "inn": "9988776655",
            "address": "м. Київ, вул. Тестова, 1",
        })

    monkeypatch.setattr(httpx_mod, "AsyncClient", lambda **kwargs: _FakeClient(handler))
    service = OneCService()
    assert await service.find_counterparty(edrpou="00000000") is None
    found = await service.find_counterparty(edrpou="99887766")
    assert found["id"].startswith("c6c91c53")
    assert found["ipn"] == "9988776655"
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_nova_poshta_search_city():
    service = NovaPoshtaService()
    cities = await service.search_city("Київ")
    assert len(cities) >= 1
    assert cities[0]["ref"] is not None


@pytest.mark.asyncio
async def test_nova_poshta_create_ttn():
    service = NovaPoshtaService()
    result = await service.create_ttn({
        "recipient_name": "Тест",
        "recipient_phone": "380501234567",
        "city_ref": "mock-city-ref",
        "warehouse_ref": "mock-wh-ref",
        "weight_kg": 1,
    })
    assert result["success"] is True
    assert result["ttn_number"].startswith("2045")
    assert "novaposhta.ua" in result["ttn_url"]


@pytest.mark.asyncio
async def test_nova_poshta_address_ttn():
    service = NovaPoshtaService()
    result = await service.create_ttn({
        "delivery_type": "address",
        "recipient_last_name": "Петренко",
        "recipient_first_name": "Іван",
        "recipient_patronymic": "Іванович",
        "recipient_phone": "380501234567",
        "city_ref": "mock-city-ref",
        "street_address": "вул. Хрещатик, 1",
        "weight_kg": 1,
    })
    assert result["success"] is True
    assert result["service_type"] == "WarehouseDoors"
