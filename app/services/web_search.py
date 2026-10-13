"""Короткий пошук в інтернеті для питань НЕ про прайс Silicon."""
from __future__ import annotations

import logging
import re

import httpx
from bs4 import BeautifulSoup

from app.config import get_settings

logger = logging.getLogger(__name__)

_SILICON = (
    "браслет",
    "силікон",
    "шапоч",
    "плаванн",
    "чашк",
    "шнур",
    "прайс",
    "замов",
    "доставк",
    "оплат",
    "єдрпоу",
    "edrpou",
    "liqpay",
    "лікпей",
    "логотип",
    "макет",
    "тираж",
    "silicon",
    "олег",
    "дмитр",
    "tyvek",
    "slap",
    "rfid",
    "брелок",
    "еко-чаш",
    "новою пошт",
)


def is_catalog_or_order_topic(text: str) -> bool:
    lower = (text or "").lower()
    return any(w in lower for w in _SILICON)


def should_web_search(text: str, *, session_has_product: bool = False) -> bool:
    lower = (text or "").strip().lower()
    if len(lower) < 8:
        return False
    if session_has_product:
        return False
    if is_catalog_or_order_topic(lower):
        return False
    from app.services.site_catalog import is_product_detail_question

    if is_product_detail_question(text):
        return False
    if any(
        w in lower
        for w in ("ти бот", "ти аі", "ai агент", "менеджер", "привіт", "вітаю")
    ):
        return False
    return "?" in text or any(
        lower.startswith(p)
        for p in ("що ", "як ", "хто ", "коли ", "чому", "де ", "чи ", "навіщо")
    )


def search_brief(query: str, *, limit: int = 3) -> str:
    """Повертає короткий текст зі сніпетів DuckDuckGo. Порожній рядок — якщо не вийшло."""
    settings = get_settings()
    if settings.mock_external_apis:
        return ""
    q = (query or "").strip()
    if not q:
        return ""
    try:
        with httpx.Client(timeout=8.0, follow_redirects=True, headers={
            "User-Agent": "Mozilla/5.0 (compatible; SiliconManager/1.0)",
        }) as client:
            resp = client.post(
                "https://html.duckduckgo.com/html/",
                data={"q": q[:180]},
            )
        if resp.status_code != 200:
            return ""
        soup = BeautifulSoup(resp.text, "lxml")
        parts: list[str] = []
        for node in soup.select(".result__snippet, .result__body")[:limit]:
            snippet = re.sub(r"\s+", " ", node.get_text(" ", strip=True))
            if snippet and snippet not in parts:
                parts.append(snippet[:280])
        return "\n".join(f"- {p}" for p in parts[:limit])
    except Exception:
        logger.warning("web search failed", exc_info=True)
        return ""
