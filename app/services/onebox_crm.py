import logging
import uuid
from typing import Any

import httpx

from app.config import get_settings

settings = get_settings()
logger = logging.getLogger(__name__)


class OneboxCRMService:
    """Інтеграція з CRM Onebox. Якщо API недоступний — mock ID (бот не падає)."""

    def __init__(self):
        self.base_url = settings.onebox_api_url.rstrip("/")
        self.api_key = settings.onebox_api_key
        self.login = getattr(settings, "onebox_login", "") or ""
        self.rest_password = getattr(settings, "onebox_rest_api_password", "") or ""

    def _headers(self, token: str | None = None) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        elif self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _mock_contact(self) -> dict:
        return {"success": True, "contact_id": f"mock-contact-{uuid.uuid4().hex[:8]}", "mock": True}

    def _mock_deal(self) -> dict:
        return {"success": True, "deal_id": f"mock-deal-{uuid.uuid4().hex[:8]}", "mock": True}

    async def get_token(self) -> str | None:
        if settings.mock_external_apis:
            return "mock-token"
        if not self.login or not self.rest_password:
            return self.api_key or None
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.post(
                    f"{self.base_url}/api/v2/token/get/",
                    json={"login": self.login, "restapipassword": self.rest_password},
                )
                data = resp.json()
                if data.get("status") == 1 or data.get("token"):
                    return data.get("token") or data.get("data", {}).get("token")
                logger.warning("OneBox token failed: %s", data)
        except Exception as exc:
            logger.warning("OneBox token error: %s", exc)
        return None

    async def create_contact(self, data: dict[str, Any]) -> dict:
        if settings.mock_external_apis:
            return self._mock_contact()

        token = await self.get_token()
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                # OneBox v2: contact/set
                resp = await client.post(
                    f"{self.base_url}/api/v2/contact/set/",
                    headers=self._headers(token),
                    json={
                        "token": token,
                        "firstname": data.get("first_name"),
                        "lastname": data.get("last_name"),
                        "phone": data.get("phone"),
                        "email": data.get("email"),
                        "city": data.get("city"),
                    },
                )
                if resp.status_code >= 400:
                    logger.warning("OneBox create_contact HTTP %s: %s", resp.status_code, resp.text[:300])
                    return self._mock_contact()
                result = resp.json()
                contact_id = (
                    result.get("id")
                    or result.get("contact_id")
                    or result.get("data", {}).get("id")
                    or f"mock-contact-{uuid.uuid4().hex[:8]}"
                )
                return {"success": True, "contact_id": str(contact_id), "mock": False}
        except Exception as exc:
            logger.warning("OneBox create_contact fallback to mock: %s", exc)
            return self._mock_contact()

    async def create_deal(self, contact_id: str, data: dict[str, Any]) -> dict:
        if settings.mock_external_apis:
            return self._mock_deal()

        token = await self.get_token()
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    f"{self.base_url}/api/v2/order/set/",
                    headers=self._headers(token),
                    json={
                        "token": token,
                        "client": contact_id,
                        "name": data.get("title", "Замовлення з AI-бота"),
                        "sum": data.get("amount") or data.get("total_amount"),
                        "comment": data.get("comment"),
                    },
                )
                if resp.status_code >= 400:
                    logger.warning("OneBox create_deal HTTP %s: %s", resp.status_code, resp.text[:300])
                    return self._mock_deal()
                result = resp.json()
                deal_id = (
                    result.get("id")
                    or result.get("deal_id")
                    or result.get("data", {}).get("id")
                    or f"mock-deal-{uuid.uuid4().hex[:8]}"
                )
                return {"success": True, "deal_id": str(deal_id), "mock": False}
        except Exception as exc:
            logger.warning("OneBox create_deal fallback to mock: %s", exc)
            return self._mock_deal()

    async def update_deal(self, deal_id: str, data: dict[str, Any]) -> dict:
        if settings.mock_external_apis or str(deal_id).startswith("mock-"):
            return {"success": True, "deal_id": deal_id, "mock": True}
        try:
            token = await self.get_token()
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    f"{self.base_url}/api/v2/order/set/",
                    headers=self._headers(token),
                    json={"token": token, "id": deal_id, **data},
                )
                if resp.status_code >= 400:
                    logger.warning("OneBox update_deal failed: %s", resp.text[:300])
                    return {"success": False, "deal_id": deal_id}
            return {"success": True, "deal_id": deal_id}
        except Exception as exc:
            logger.warning("OneBox update_deal error: %s", exc)
            return {"success": False, "deal_id": deal_id, "error": str(exc)}

    async def register_client_in_crm(self, client_data: dict[str, Any], order_data: dict[str, Any] | None = None) -> dict:
        contact_result = await self.create_contact({
            "first_name": client_data.get("first_name"),
            "last_name": client_data.get("last_name"),
            "phone": client_data.get("phone"),
            "email": client_data.get("email"),
            "city": client_data.get("city"),
            "chat_id": client_data.get("chat_id"),
        })
        deal_result: dict[str, Any] = {"deal_id": None}
        if order_data:
            deal_result = await self.create_deal(contact_result["contact_id"], {
                "title": order_data.get("title", "Замовлення з AI-бота"),
                "amount": order_data.get("total_amount"),
                "product": order_data.get("product_type"),
                "quantity": order_data.get("quantity"),
                "comment": order_data.get("comment"),
            })
        return {
            "success": True,
            "contact_id": contact_result["contact_id"],
            "deal_id": deal_result.get("deal_id"),
            "mock": contact_result.get("mock") or deal_result.get("mock"),
        }
