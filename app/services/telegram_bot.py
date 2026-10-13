import logging
from pathlib import Path

import httpx

from app.config import get_settings

settings = get_settings()
logger = logging.getLogger(__name__)

UPLOADS_TG = Path(__file__).resolve().parent.parent.parent / "uploads" / "telegram"
UPLOADS_TG.mkdir(parents=True, exist_ok=True)


class TelegramBot:
    """Пряма інтеграція з Telegram Bot API (без SendPulse)."""

    def __init__(self, token: str | None = None):
        self.token = token or settings.telegram_bot_token
        self.base_url = f"https://api.telegram.org/bot{self.token}" if self.token else ""
        self.file_base = f"https://api.telegram.org/file/bot{self.token}" if self.token else ""

    @property
    def enabled(self) -> bool:
        return bool(self.token)

    async def send_chat_action(self, chat_id: str | int, action: str = "typing") -> dict:
        """Показує статус «друкує…» у Telegram."""
        if not self.enabled:
            return {"success": False, "error": "Telegram token not configured"}
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    f"{self.base_url}/sendChatAction",
                    json={"chat_id": int(chat_id), "action": action},
                )
                data = resp.json()
                if not data.get("ok"):
                    return {"success": False, "error": data.get("description")}
                return {"success": True}
        except Exception as exc:
            logger.warning("sendChatAction failed: %s", exc)
            return {"success": False, "error": str(exc)}

    async def send_message(self, chat_id: str | int, text: str) -> dict:
        if not self.enabled:
            return {"success": False, "error": "Telegram token not configured"}

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{self.base_url}/sendMessage",
                json={"chat_id": int(chat_id), "text": text},
            )
            data = resp.json()
            if not data.get("ok"):
                logger.error("Telegram send error: %s", data)
                return {"success": False, "error": data.get("description")}
            return {"success": True}

    async def send_photo(self, chat_id: str | int, file_bytes: bytes, filename: str, caption: str = "") -> dict:
        if not self.enabled:
            return {"success": False, "error": "Telegram token not configured"}
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{self.base_url}/sendPhoto",
                data={"chat_id": int(chat_id), "caption": caption[:1024]},
                files={"photo": (filename, file_bytes)},
            )
            data = resp.json()
            if not data.get("ok"):
                return {"success": False, "error": data.get("description")}
            return {"success": True}

    async def send_video(self, chat_id: str | int, file_bytes: bytes, filename: str, caption: str = "") -> dict:
        if not self.enabled:
            return {"success": False, "error": "Telegram token not configured"}
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                f"{self.base_url}/sendVideo",
                data={"chat_id": int(chat_id), "caption": caption[:1024]},
                files={"video": (filename, file_bytes)},
            )
            data = resp.json()
            if not data.get("ok"):
                return {"success": False, "error": data.get("description")}
            return {"success": True}

    async def send_document(self, chat_id: str | int, file_bytes: bytes, filename: str, caption: str = "") -> dict:
        if not self.enabled:
            return {"success": False, "error": "Telegram token not configured"}
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                f"{self.base_url}/sendDocument",
                data={"chat_id": int(chat_id), "caption": caption[:1024]},
                files={"document": (filename, file_bytes)},
            )
            data = resp.json()
            if not data.get("ok"):
                return {"success": False, "error": data.get("description")}
            return {"success": True}

    async def get_chat(self, chat_id: str | int) -> dict | None:
        """Профіль приватного чату: first_name, last_name, username."""
        if not self.enabled:
            return None
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.get(
                    f"{self.base_url}/getChat",
                    params={"chat_id": int(chat_id)},
                )
                data = resp.json()
                if not data.get("ok"):
                    return None
                return data.get("result") or None
        except Exception as exc:
            logger.warning("getChat failed: %s", exc)
            return None

    async def get_user_profile_photo_url(self, user_id: int) -> str | None:
        if not self.enabled:
            return None
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(
                    f"{self.base_url}/getUserProfilePhotos",
                    params={"user_id": int(user_id), "limit": 1},
                )
                data = resp.json()
                photos = (data.get("result") or {}).get("photos") or []
                if not photos or not photos[0]:
                    return None
                file_id = photos[0][-1]["file_id"]
                file_resp = await client.get(f"{self.base_url}/getFile", params={"file_id": file_id})
                file_data = file_resp.json()
                path = (file_data.get("result") or {}).get("file_path")
                if not path:
                    return None
                # download locally for admin panel
                dl = await client.get(f"{self.file_base}/{path}")
                if dl.status_code != 200:
                    return None
                ext = Path(path).suffix or ".jpg"
                local_name = f"tg_{user_id}{ext}"
                dest = UPLOADS_TG / local_name
                dest.write_bytes(dl.content)
                return f"/uploads/telegram/{local_name}"
        except Exception as exc:
            logger.warning("get profile photo failed: %s", exc)
            return None

    async def download_file(self, file_id: str, dest_name: str) -> str | None:
        """Завантажує файл Telegram у uploads/telegram. Повертає URL для адмінки."""
        if not self.enabled or not file_id:
            return None
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                file_resp = await client.get(f"{self.base_url}/getFile", params={"file_id": file_id})
                file_data = file_resp.json()
                path = (file_data.get("result") or {}).get("file_path")
                if not path:
                    return None
                dl = await client.get(f"{self.file_base}/{path}")
                if dl.status_code != 200:
                    return None
                ext = Path(path).suffix or Path(dest_name).suffix or ".jpg"
                local_name = dest_name if dest_name.endswith(ext) else f"{dest_name}{ext}"
                dest = UPLOADS_TG / local_name
                dest.write_bytes(dl.content)
                return f"/uploads/telegram/{local_name}"
        except Exception as exc:
            logger.warning("download telegram file failed: %s", exc)
            return None

    async def enrich_client_profile(
        self,
        chat_id: str | int,
        *,
        from_user: dict | None = None,
        force_photo: bool = False,
    ) -> dict:
        """Збирає ім'я / нік / фото з message.from + getChat + getUserProfilePhotos."""
        profile: dict = {
            "tg_first_name": None,
            "tg_last_name": None,
            "tg_username": None,
            "tg_photo_url": None,
            "user_id": None,
        }
        src = from_user or {}
        if src.get("first_name"):
            profile["tg_first_name"] = src.get("first_name")
        if src.get("last_name") is not None:
            profile["tg_last_name"] = src.get("last_name") or None
        if src.get("username") is not None:
            profile["tg_username"] = src.get("username") or None
        if src.get("id"):
            profile["user_id"] = int(src["id"])

        chat = await self.get_chat(chat_id)
        if chat:
            if not profile["tg_first_name"] and chat.get("first_name"):
                profile["tg_first_name"] = chat.get("first_name")
            if profile["tg_last_name"] is None and chat.get("last_name") is not None:
                profile["tg_last_name"] = chat.get("last_name") or None
            if profile["tg_username"] is None and chat.get("username") is not None:
                profile["tg_username"] = chat.get("username") or None
            if not profile["user_id"] and chat.get("id"):
                profile["user_id"] = int(chat["id"])

        uid = profile["user_id"] or (int(chat_id) if str(chat_id).lstrip("-").isdigit() else None)
        if uid and (force_photo or not profile.get("tg_photo_url")):
            photo = await self.get_user_profile_photo_url(int(uid))
            if photo:
                profile["tg_photo_url"] = photo
        return profile

    async def delete_webhook(self) -> None:
        if not self.enabled:
            return
        async with httpx.AsyncClient(timeout=30) as client:
            await client.post(f"{self.base_url}/deleteWebhook")

    async def get_updates(self, offset: int | None = None, timeout: int = 30) -> list[dict]:
        if not self.enabled:
            return []
        params: dict = {"timeout": timeout}
        if offset is not None:
            params["offset"] = offset
        async with httpx.AsyncClient(timeout=timeout + 25) as client:
            resp = await client.get(f"{self.base_url}/getUpdates", params=params)
            data = resp.json()
            if not data.get("ok"):
                logger.error("Telegram getUpdates error: %s", data)
                return []
            return data.get("result", [])

    async def send_ttn_notification(self, chat_id: str, ttn_number: str, ttn_url: str) -> dict:
        text = (
            "Дякуємо за замовлення в компанії Silicon!\n\n"
            "Ваше замовлення виконано та передано на відправку Новою поштою.\n"
            f"Номер ТТН: {ttn_number}\n"
            f"Відстежити: {ttn_url}\n\n"
            "З повагою,\nОлег, консультант Silicon"
        )
        return await self.send_message(chat_id, text)

    async def send_payment_confirmation(self, chat_id: str, amount: float) -> dict:
        text = (
            "Бачу вашу оплату 👍 Беремо замовлення в роботу."
        )
        return await self.send_message(chat_id, text)
