import pytest
from httpx import AsyncClient

from app.services.pricing_calculator import PricingCalculator, format_money, format_quote_message


@pytest.fixture
def calc():
    return PricingCalculator()


def test_format_money_strips_trailing_zero():
    assert format_money(1150.0) == "1150"
    assert format_money(11.5) == "11.5"
    assert format_money(18.40) == "18.4"


def test_silicone_12mm_100_units(calc):
    result = calc.calculate("силіконовий", 100, size_mm="12")
    assert result["success"] is True
    assert result["unit_price"] == 11.50
    assert result["total_amount"] == 1150.0
    assert result["production_days"] == 7
    assert "1150 грн" in result["message"]
    assert "1150.0" not in result["message"]
    msg = format_quote_message(result, with_confirm=True)
    assert "100 шт" in msg
    assert "11.5 грн/шт" in msg
    assert "1150 грн" in msg
    assert "5–7" in msg or "5-7" in msg
    assert "Оформлюємо?" in msg


def test_silicone_20mm_500_units(calc):
    result = calc.calculate("силіконовий", 500, size_mm="20")
    assert result["success"] is True
    assert result["unit_price"] == 18.40
    assert result["total_amount"] == 9200.0


def test_silicone_5mm_1000_units(calc):
    result = calc.calculate("silicone_bracelet_5mm", 1000)
    assert result["success"] is True
    assert result["unit_price"] == 8.50


def test_reflective_200_units(calc):
    result = calc.calculate("світловідбиваючий", 200)
    assert result["success"] is True
    assert result["unit_price"] == 26.00
    assert result["total_amount"] == 5200.0


def test_reflective_3000_units(calc):
    result = calc.calculate("reflective", 3000)
    assert result["success"] is True
    assert result["unit_price"] == 22.00


def test_swim_cap_not_silicone(calc):
    result = calc.calculate("шапочки для плавання", 1)
    assert result["success"] is True
    assert result["unit_price"] == 400.0
    assert "браслет" not in (result.get("product_name") or "").lower()
    hundred = calc.calculate("шапочки 100 штук", 100)
    assert hundred["success"] is True
    assert hundred["unit_price"] == 300.0


def test_unknown_product(calc):
    result = calc.calculate("голограми", 50)
    assert result["success"] is False


def test_silicone_10_units_from_pdf(calc):
    result = calc.calculate("силіконовий", 10, size_mm="12")
    assert result["success"] is True
    assert result["unit_price"] == 50.0


def test_urgent_production(calc):
    result = calc.calculate("силіконовий", 100, size_mm="12", urgent=True)
    assert result["production_days"] == 5


@pytest.mark.asyncio
async def test_pricing_api(client: AsyncClient):
    resp = await client.post("/api/pricing/calculate", json={
        "product_type": "силіконовий",
        "quantity": 200,
        "size_mm": "12",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["unit_price"] == 11.00
    assert data["total_amount"] == 2200.0


@pytest.mark.asyncio
async def test_pricing_api_reflective(client: AsyncClient):
    resp = await client.post("/api/pricing/calculate", json={
        "product_type": "світловідбиваючий",
        "quantity": 100,
    })
    assert resp.status_code == 200
    assert resp.json()["unit_price"] == 28.00
