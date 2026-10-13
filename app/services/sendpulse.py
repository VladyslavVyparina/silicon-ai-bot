from typing import Any

import httpx

from app.config import get_settings

settings = get_settings()


class SendPulseService:
    """SendPulse Send API — проактивні повідомлення клієнту в чат."""

    TOKEN_URL = "https://api.sendpulse.com/oauth/access_token"
    SEND_URL = "https://api.sendpulse.com/chatbots/send"

    def __init__(self):
        self.client_id = settings.sendpulse_client_id
        self.client_secret = settings.sendpulse_client_secret
        self.bot_id = settings.sendpulse_bot_id
        self._token: str | None = None

    async def _get_token(self) -> str:
        if settings.mock_external_apis:
            return "mock-token"

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                self.TOKEN_URL,
                json={
                    "grant_type": "client_credentials",
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                },
            )
            resp.raise_for_status()
            self._token = resp.json()["access_token"]
            return self._token

    async def send_message(self, chat_id: str, text: str, channel: str = "telegram") -> dict:
        if settings.mock_external_apis:
            return {"success": True, "chat_id": chat_id, "text": text, "mock": True}

        token = await self._get_token()
        payload = {
            "bot_id": self.bot_id,
            "channel": channel,
            "contact_id": chat_id,
            "message": {"type": "text", "text": text},
        }
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                self.SEND_URL,
                headers={"Authorization": f"Bearer {token}"},
                json=payload,
            )
            resp.raise_for_status()
            return {"success": True, "result": resp.json()}

    async def send_ttn_notification(self, chat_id: str, ttn_number: str, ttn_url: str, channel: str = "telegram") -> dict:
        text = (
            f"Дякуємо за замовлення в компанії Silicon! 🎉\n\n"
            f"Ваше замовлення виконано та передано на відправку.\n"
            f"Номер ТТН: {ttn_number}\n"
            f"Відстежити посилку: {ttn_url}\n\n"
            f"З повагою, команда Silicon"
        )
        return await self.send_message(chat_id, text, channel)

    async def send_payment_confirmation(self, chat_id: str, amount: float, channel: str = "telegram") -> dict:
        text = (
            f"✅ Оплату отримано!\n\n"
            f"Сума: {amount} грн.\n"
            f"Дякуємо! Ваше замовлення передано у виробництво.\n\n"
            f"З повагою, команда Silicon"
        )
        return await self.send_message(chat_id, text, channel)
