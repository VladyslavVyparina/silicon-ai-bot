import pytest
from httpx import AsyncClient

from app.services.ai_consultant import AIConsultant


def test_prompt_is_human_and_strips_tech_leaks():
    c = AIConsultant()
    prompt = c._reload_instructions()
    assert "Олег" in prompt
    assert "відповіси на останнє повідомлення" in prompt.lower() or "останнє повідомлення" in prompt
    assert "Не підміняй відповідь своїм шаблоном" in prompt
    assert "Не перезапускай скрипт" in prompt
    raw, esc = c._detect_escalation(
        "Переключу на менеджера.\n[ESCALATE]\nдив. pricelist_silicon.txt"
    )
    assert esc is True
    assert "[ESCALATE]" not in raw
    assert "pricelist_silicon" not in raw
    assert "менеджера" in raw
    assert "pricelist_silicon" not in c.build_assortment_overview()
    assert not c._wants_assortment("а яка ціна?")
    assert not c._wants_assortment("а скільки днів?")
    assert not c._wants_assortment("силіконові браслети яка ціна")
    assert c._wants_assortment("які у вас є товари напиши та ціну")
    assert c._wants_assortment("прайс")
    assert "який саме товар і кількість" not in c.FALLBACK_RESPONSE.lower()
    overview = c.build_assortment_overview()
    assert "•" not in overview
    assert "силікон" in overview.lower()
    assert "Орієнтири" not in overview
    assert "300×30" not in overview
    assert "17,50" not in overview
    assert "50 шт" not in overview
    assert "НА ПЕРШОМУ МІСЦІ" in prompt
    assert "ніколи не називай Дмитра" in prompt
    assert "ВОРОНКА" in prompt
    assert "УМОВА ВИХОДУ" in prompt
    assert "Не ігноруй питання заради воронки" in prompt
    assert AIConsultant.HISTORY_LIMIT == 40
    assert "без маркерів-крапок «•»" in prompt
    assert "ВІТАННЯ" in prompt
    assert "на зв'язку сам" in prompt or "реальн" in prompt.lower()
    assert "реальний менеджер" in prompt.lower()
    assert "аі агент" in prompt.lower()
    from app.services.bot_settings import get_greeting_sync, load_knowledge_text

    greet = get_greeting_sync()
    assert "Олег" in greet
    assert "Silicon" in greet
    assert "Чим можу" in greet or "допомогти" in greet
    knowledge = load_knowledge_text(80000)
    assert "браслет" in knowledge.lower() or "силікон" in knowledge.lower()
    assert "шапоч" in knowledge.lower()
    assert "400" in knowledge
    assert "/uni" not in knowledge
    # повна база знань з app/data підвантажується разом із прайсом
    assert "consultant_knowledge_full" in knowledge or "силіконові браслети" in knowledge.lower()
    assert len(knowledge) > 20000
    assert "ДЕ БРАТИ ФАКТИ" in prompt
    assert "Прайс Silicon" in prompt or "data/knowledge" in prompt
    assert "### Файл:" not in knowledge
    from pathlib import Path
    from app.config import PROJECT_ROOT

    assert (PROJECT_ROOT / "data" / "knowledge" / "Prays_Silicon.docx").exists()


@pytest.mark.asyncio
async def test_health(client: AsyncClient):
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_consult_delivery_template(client: AsyncClient):
    resp = await client.post("/api/ai/consult", json={
        "question": "Як здійснюється доставка?",
        "chat_id": "test-1",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["source"] == "template"
    assert "Україн" in data["response"] or "достав" in data["response"].lower()


@pytest.mark.asyncio
async def test_consult_catalog_search(client: AsyncClient):
    resp = await client.post("/api/ai/consult", json={
        "question": "Розкажіть про силіконові браслети",
        "chat_id": "test-2",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert "response" in data
    assert len(data["response"]) > 20


@pytest.mark.asyncio
async def test_consult_unknown_no_auto_escalate(client: AsyncClient):
    resp = await client.post("/api/ai/consult", json={
        "question": "xyzabc123 неіснуючий запит qwerty",
        "chat_id": "test-3",
    })
    assert resp.status_code == 200
    data = resp.json()
    # невідоме питання — бот уточнює сам, без автоматичної заявки менеджеру
    assert data["escalate_to_manager"] is False
    assert len(data["response"]) > 10


@pytest.mark.asyncio
async def test_consult_payment_template(client: AsyncClient):
    resp = await client.post("/api/ai/consult", json={
        "question": "Які способи оплати через LiqPay?",
        "chat_id": "test-4",
    })
    assert resp.status_code == 200
    assert "LiqPay" in resp.json()["response"] or "оплат" in resp.json()["response"].lower()


@pytest.mark.asyncio
async def test_consult_silicone_production_terms(client: AsyncClient):
    resp = await client.post("/api/ai/consult", json={
        "question": "Яка вартість та термін виготовлення браслетів?",
        "chat_id": "terms-bracelets-1",
    })
    text = resp.json()["response"]
    lower = text.lower()
    assert "14" not in text
    assert "5" in text and "7" in text
    assert "робоч" in lower
    assert "індивідуаль" in lower or "макет" in lower


def test_web_search_skips_catalog_topics():
    from app.services.web_search import should_web_search

    assert should_web_search("що таке NFC-мітка в телефоні?") is True
    assert should_web_search("шапочки яка ціна") is False
    assert should_web_search("прайс на браслети") is False
    assert should_web_search("ти бот?") is False
    assert should_web_search("що означає слово pantone у друці?") is True
    assert should_web_search("А з якого вона матеріалу?", session_has_product=True) is False
    assert should_web_search("що таке силіконові браслети, розкажи детальніше") is False


def test_site_product_brief_from_catalog_not_web():
    from app.services.site_catalog import site_product_brief

    text = site_product_brief("що таке силіконові браслети детальніше")
    assert "silicon.com.ua" in text.lower() or "силікон" in text.lower()
    assert "wikipedia" not in text.lower()
