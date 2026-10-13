"""Telegram long polling — працює без HTTPS (на відміну від webhook)."""
import asyncio
import logging
import time
from pathlib import Path

from sqlalchemy import select

from app.database.models import Client
from app.database.session import async_session, init_db
from app.services.chat_handler import process_message
from app.services.order_service import OrderService
from app.services.telegram_bot import TelegramBot
from app.config import get_settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

TYPING_SECONDS = 3.0


async def _upsert_telegram_profile(
    db,
    chat_id: str,
    from_user: dict,
    bot: TelegramBot,
    *,
    chat_obj: dict | None = None,
) -> None:
    if not from_user and not chat_obj:
        return

    merged = dict(from_user or {})
    # У приватних чатах Telegram кладе ім'я також у message.chat
    if chat_obj:
        if not merged.get("first_name") and chat_obj.get("first_name"):
            merged["first_name"] = chat_obj.get("first_name")
        if merged.get("last_name") is None and chat_obj.get("last_name") is not None:
            merged["last_name"] = chat_obj.get("last_name")
        if merged.get("username") is None and chat_obj.get("username") is not None:
            merged["username"] = chat_obj.get("username")
        if not merged.get("id") and chat_obj.get("id"):
            merged["id"] = chat_obj.get("id")

    result = await db.execute(select(Client).where(Client.chat_id == str(chat_id)))
    client = result.scalar_one_or_none()
    first = merged.get("first_name") or None
    last = merged.get("last_name") if "last_name" in merged else None
    username = merged.get("username") if "username" in merged else None
    user_id = merged.get("id")

    if not client:
        client = Client(
            chat_id=str(chat_id),
            channel="telegram",
            tg_first_name=first,
            tg_last_name=last,
            tg_username=username,
        )
        db.add(client)
    else:
        # Ім'я з Telegram — окремо; CRM-ім'я (first/last) не чіпаємо
        if first:
            client.tg_first_name = first
        if last is not None:
            client.tg_last_name = last
        if username is not None:
            client.tg_username = username

    need_photo = False
    if user_id:
        photo_path = None
        if client.tg_photo_url and str(client.tg_photo_url).startswith("/uploads/"):
            photo_path = Path(__file__).resolve().parent / str(client.tg_photo_url).lstrip("/")
        need_photo = (
            not client.tg_photo_url
            or not str(client.tg_photo_url).startswith("/uploads/")
            or photo_path is None
            or not photo_path.exists()
        )
    if need_photo and user_id:
        photo = await bot.get_user_profile_photo_url(int(user_id))
        if photo:
            client.tg_photo_url = photo
    await db.commit()


async def _reply_with_typing(bot: TelegramBot, chat_id: str, text: str) -> None:
    """Статус «друкує…» ~3 сек, потім відповідь."""
    await bot.send_chat_action(chat_id, "typing")
    t0 = time.monotonic()
    try:
        async with async_session() as db:
            response = await process_message(db, chat_id, text)
    except Exception:
        logger.exception("process_message failed for %s", chat_id)
        await bot.send_message(
            chat_id,
            "Зараз не вийшло відповісти. Напишіть ще раз — або «рахунок», якщо чекаєте PDF.",
        )
        return
    elapsed = time.monotonic() - t0
    remain = TYPING_SECONDS - elapsed
    if remain > 0:
        # Telegram скидає typing ~через 5 с; тримаємо індикатор до відправки
        await bot.send_chat_action(chat_id, "typing")
        await asyncio.sleep(remain)
    if response.response:
        await bot.send_message(chat_id, response.response)
    # Окремі повідомлення (напр. кожне посилання на товар окремо — щоб у Telegram
    # підтягувалось прев'ю з фото; разом в одному повідомленні фото не показується).
    for extra in response.extra_messages or []:
        extra = (extra or "").strip()
        if extra:
            await bot.send_message(chat_id, extra)
    for doc in response.documents or []:
        try:
            path = Path(doc.path)
            if path.exists():
                await bot.send_document(
                    chat_id,
                    path.read_bytes(),
                    doc.filename or path.name,
                    caption="Рахунок у PDF. Як оплатите — скрін квитанції сюди.",
                )
            else:
                logger.warning("Invoice PDF missing: %s", doc.path)
        except Exception:
            logger.exception("Failed to send invoice PDF to %s", chat_id)


async def _payment_watch_loop() -> None:
    """Самі питаємо LiqPay, поки не вийшов час очікування (за замовчуванням 15 хв)."""
    service = OrderService()
    interval = max(10, int(getattr(get_settings(), "liqpay_watch_interval_seconds", 20) or 20))
    while True:
        await asyncio.sleep(interval)
        try:
            async with async_session() as db:
                await service.watch_pending_payments(db)
        except Exception:
            logger.exception("payment watch failed")


async def main() -> None:
    bot = TelegramBot()
    if not bot.enabled:
        raise SystemExit("TELEGRAM_BOT_TOKEN не задано в .env")

    await init_db()
    await bot.delete_webhook()
    logger.info("Telegram bot started (long polling)")
    asyncio.create_task(_payment_watch_loop())

    offset = None
    while True:
        try:
            updates = await bot.get_updates(offset=offset, timeout=30)
            for update in updates:
                offset = update["update_id"] + 1
                message = update.get("message") or update.get("edited_message")
                if not message:
                    continue

                chat_id = str(message["chat"]["id"])
                from_user = message.get("from") or {}
                chat_obj = message.get("chat") or {}

                async with async_session() as db:
                    await _upsert_telegram_profile(
                        db, chat_id, from_user, bot, chat_obj=chat_obj
                    )

                text = message.get("text")
                caption = (message.get("caption") or "").strip()
                photos = message.get("photo") or []
                document = message.get("document") or {}
                file_id = None
                if photos:
                    file_id = (photos[-1] or {}).get("file_id")
                elif document:
                    mime = (document.get("mime_type") or "").lower()
                    name = (document.get("file_name") or "").lower()
                    if mime.startswith("image/") or name.endswith(
                        (".jpg", ".jpeg", ".png", ".webp", ".pdf")
                    ):
                        file_id = document.get("file_id")
                if file_id:
                    dest = f"shot_{chat_id}_{str(file_id)[-16:]}"
                    saved = await bot.download_file(file_id, dest)
                    marker = f"[СКРІН|{saved}]" if saved else "[СКРІН]"
                    text = f"{marker} {caption or text or ''}".strip()
                elif not text:
                    text = caption or None
                if not text:
                    continue

                logger.info("Message from %s: %s", chat_id, text[:80])
                await _reply_with_typing(bot, chat_id, text)
        except Exception:
            logger.exception("Polling error")
            await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
