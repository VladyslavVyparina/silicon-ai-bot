import logging
import re
import uuid
from pathlib import Path
from typing import Any

import httpx

from app.config import PROJECT_ROOT, get_settings

logger = logging.getLogger(__name__)

HS_API_SUFFIX = "/hs/bot-api/v1"

# In-memory mock registry for tests
_mock_counterparties: list[dict[str, Any]] = [
    {"id": "1c-001", "name": "ТОВ Сілікон", "edrpou": "12345678"},
    {"id": "1c-002", "name": "ПП Тест Компані", "edrpou": "87654321"},
]


def _api_base(raw_url: str) -> str:
    """Нормалізує ONEC_API_URL до .../{база}/hs/bot-api/v1."""
    url = (raw_url or "").strip().rstrip("/")
    if not url:
        return ""
    if "/hs/bot-api" in url:
        return url.rstrip("/")
    for junk in ("/odata/standard.odata", "/odata"):
        if junk in url:
            url = url.split(junk, 1)[0].rstrip("/")
    return f"{url}{HS_API_SUFFIX}"


def _unwrap(payload: Any) -> Any:
    if not isinstance(payload, dict):
        return payload
    for key in ("data", "result", "value", "item"):
        inner = payload.get(key)
        if isinstance(inner, (dict, list)) and inner:
            return inner
    return payload


