import base64
import hashlib
import json
import logging
import uuid
from typing import Any
from urllib.parse import urlencode

import httpx

from app.config import get_settings

settings = get_settings()
logger = logging.getLogger(__name__)


class LiqPayService:
    """Створення платежів через LiqPay, статус і перевірка webhook-підпису."""

    CHECKOUT_URL = "https://www.liqpay.ua/api/3/checkout"
    REQUEST_URL = "https://www.liqpay.ua/api/request"

    def __init__(self):
        self.public_key = settings.liqpay_public_key or "sandbox_i0000000"
        self.private_key = settings.liqpay_private_key or "sandbox_private_key"
        self.sandbox = settings.liqpay_sandbox

    def _sign(self, data_b64: str) -> str:
        sign_string = self.private_key + data_b64 + self.private_key
        return base64.b64encode(hashlib.sha1(sign_string.encode()).digest()).decode()

    def _encode(self, params: dict[str, Any]) -> tuple[str, str]:
        data_json = json.dumps(params, separators=(",", ":"), ensure_ascii=False)
        data_b64 = base64.b64encode(data_json.encode("utf-8")).decode()
        return data_b64, self._sign(data_b64)

    def create_payment(
        self,
        order_id: str,
        amount: float,
        description: str,
        result_url: str | None = None,
        liqpay_order_id: str | None = None,
    ) -> dict:
        if amount is None or float(amount) <= 0:
            return {"success": False, "error": "Некоректна сума платежу"}

        liqpay_order_id = (liqpay_order_id or "").strip() or f"silicon-{order_id}-{uuid.uuid4().hex[:6]}"
        params: dict[str, Any] = {
            "public_key": self.public_key,
            "version": 3,
            "action": "pay",
            "amount": round(float(amount), 2),
            "currency": "UAH",
            "description": description[:500],
            "order_id": liqpay_order_id,
            "sandbox": 1 if self.sandbox or settings.mock_external_apis else 0,
        }
        from app.config import is_https_url

        # LiqPay часто відхиляє checkout, якщо result_url/server_url — http://IP
        if is_https_url(result_url):
            params["result_url"] = result_url
        server = (settings.liqpay_server_url or "").strip()
        if is_https_url(server):
            params["server_url"] = server

        data_b64, signature = self._encode(params)
        query = urlencode({"data": data_b64, "signature": signature})
        payment_url = f"{self.CHECKOUT_URL}?{query}"

        return {
            "success": True,
            "payment_url": payment_url,
            "liqpay_order_id": liqpay_order_id,
            "data": data_b64,
            "signature": signature,
            "mock": bool(settings.mock_external_apis),
        }

    def fetch_status(self, liqpay_order_id: str) -> dict:
        """Запит статусу платежу в LiqPay (не чекаємо лише webhook)."""
        if not liqpay_order_id:
            return {"success": False, "status": "error"}
        if settings.mock_external_apis:
            return {"success": True, "status": "wait_accept", "mock": True}
        params = {
            "action": "status",
            "version": 3,
            "public_key": self.public_key,
            "order_id": str(liqpay_order_id),
        }
        data_b64, signature = self._encode(params)
        try:
            with httpx.Client(timeout=12) as client:
                resp = client.post(
                    self.REQUEST_URL,
                    data={"data": data_b64, "signature": signature},
                )
            payload = resp.json()
        except Exception as exc:
            logger.warning("LiqPay status error: %s", exc)
            return {"success": False, "status": "error"}
        status = str(payload.get("status") or "")
        return {"success": True, "status": status, "raw": payload}

    @staticmethod
    def is_paid_status(status: str | None) -> bool:
        return str(status or "").lower() in {"success", "sandbox"}

    def verify_webhook(self, data: str, signature: str) -> bool:
        expected = self._sign(data)
        return expected == signature

    def decode_webhook(self, data: str) -> dict:
        decoded = base64.b64decode(data).decode()
        return json.loads(decoded)
