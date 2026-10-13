"""Синхронізація каталогу з сайту silicon.com.ua."""

from datetime import datetime

from bs4 import BeautifulSoup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database.models import CatalogItem

DEFAULT_CATALOG = [
    {
        "name": "Силіконові браслети",
        "category": "Браслети",
        "description": "Брендовані силіконові браслети з нанесенням логотипу. Розміри 5, 12, 20 мм.",
        "url": "https://silicon.com.ua/silikonovi-braslety/",
        "price_info": "Від 8.50 грн/шт при тираже 1000+",
    },
    {
        "name": "Світловідбиваючі браслети",
        "category": "Браслети",
        "description": "Slap-браслети зі світловідбиваючої плівки, 300x30 мм та 400x30 мм.",
        "url": "https://silicon.com.ua/svitlovidbivayuchi-braslety/",
        "price_info": "Від 32 грн/шт при тираже 3000+",
    },
    {
        "name": "Контрольні браслети Tyvek",
        "category": "Браслети",
        "description": "Одноразові контрольні браслети для заходів з нанесенням логотипу.",
        "url": "https://silicon.com.ua/kontrolni-braslety-tyvek/",
        "price_info": "Індивідуальний прорахунок",
    },
    {
        "name": "Тканинні браслети",
        "category": "Браслети",
        "description": "Браслети з тканинної стрічки для фестивалів, концертів, конференцій.",
        "url": "https://silicon.com.ua/tkaninni-braslety/",
        "price_info": "Індивідуальний прорахунок",
    },
    {
        "name": "Силіконові слеп-браслети",
        "category": "Браслети",
        "description": "Slap-браслети з силікону з можливістю брендування.",
        "url": "https://silicon.com.ua/silikonovi-slep-braslety/",
        "price_info": "Індивідуальний прорахунок",
    },
    {
        "name": "Еко-чашки з бамбука",
        "category": "Посуд",
        "description": "Багаторазові чашки 350 мл та 450 мл з нанесенням логотипу.",
        "url": "https://silicon.com.ua/eko-chashky/",
        "price_info": "Від 1 шт з брендуванням",
    },
    {
        "name": "Шнурки для бейджів",
        "category": "Аксесуари",
        "description": "Стрічки 15-30 мм з друком логотипу, різні карабіни та фастекси.",
        "url": "https://silicon.com.ua/shnurky-dlya-beydzha/",
        "price_info": "Індивідуальний прорахунок",
    },
    {
        "name": "Брелоки",
        "category": "Аксесуари",
        "description": "Брендовані брелоки зі стрічкою 20 або 25 мм.",
        "url": "https://silicon.com.ua/breloky/",
        "price_info": "Індивідуальний прорахунок",
    },
    {
        "name": "RFID-браслети",
        "category": "Браслети",
        "description": "Браслети з RFID-чипом для контролю доступу.",
        "url": "https://silicon.com.ua/rfid-braslety/",
        "price_info": "Індивідуальний прорахунок",
    },
    {
        "name": "Нанесення логотипу",
        "category": "Послуги",
        "description": "Нанесення логотипу на продукцію замовника.",
        "url": "https://silicon.com.ua/",
        "price_info": "За запитом",
    },
]


async def sync_catalog_from_site(db: AsyncSession) -> dict:
    """Спроба завантажити каталог з сайту; fallback на вбудований каталог."""
    items = DEFAULT_CATALOG
    fetched_from_site = False

    if not get_settings().mock_external_apis:
        try:
            import httpx

            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                resp = await client.get("https://silicon.com.ua/")
                if resp.status_code == 200:
                    soup = BeautifulSoup(resp.text, "lxml")
                    parsed = _parse_homepage(soup)
                    if parsed:
                        items = parsed
                        fetched_from_site = True
        except Exception:
            pass

    count = 0
    for item_data in items:
        existing = await db.execute(
            select(CatalogItem).where(CatalogItem.name == item_data["name"])
        )
        item = existing.scalar_one_or_none()
        if item:
            item.description = item_data.get("description")
            item.price_info = item_data.get("price_info")
            item.url = item_data.get("url")
            item.updated_at = datetime.utcnow()
        else:
            db.add(CatalogItem(**item_data))
        count += 1

    await db.commit()
    return {"synced": count, "from_site": fetched_from_site}


def _parse_homepage(soup: BeautifulSoup) -> list[dict]:
    items = []
    for link in soup.select("a[href]"):
        href = link.get("href", "")
        text = link.get_text(strip=True)
        if not text or len(text) < 5:
            continue
        if any(kw in href.lower() for kw in ["braslet", "chashk", "shnurk", "brelok", "rfid", "tyvek"]):
            items.append({
                "name": text[:256],
                "category": "Каталог",
                "description": text,
                "url": href if href.startswith("http") else f"https://silicon.com.ua{href}",
                "price_info": None,
            })
    return items[:30]