def _pick(data: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if data.get(key) not in (None, ""):
            return data[key]
    lower = {str(k).lower(): v for k, v in data.items()}
    for key in keys:
        val = lower.get(key.lower())
        if val not in (None, ""):
            return val
    return None


def _normalize_counterparty(raw: Any) -> dict[str, Any] | None:
    payload = _unwrap(raw)
    if isinstance(payload, list):
        payload = payload[0] if payload else None
    if not isinstance(payload, dict) or not payload:
        return None
    if payload.get("found") is False:
        return None
    if payload.get("found") is True:
        nested = payload.get("counterparty") or payload.get("item") or payload.get("data")
        if isinstance(nested, dict):
            payload = {**payload, **nested}
    cid = _pick(payload, "id", "Ref_Key", "ref", "guid", "UUID")
    if cid is None:
        return None
    edrpou = _pick(payload, "edrpou", "code", "Code", "ЄДРПОУ", "Код")
    return {
        "id": str(cid),
        "name": _pick(payload, "name", "Description", "Наименование", "Найменування"),
        "edrpou": str(edrpou) if edrpou is not None else None,
        "ipn": _pick(payload, "ipn", "inn", "ІПН", "ИНН"),
        "address": _pick(payload, "address", "Адрес", "Адреса"),
        "phone": _pick(payload, "phone", "Телефон"),
        "raw": payload,
    }


def _guid_in_text(text: str) -> str | None:
    match = re.search(
        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
        text or "",
    )
    return match.group(0) if match else None


def _bas_name_candidates(name: str) -> list[str]:
    """Назви для номенклатури BAS. Перша — як у боті, далі форма 202*ширина*2."""
    raw = re.sub(r"\s+", " ", str(name or "")).strip()
    found: list[str] = []

    def add(value: str) -> None:
        value = re.sub(r"\s+", " ", value).strip()
        if value and value not in found:
            found.append(value)

    add(raw)
    lower = raw.lower()
    if "силік" in lower or "браслет" in lower:
        dims = re.search(r"(\d+)\s*[*xх×]\s*(\d+)\s*[*xх×]\s*(\d+)", lower)
        if dims:
            a, b, c = dims.groups()
        else:
            width = re.search(r"(\d+)\s*мм", lower)
            a, b, c = ("202", width.group(1), "2") if width else ("", "", "")
        if a and b and c:
            add(f"Силіконовий браслет {a}*{b}*{c} мм")
            add(f"Силіконові браслети {a}*{b}*{c} мм")
    return found or [raw or "Товар"]


def _missing_bas_product(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    message = str(payload.get("message") or "")
    match = re.search(r"Product not found in BAS:\s*(.+)", message)
    return match.group(1).strip() if match else None


def _normalize_invoice(raw: Any, fallback_number: str, counterparty_id: str) -> dict[str, Any]:
    payload = _unwrap(raw)
    if isinstance(payload, list):
        payload = payload[0] if payload else {}
    if not isinstance(payload, dict):
        payload = {}
    invoice_id = _pick(payload, "id", "Ref_Key", "ref", "guid")
    number = _pick(payload, "number", "Number", "Номер") or fallback_number
    return {
        "success": True,
        "invoice_id": str(invoice_id) if invoice_id is not None else None,
        "invoice_number": str(number),
        "counterparty_id": counterparty_id,
        "raw": payload,
    }


class OneCService:
    """HTTP-сервіс 1С: /hs/bot-api/v1/counterparty та /invoice."""

    def __init__(self):
        s = get_settings()
        self.base_url = _api_base(s.onec_api_url)
        user = s.onec_api_user or ""
        password = s.onec_api_password or ""
        self.auth = (user, password) if user else None

    def _bind(self) -> None:
        s = get_settings()
        self.base_url = _api_base(s.onec_api_url)
        user = s.onec_api_user or ""
        password = s.onec_api_password or ""
        self.auth = (user, password) if user else None

    def _mock(self) -> bool:
        return get_settings().mock_external_apis

    def configured(self) -> bool:
        self._bind()
        return bool(self.base_url and self.auth and self.auth[0] and self.auth[1])

    async def _post_first(self, client: httpx.AsyncClient, paths: tuple[str, ...], payload: dict[str, Any]):
        """POST на перший наявний шлях. Другий — якщо сервіс опублікований у множині з контракту."""
        last = None
        for path in paths:
            resp = await client.post(f"{self.base_url}/{path}", json=payload, auth=self.auth)
            last = resp
            if resp.status_code != 404:
                return resp
        return last

    @staticmethod
    def _counterparty_payload(data: dict[str, Any]) -> dict[str, Any]:
        edrpou = data.get("edrpou") or data.get("code")
        name = data.get("name") or data.get("company_name")
        ipn = data.get("ipn") or data.get("inn")
        digits = "".join(ch for ch in str(ipn or "") if ch.isdigit())
        if not digits.isdigit() or not (10 <= len(digits) <= 12):
            ipn = None
        else:
            ipn = digits
        return {
            "edrpou": edrpou,
            "code": edrpou,
            "name": name,
            "ipn": ipn,
            "inn": ipn,
            "address": data.get("company_address") or data.get("address"),
            "phone": data.get("phone"),
            "email": data.get("email"),
            "bank_account": data.get("bank_account"),
            "bank_name": data.get("bank_name"),
        }

    @staticmethod
    def _invoice_items(items: list | None) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for it in items or []:
            qty = it.get("qty") if it.get("qty") is not None else it.get("quantity")
            price = it.get("unit_price") if it.get("unit_price") is not None else it.get("price")
            name = it.get("product") or it.get("name")
            amount = it.get("amount")
            if amount is None and qty is not None and price is not None:
                try:
                    amount = round(float(qty) * float(price), 2)
                except (TypeError, ValueError):
                    amount = None
            out.append({
                "product": name,
                "name": name,
                "qty": qty,
                "quantity": qty,
                "unit_price": price,
                "price": price,
                "amount": amount,
            })
        return out

    async def find_counterparty(self, edrpou: str | None = None, name: str | None = None) -> dict | None:
        self._bind()
        if self._mock():
            for cp in _mock_counterparties:
                if edrpou and str(cp.get("edrpou") or "") == str(edrpou):
                    return cp
                if name and name.lower() in (cp.get("name") or "").lower():
                    return cp
            return None

        if not self.base_url:
            logger.warning("ONEC_API_URL порожній — пошук контрагента пропущено")
            return None

        params: dict[str, str] = {}
        if edrpou:
            params["code"] = str(edrpou).strip()
        elif name:
            params["name"] = name
        else:
            return None

        try:
            async with httpx.AsyncClient(timeout=12) as client:
                resp = await client.get(
                    f"{self.base_url}/counterparty",
                    params=params,
                    auth=self.auth,
                )
                if resp.status_code == 404:
                    return None
                resp.raise_for_status()
                try:
                    parsed = resp.json()
                except ValueError:
                    logger.warning("1С search: відповідь не JSON: %s", (resp.text or "")[:300])
                    return None
                if parsed in (None, "", [], {}):
                    return None
                return _normalize_counterparty(parsed)
        except Exception as exc:
            logger.warning("1С search failed: %s", exc)
            return None

    async def create_counterparty(self, data: dict[str, Any]) -> dict:
        existing = await self.find_counterparty(
            edrpou=data.get("edrpou"),
            name=None if data.get("edrpou") else (data.get("name") or data.get("company_name")),
        )
        if existing:
            return {
                "success": True,
                "counterparty_id": existing["id"],
                "created": False,
                "message": "Контрагент вже існує в 1С",
            }

        if self._mock():
            new_id = f"1c-{uuid.uuid4().hex[:6]}"
            cp = {
                "id": new_id,
                "name": data.get("name") or data.get("company_name"),
                "edrpou": data.get("edrpou"),
                "ipn": data.get("ipn"),
                "address": data.get("company_address") or data.get("address"),
            }
            _mock_counterparties.append(cp)
            return {
                "success": True,
                "counterparty_id": new_id,
                "created": True,
                "message": "Контрагент створено в 1С",
            }

        payload = self._counterparty_payload(data)
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await self._post_first(client, ("counterparty", "counterparties"), payload)
                if resp.status_code == 409:
                    existing_id = _guid_in_text(resp.text)
                    if existing_id:
                        return {
                            "success": True,
                            "counterparty_id": existing_id,
                            "created": False,
                            "message": "Контрагент вже існує в 1С",
                        }
                if resp.status_code >= 400:
                    logger.error("1С create counterparty HTTP %s: %s", resp.status_code, resp.text[:800])
                resp.raise_for_status()
                body = resp.json()
                if isinstance(body, dict) and body.get("success") is False:
                    raise RuntimeError(body.get("message") or "1С відхилила створення контрагента")
                normalized = _normalize_counterparty(body)
                cid = (normalized or {}).get("id") or (body.get("id") if isinstance(body, dict) else None)
                return {
                    "success": True,
                    "counterparty_id": str(cid) if cid else None,
                    "created": True,
                    "message": "Контрагент створено в 1С",
                }
        except Exception as exc:
            logger.exception("1С create counterparty failed: %s", exc)
            return {
                "success": False,
                "counterparty_id": None,
                "created": False,
                "error": str(exc),
            }

    async def create_invoice(self, counterparty_id: str, order_data: dict[str, Any]) -> dict:
        self._bind()
        fallback_number = f"INV-{uuid.uuid4().hex[:8].upper()}"
        if self._mock():
            return {
                "success": True,
                "invoice_id": f"inv-{uuid.uuid4().hex[:8]}",
                "invoice_number": fallback_number,
                "counterparty_id": counterparty_id,
                "amount": order_data.get("total_amount"),
                "mock": True,
                "posted_to_1c": False,
            }

        base_items = self._invoice_items(order_data.get("items") or [])
        candidate_lists = [
            _bas_name_candidates(str(item.get("product") or item.get("name") or ""))
            for item in base_items
        ] or [["Товар"]]
        choice = [0 for _ in candidate_lists]
        payload = {
            "counterparty_id": counterparty_id,
            "amount": order_data.get("total_amount") or order_data.get("amount"),
            "description": order_data.get("description", "Замовлення Silicon"),
            "comment": order_data.get("comment")
            or order_data.get("description")
            or "Створено Telegram-ботом",
            "source": "telegram-bot",
            "order_id": order_data.get("order_id"),
            "buyer": order_data.get("buyer") or {},
            "items": base_items,
        }
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = None
                body: Any = {}
                for _attempt in range(12):
                    items = []
                    for item, names, picked in zip(base_items, candidate_lists, choice):
                        row = dict(item)
                        name = names[min(picked, len(names) - 1)]
                        row["product"] = name
                        row["name"] = name
                        items.append(row)
                    payload["items"] = items
                    resp = await self._post_first(client, ("invoice", "invoices"), payload)
                    try:
                        body = resp.json()
                    except ValueError:
                        body = {}
                    missing = _missing_bas_product(body) if resp.status_code == 422 else None
                    if not missing:
                        break
                    moved = False
                    for index, row in enumerate(items):
                        names = candidate_lists[index]
                        if row.get("product") == missing and choice[index] < len(names) - 1:
                            choice[index] += 1
                            moved = True
                            break
                    if not moved:
                        break
                if resp is None:
                    raise RuntimeError("1С не відповіла на створення рахунку")
                if resp.status_code >= 400:
                    logger.error("1С create invoice HTTP %s: %s", resp.status_code, resp.text[:800])
                resp.raise_for_status()
                if isinstance(body, dict) and body.get("success") is False:
                    raise RuntimeError(body.get("message") or "1С відхилила створення рахунку")
                result = _normalize_invoice(body, fallback_number, counterparty_id)
                if not result.get("invoice_id"):
                    raise RuntimeError("1С не повернула id рахунку")
                result["amount"] = payload["amount"]
                result["mock"] = False
                result["posted_to_1c"] = True
                return result
        except Exception as exc:
            logger.exception("1С create invoice failed: %s", exc)
            return {
                "success": False,
                "invoice_id": None,
                "invoice_number": None,
                "counterparty_id": counterparty_id,
                "amount": payload["amount"],
                "mock": False,
                "posted_to_1c": False,
                "error": str(exc),
            }

    async def download_invoice_pdf(self, invoice_id: str, order_id: int | None = None) -> Path | None:
        """GET /invoice/{id}/pdf — офіційний PDF з 1С."""
        self._bind()
        if self._mock() or not invoice_id or not self.base_url:
            return None
        url = f"{self.base_url}/invoice/{invoice_id}/pdf"
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.get(url, auth=self.auth)
            resp.raise_for_status()
            body = resp.content
            content_type = (resp.headers.get("content-type") or "").lower()
        if "pdf" not in content_type and not body.startswith(b"%PDF"):
            logger.warning("1С PDF: неочікуваний тип %s", content_type)
            return None
        invoices_dir = PROJECT_ROOT / "data" / "invoices"
        invoices_dir.mkdir(parents=True, exist_ok=True)
        safe = "".join(ch for ch in str(invoice_id) if ch.isalnum() or ch in "-_")
        path = invoices_dir / f"invoice_{order_id or 'x'}_{safe}_1c.pdf"
        path.write_bytes(body)
        return path


def reset_mock_counterparties() -> None:
    global _mock_counterparties
    _mock_counterparties = [
        {"id": "1c-001", "name": "ТОВ Сілікон", "edrpou": "12345678"},
        {"id": "1c-002", "name": "ПП Тест Компані", "edrpou": "87654321"},
    ]
