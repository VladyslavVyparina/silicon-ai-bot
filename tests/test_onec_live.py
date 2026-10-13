"""Живий прогін HTTP-сервісу 1С bot-api.

Запуск:
  ONEC_LIVE=1 ONEC_API_URL=http://185.161.208.178/FOPAfonin \\
  ONEC_API_USER=bot ONEC_API_PASSWORD=*** \\
  pytest tests/test_onec_live.py -q -s
"""
from __future__ import annotations

import os
import time
import uuid

import pytest

from app.config import get_settings
from app.services.onec import OneCService

pytestmark = pytest.mark.skipif(
    os.getenv("ONEC_LIVE") != "1",
    reason="Потрібно ONEC_LIVE=1 і доступе 1С",
)


@pytest.fixture
def live_onec(monkeypatch):
    url = os.environ.get("ONEC_API_URL") or "http://185.161.208.178/FOPAfonin"
    user = os.environ.get("ONEC_API_USER") or ""
    password = os.environ.get("ONEC_API_PASSWORD") or ""
    if not user or not password:
        pytest.skip("Немає ONEC_API_USER / ONEC_API_PASSWORD")
    monkeypatch.setenv("MOCK_EXTERNAL_APIS", "false")
    monkeypatch.setenv("ONEC_API_URL", url)
    monkeypatch.setenv("ONEC_API_USER", user)
    monkeypatch.setenv("ONEC_API_PASSWORD", password)
    get_settings.cache_clear()
    yield OneCService()
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_live_search_miss(live_onec: OneCService):
    found = await live_onec.find_counterparty(edrpou="00000000")
    assert found is None


@pytest.mark.asyncio
async def test_live_create_search_dedupe_invoice_pdf(live_onec: OneCService):
    code = f"88{int(time.time()) % 1000000:06d}"[:8]
    phone = "+380501112233"
    created = await live_onec.create_counterparty({
        "edrpou": code,
        "name": f"ТОВ Silicon Live {code}",
        "ipn": f"{code}00",
        "company_address": "м. Київ, вул. Тестова, 1",
        "phone": phone,
        "email": "live-test@silicon.local",
    })
    assert created["success"] is True
    assert created["created"] is True
    assert created["counterparty_id"]

    found = await live_onec.find_counterparty(edrpou=code)
    assert found is not None
    assert found["id"] == created["counterparty_id"]
    assert found["edrpou"] == code
    assert found.get("ipn")

    again = await live_onec.create_counterparty({
        "edrpou": code,
        "name": "Має бути дублікат",
        "phone": phone,
    })
    assert again["created"] is False
    assert again["counterparty_id"] == created["counterparty_id"]

    invoice = await live_onec.create_invoice(
        created["counterparty_id"],
        {
            "total_amount": 20280,
            "description": f"Тест Silicon live {uuid.uuid4().hex[:6]}",
            "items": [
                {
                    "product": "Шнурок для бейджа з логотипом і двома карабінами, 20мм",
                    "qty": 600,
                    "unit_price": 33.8,
                    "amount": 20280,
                }
            ],
        },
    )
    assert invoice["success"] is True
    assert invoice["invoice_id"]
    assert invoice["invoice_number"]

    pdf = await live_onec.download_invoice_pdf(invoice["invoice_id"], order_id=0)
    assert pdf is not None
    assert pdf.exists()
    assert pdf.read_bytes()[:4] == b"%PDF"
    print(
        f"\nLIVE OK code={code} cp={created['counterparty_id']} "
        f"inv={invoice['invoice_number']} pdf={pdf}"
    )
