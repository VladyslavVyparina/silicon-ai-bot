from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import APIRouter, FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import admin, ai, chat, orders, pay, pricing, webhooks
from app.config import get_settings
from app.database.models import Template
from app.database.session import async_session, init_db
from app.services.admin_auth import ensure_owner_exists
from app.services.catalog_sync import sync_catalog_from_site

settings = get_settings()
ROOT = Path(__file__).resolve().parent.parent
UPLOADS_ROOT = ROOT / "uploads"
STATIC_ROOT = Path(__file__).resolve().parent / "static"
UPLOADS_ROOT.mkdir(parents=True, exist_ok=True)
(UPLOADS_ROOT / "avatars").mkdir(parents=True, exist_ok=True)
(UPLOADS_ROOT / "telegram").mkdir(parents=True, exist_ok=True)
STATIC_ROOT.mkdir(parents=True, exist_ok=True)


async def _seed_templates(db: AsyncSession) -> None:
    result = await db.execute(select(Template).limit(1))
    if result.scalar_one_or_none():
        return
    defaults = [
        Template(
            keywords="доставка,нова пошта,відправка,доставити",
            response_text="По Україні — Нова Пошта або транспорт, є самовивіз. Адресу зберемо вже на оформленні.",
        ),
        Template(
            keywords="оплата,liqpay,лікпей,карткою",
            response_text=(
                "Оплата карткою онлайн або рахунок для ФОП/ТОВ. "
                "Для рахунку потрібні назва організації, ЄДРПОУ, ІПН (якщо є) і адреса."
            ),
        ),
        Template(
            keywords="графік,робота,вихідні,час роботи",
            response_text="Зверніться до менеджера для уточнення графіку роботи та термінів.",
        ),
    ]
    db.add_all(defaults)
    await db.commit()


async def _refresh_outdated_templates(db: AsyncSession) -> None:
    result = await db.execute(select(Template))
    changed = False
    for tpl in result.scalars().all():
        text = tpl.response_text or ""
        if "Дмитра Афоніна" in text or "Дмитру" in text or "підключу Дмитра" in text:
            tpl.response_text = (
                text.replace("менеджера Дмитра Афоніна", "менеджера")
                .replace("Дмитра Афоніна", "менеджера")
                .replace("Дмитру", "менеджеру")
                .replace("Дмитра", "менеджера")
            )
            changed = True
            text = tpl.response_text
        if (
            "Рахунків на реквізити немає" in text
            or "створюємо в 1С" in text
            or "Відправлення здійснюється" in text
            or "Оплата: онлайн LiqPay" in text
        ):
            if "достав" in (tpl.keywords or "").lower() or "Відправлення здійснюється" in text:
                tpl.response_text = (
                    "По Україні — Нова Пошта або транспорт, є самовивіз. "
                    "Адресу зберемо вже на оформленні."
                )
            else:
                tpl.response_text = (
                    "Оплата карткою онлайн або рахунок для ФОП/ТОВ. "
                    "Для рахунку потрібні назва організації, ЄДРПОУ, ІПН (якщо є) і адреса."
                )
            changed = True
    if changed:
        await db.commit()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    async with async_session() as db:
        await sync_catalog_from_site(db)
        await _seed_templates(db)
        await _refresh_outdated_templates(db)
        await ensure_owner_exists(db)
        from app.services.bot_settings import restore_prompt_override_from_db

        await restore_prompt_override_from_db(db)
    yield


app = FastAPI(title=settings.app_name, version="1.0.0", lifespan=lifespan)
app.mount("/uploads", StaticFiles(directory=str(UPLOADS_ROOT)), name="uploads")
app.mount("/static", StaticFiles(directory=str(STATIC_ROOT)), name="static")

api_router = APIRouter(prefix="/api")
api_router.include_router(ai.router)
api_router.include_router(pricing.router)
api_router.include_router(orders.router)
api_router.include_router(webhooks.router)
api_router.include_router(chat.router)
app.include_router(api_router)
app.include_router(admin.router)
app.include_router(pay.router)


@app.get("/health")
async def health():
    return {"status": "ok", "service": settings.app_name}


@app.get("/test-bot")
async def test_bot_page():
    """Веб-чат тестового бота для стенду 1С (без Telegram)."""
    return FileResponse(STATIC_ROOT / "test_bot.html")
