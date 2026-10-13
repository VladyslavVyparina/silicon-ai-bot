"""Опис продукції лише з silicon.com.ua (не з відкритого інтернету). Ціни — з прайсу."""
from __future__ import annotations

import logging
import re
from functools import lru_cache

import httpx
from bs4 import BeautifulSoup

from app.config import get_settings
from app.services.catalog_sync import DEFAULT_CATALOG

logger = logging.getLogger(__name__)

SITE_HOME = "https://silicon.com.ua/?route=common/home"
SITE_ORIGIN = "https://silicon.com.ua"

_UA = "Mozilla/5.0 (compatible; SiliconManager/1.0; +https://silicon.com.ua)"

# ключові слова → сторінки каталогу
_KEYWORD_PAGES: list[tuple[tuple[str, ...], str]] = [
    (("світловідб", "рефлект", "slap світл", "300x30", "400x30"), "https://silicon.com.ua/svitlovidbivayuchi-braslety/"),
    (("tyvek", "тайвек", "контрольн"), "https://silicon.com.ua/kontrolni-braslety-tyvek/"),
    (("тканин", "фестивал"), "https://silicon.com.ua/tkaninni-braslety/"),
    (("слеп", "slap"), "https://silicon.com.ua/silikonovi-slep-braslety/"),
    (("rfid", "рфід", "чіп"), "https://silicon.com.ua/rfid-braslety/"),
    (("чашк", "екочаш", "бамбук"), "https://silicon.com.ua/eko-chashky/"),
    (("шнур", "бейдж"), "https://silicon.com.ua/shnurky-dlya-beydzha/"),
    (("брелок",), "https://silicon.com.ua/breloky/"),
    (("карабін", "карабин"), "https://silicon.com.ua/karabiny-metalevi"),
    (("килим", "випічк"), "https://silicon.com.ua/silikonovij-kilimok-dlya-vypichki"),
    (("резинк", "резинок", "фітнес", "fitness", "jk-rb"), "https://silicon.com.ua/Rubber-bands-for-fitness-JK-RB01"),
    (("голограм",), "https://silicon.com.ua/gologrami"),
    (("силікон", "браслет", "логотип"), "https://silicon.com.ua/silikonovi-braslety/"),
]


def is_product_detail_question(text: str) -> bool:
    lower = (text or "").lower()
    if not lower:
        return False
    about_product = any(
        w in lower
        for w in (
            "браслет",
            "силікон",
            "шапоч",
            "чашк",
            "шнур",
            "tyvek",
            "тайвек",
            "rfid",
            "slap",
            "слеп",
            "брелок",
            "тканин",
            "світловідб",
            "продукц",
            "виріб",
            "товар",
        )
    )
    wants_detail = any(
        w in lower
        for w in (
            "що це",
            "що таке",
            "що означа",
            "з чого",
            "з якого",
            "матеріал",
            "опис",
            "для чого",
            "чим відрізн",
            "яка різниц",
            "як вигляда",
            "детальніш",
            "розкажи про",
            "поясни",
        )
    )
    return about_product and wants_detail


def _fallback_from_defaults(urls: list[str]) -> str:
    by_url = {item["url"]: item for item in DEFAULT_CATALOG}
    lines: list[str] = []
    for url in urls:
        item = by_url.get(url.rstrip("/") + "/") or by_url.get(url)
        if not item:
            # home
            continue
        lines.append(
            f"{item['name']}: {item.get('description') or ''} "
            f"(сторінка {item.get('url')}). Ціни не з цієї сторінки — лише з прайсу."
        )
    if not lines:
        names = "; ".join(f"{i['name']} — {i.get('description') or ''}" for i in DEFAULT_CATALOG[:8])
        lines.append(
            f"Асортимент Silicon (silicon.com.ua): {names}. "
            "Детальні сторінки товарів на сайті; ціни тільки з прайсу / бази знань."
        )
    return "\n".join(lines)[:3500]


def _urls_for_question(question: str) -> list[str]:
    lower = (question or "").lower()
    urls: list[str] = [SITE_HOME]
    for keys, url in _KEYWORD_PAGES:
        if any(k in lower for k in keys):
            urls.append(url)
    # унікальні, порядок збережений
    seen: set[str] = set()
    out: list[str] = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out[:4]


def _extract_text(html: str) -> str:
    soup = BeautifulSoup(html or "", "html.parser")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    chunks: list[str] = []
    meta = soup.find("meta", attrs={"name": "description"})
    if meta and meta.get("content"):
        chunks.append(str(meta["content"]).strip())
    title = soup.find("title")
    if title and title.get_text(strip=True):
        chunks.append(title.get_text(" ", strip=True))
    for el in soup.find_all(["h1", "h2", "h3", "p", "li"]):
        t = re.sub(r"\s+", " ", el.get_text(" ", strip=True))
        if len(t) < 25:
            continue
        if t.lower() in {c.lower() for c in chunks}:
            continue
        chunks.append(t)
        if sum(len(c) for c in chunks) > 3500:
            break
    text = "\n".join(chunks)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:2800]


@lru_cache(maxsize=32)
def _fetch_page(url: str) -> str:
    settings = get_settings()
    if settings.mock_external_apis:
        return ""
    try:
        with httpx.Client(timeout=8.0, follow_redirects=True, headers={"User-Agent": _UA}) as client:
            resp = client.get(url)
            if resp.status_code >= 400:
                return ""
            return _extract_text(resp.text)
    except Exception:
        logger.warning("site_catalog fetch failed: %s", url, exc_info=True)
        return ""


def site_product_brief(question: str) -> str:
    """Короткий опис продукції з silicon.com.ua + запасний текст з каталогу сайту."""
    urls = _urls_for_question(question)
    parts: list[str] = []
    for url in urls:
        body = _fetch_page(url)
        if body:
            parts.append(f"[{url}]\n{body}")
    if parts:
        return "\n\n".join(parts)[:6000]
    return _fallback_from_defaults(urls)
