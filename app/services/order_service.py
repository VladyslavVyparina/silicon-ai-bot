import json
import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_public_base_url, get_settings
from app.database.models import (
    Client,
    DeliveryInfo,
    Order,
    OrderStatus,
    Payment,
    PaymentStatus,
    WebhookLog,
)
from app.services.liqpay import LiqPayService
from app.services.nova_poshta import NovaPoshtaService
from app.services.onebox_crm import OneboxCRMService
from app.services.onec import OneCService
from app.services.telegram_bot import TelegramBot

logger = logging.getLogger(__name__)


class OrderService:
    """Оркестрація замовлень: CRM, 1С, оплата, доставка, сповіщення."""

    def __init__(self):
        self.crm = OneboxCRMService()
        self.onec = OneCService()
        self.liqpay = LiqPayService()
        self.nova_poshta = NovaPoshtaService()
        self.telegram = TelegramBot()

    async def get_or_create_client(self, db: AsyncSession, chat_id: str, data: dict[str, Any] | None) -> Client:
        data = data or {}
        result = await db.execute(select(Client).where(Client.chat_id == chat_id))
        client = result.scalar_one_or_none()
        if client:
            for field in ("first_name", "last_name", "phone", "email", "city", "edrpou", "company_name"):
                if data.get(field):
                    setattr(client, field, data[field])
            return client

        client = Client(
            chat_id=chat_id,
            channel=data.get("channel", "telegram"),
            first_name=data.get("first_name"),
            last_name=data.get("last_name"),
            phone=data.get("phone"),
            email=data.get("email"),
            city=data.get("city"),
            edrpou=data.get("edrpou"),
            company_name=data.get("company_name"),
        )
        db.add(client)
        await db.flush()
        return client

    async def register_client_in_crm(self, db: AsyncSession, chat_id: str, client_data: dict[str, Any]) -> dict:
        client = await self.get_or_create_client(db, chat_id, client_data)

        onec_result = None
        if client_data.get("edrpou") or client_data.get("company_name"):
            onec_result = await self.onec.create_counterparty({
                "edrpou": client_data.get("edrpou"),
                "name": client_data.get("company_name"),
                "phone": client_data.get("phone"),
                "email": client_data.get("email"),
            })
            if onec_result.get("counterparty_id"):
                client.onec_counterparty_id = onec_result["counterparty_id"]

        crm_result = await self.crm.register_client_in_crm(
            {**client_data, "chat_id": chat_id},
            order_data=client_data.get("order") or {"title": "Консультація AI-бот"},
        )
        if crm_result.get("contact_id"):
            client.onebox_contact_id = crm_result["contact_id"]
        if crm_result.get("deal_id"):
            client.onebox_deal_id = crm_result["deal_id"]

        await db.commit()
        return {
            "success": True,
            "client_id": client.id,
            "onebox_contact_id": client.onebox_contact_id,
            "onebox_deal_id": client.onebox_deal_id,
            "onec_counterparty_id": client.onec_counterparty_id,
            "onec_created": onec_result.get("created") if onec_result else None,
        }

    async def create_order(
        self, db: AsyncSession, chat_id: str, order_data: dict[str, Any]
    ) -> Order:
        client = await self.get_or_create_client(db, chat_id, order_data.get("client") or {})
        order = Order(
            client_id=client.id,
            product_type=order_data.get("product_type"),
            product_params=json.dumps(order_data.get("params", {}), ensure_ascii=False),
            quantity=order_data.get("quantity"),
            unit_price=order_data.get("unit_price"),
            total_amount=order_data.get("total_amount"),
            production_days=order_data.get("production_days"),
            comment=order_data.get("comment"),
            status=OrderStatus.NEW,
        )
        db.add(order)
        await db.flush()

        if not client.onebox_deal_id:
            try:
                crm_result = await self.crm.register_client_in_crm(
                    {"chat_id": chat_id, **(order_data.get("client") or {})},
                    order_data={
                        "title": f"Замовлення #{order.id}",
                        "product_type": order.product_type,
                        "quantity": order.quantity,
                        "total_amount": order.total_amount,
                        "comment": order.comment,
                    },
                )
                if crm_result.get("deal_id"):
                    client.onebox_deal_id = crm_result["deal_id"]
            except Exception:
                pass

        await db.commit()
        await db.refresh(order)
        return order

    async def save_delivery_info(self, db: AsyncSession, chat_id: str, delivery_data: dict[str, Any]) -> DeliveryInfo:
        from app.services.delivery_rules import build_recipient_name, validate_delivery_payload

        payload = dict(delivery_data)
        ok, errors = validate_delivery_payload(payload)
        if not ok:
            raise ValueError("Неповні дані доставки: " + ", ".join(errors))
        payload["recipient_name"] = build_recipient_name(payload)

        client = await self.get_or_create_client(db, chat_id, payload)
        result = await db.execute(select(DeliveryInfo).where(DeliveryInfo.client_id == client.id))
        delivery = result.scalar_one_or_none()

        fields = (
            "delivery_type", "recipient_last_name", "recipient_first_name", "recipient_patronymic",
            "recipient_name", "recipient_phone", "city", "city_ref",
            "warehouse", "warehouse_ref", "street_address",
            "weight_kg", "seats_amount", "description",
        )
        if delivery:
            for field in fields:
                if payload.get(field) is not None:
                    setattr(delivery, field, payload[field])
        else:
            delivery = DeliveryInfo(
                client_id=client.id,
                delivery_type=payload.get("delivery_type") or "warehouse",
                recipient_last_name=payload.get("recipient_last_name"),
                recipient_first_name=payload.get("recipient_first_name"),
                recipient_patronymic=payload.get("recipient_patronymic"),
                recipient_name=payload["recipient_name"],
                recipient_phone=payload["recipient_phone"],
                city=payload["city"],
                city_ref=payload.get("city_ref"),
                warehouse=payload.get("warehouse"),
                warehouse_ref=payload.get("warehouse_ref"),
                street_address=payload.get("street_address"),
                weight_kg=payload.get("weight_kg", 1.0),
                seats_amount=payload.get("seats_amount", 1),
                description=payload.get("description"),
            )
            db.add(delivery)

        await db.commit()
        await db.refresh(delivery)
        return delivery

    async def create_ttn(self, db: AsyncSession, order_id: int) -> dict:
        result = await db.execute(select(Order).where(Order.id == order_id))
        order = result.scalar_one_or_none()
        if not order:
            return {"success": False, "error": "Замовлення не знайдено"}

        client_result = await db.execute(select(Client).where(Client.id == order.client_id))
        client = client_result.scalar_one()
        delivery_result = await db.execute(select(DeliveryInfo).where(DeliveryInfo.client_id == client.id))
        delivery = delivery_result.scalar_one_or_none()
        if not delivery:
            return {"success": False, "error": "Дані доставки не заповнені"}

        city_ref = delivery.city_ref
        if not city_ref:
            cities = await self.nova_poshta.search_city(delivery.city)
            if cities:
                city_ref = cities[0]["ref"]
                delivery.city_ref = city_ref

        warehouse_ref = delivery.warehouse_ref
        delivery_type = getattr(delivery, "delivery_type", None) or "warehouse"
        if delivery_type != "address" and not warehouse_ref and city_ref:
            warehouses = await self.nova_poshta.get_warehouses(city_ref)
            if warehouses:
                # Prefer matching warehouse number from free-text if provided
                warehouse_ref = warehouses[0]["ref"]
                if delivery.warehouse:
                    needle = str(delivery.warehouse).lower()
                    for wh in warehouses:
                        if needle in (wh.get("name") or "").lower():
                            warehouse_ref = wh["ref"]
                            break
                delivery.warehouse_ref = warehouse_ref

        ttn_result = await self.nova_poshta.create_ttn({
            "delivery_type": delivery_type,
            "recipient_name": delivery.recipient_name,
            "recipient_last_name": delivery.recipient_last_name,
            "recipient_first_name": delivery.recipient_first_name,
            "recipient_patronymic": delivery.recipient_patronymic,
            "recipient_phone": delivery.recipient_phone,
            "city_ref": city_ref,
            "warehouse_ref": warehouse_ref,
            "street_address": getattr(delivery, "street_address", None),
            "weight_kg": delivery.weight_kg,
            "seats_amount": delivery.seats_amount,
            "description": delivery.description or f"Замовлення Silicon #{order.id}",
            "cost": order.total_amount or 100,
        })

        if not ttn_result.get("success"):
            return ttn_result

        order.ttn_number = ttn_result["ttn_number"]
        order.ttn_url = ttn_result["ttn_url"]
        order.status = OrderStatus.SHIPPED

        if client.onebox_deal_id:
            await self.crm.update_deal(client.onebox_deal_id, {
                "ttn": order.ttn_number,
                "status": "shipped",
            })

        await db.commit()
        return {"success": True, "ttn_number": order.ttn_number, "ttn_url": order.ttn_url}

    @staticmethod
    def payment_description(order: Order) -> str:
        """Опис для LiqPay / рахунку: кожна позиція окремо + сума."""
        from app.services.pricing_calculator import format_money

        cart: list = []
        if order.product_params:
            try:
                params = json.loads(order.product_params)
                if isinstance(params, dict) and isinstance(params.get("cart"), list):
                    cart = params["cart"]
            except Exception:
                cart = []

        parts: list[str] = []
        if cart:
            for i, row in enumerate(cart, 1):
                name = str(row.get("product_name") or "Товар").strip()
                qty = int(row.get("quantity") or 0)
                amount = format_money(row.get("total_amount"))
                parts.append(f"{i}. {name} — {qty} шт ({amount} грн)")
            total = format_money(order.total_amount)
            parts.append(f"Разом {total} грн")
        else:
            if order.product_type:
                parts.append(str(order.product_type).strip())
            if order.quantity:
                parts.append(f"{int(order.quantity)} шт")
            if order.total_amount:
                parts.append(f"{format_money(order.total_amount)} грн")
        parts.append(f"Silicon#{order.id}")
        # LiqPay description — до 500 символів; короткі рядки щоб вмістились усі позиції
        text = "; ".join(parts)
        if len(text) > 480 and cart:
            short = []
            for i, row in enumerate(cart, 1):
                name = str(row.get("product_name") or "Товар").strip()
                qty = int(row.get("quantity") or 0)
                short.append(f"{i}.{name} {qty}шт")
            short.append(f"Разом {format_money(order.total_amount)} грн")
            short.append(f"Silicon#{order.id}")
            text = "; ".join(short)
        return text[:500]

    async def create_payment(self, db: AsyncSession, order_id: int, *, force_new: bool = False) -> dict:
        result = await db.execute(select(Order).where(Order.id == order_id))
        order = result.scalar_one_or_none()
        if not order or not order.total_amount:
            return {"success": False, "error": "Замовлення або сума не знайдені"}
        if float(order.total_amount) <= 0:
            return {"success": False, "error": "Некоректна сума платежу"}

        existing = (
            await db.execute(
                select(Payment).where(Payment.order_id == order.id).order_by(Payment.id.desc())
            )
        ).scalars().first()
        if existing and existing.status == PaymentStatus.SUCCESS:
            return {
                "success": True,
                "already_paid": True,
                "payment_url": existing.payment_url,
                "liqpay_order_id": existing.liqpay_payment_id,
            }
        if (
            existing
            and existing.status == PaymentStatus.PENDING
            and existing.payment_url
            and not force_new
        ):
            return {
                "success": True,
                "payment_url": existing.payment_url,
                "liqpay_order_id": existing.liqpay_payment_id,
                "reused": True,
            }

        desc = self.payment_description(order)
        payment = Payment(
            order_id=order.id,
            amount=order.total_amount,
            liqpay_payment_id="",
            payment_url="",
            status=PaymentStatus.PENDING,
        )
        order.status = OrderStatus.AWAITING_PAYMENT
        db.add(payment)
        await db.flush()

        base = get_public_base_url()
        short_url = f"{base}/pay/{payment.id}"
        payment_result = self.liqpay.create_payment(
            order_id=str(order.id),
            amount=float(order.total_amount),
            description=desc,
        )
        if not payment_result.get("success"):
            await db.rollback()
            return payment_result

        payment.liqpay_payment_id = payment_result["liqpay_order_id"]
        payment.liqpay_data = payment_result.get("data")
        payment.liqpay_signature = payment_result.get("signature")
        payment.payment_url = short_url
        order.liqpay_order_id = payment_result["liqpay_order_id"]
        await db.commit()

        return {
            "success": True,
            "payment_url": short_url,
            "payment_id": payment.id,
            "liqpay_order_id": payment_result["liqpay_order_id"],
            "data": payment_result.get("data"),
            "signature": payment_result.get("signature"),
        }

    async def create_invoice(self, db: AsyncSession, order_id: int, requisites: dict[str, Any]) -> dict:
        result = await db.execute(select(Order).where(Order.id == order_id))
        order = result.scalar_one_or_none()
        if not order:
            return {"success": False, "error": "Замовлення не знайдено"}

        client_result = await db.execute(select(Client).where(Client.id == order.client_id))
        client = client_result.scalar_one()

        ipn_raw = str(requisites.get("ipn") or "").strip()
        ipn_digits = "".join(ch for ch in ipn_raw if ch.isdigit())
        edrpou = str(requisites.get("edrpou") or client.edrpou or "")
        if not ipn_digits.isdigit() or not (10 <= len(ipn_digits) <= 12) or ipn_digits == edrpou:
            ipn_digits = ""
        buyer = {
            "edrpou": requisites.get("edrpou") or client.edrpou,
            "company_name": requisites.get("company_name") or client.company_name,
            "ipn": ipn_digits or None,
            "company_address": requisites.get("company_address") or requisites.get("address"),
            "phone": client.phone,
            "email": client.email,
            "bank_account": requisites.get("bank_account"),
            "bank_name": requisites.get("bank_name"),
        }
        req_cp = str(requisites.get("onec_counterparty_id") or "").strip()
        client_cp = str(client.onec_counterparty_id or "").strip()
        req_edrpou = str(buyer.get("edrpou") or "").strip()
        client_edrpou = str(client.edrpou or "").strip()
        if req_cp:
            existing_cp = req_cp
        elif client_cp and client_edrpou and client_edrpou == req_edrpou:
            existing_cp = client_cp
        else:
            existing_cp = ""
        if existing_cp:
            counterparty_result = {
                "success": True,
                "counterparty_id": existing_cp,
                "created": False,
            }
        else:
            try:
                counterparty_result = await self.onec.create_counterparty({
                    "edrpou": buyer["edrpou"],
                    "name": buyer["company_name"],
                    "ipn": buyer.get("ipn"),
                    "company_address": buyer.get("company_address"),
                    "phone": client.phone,
                    "email": client.email,
                    "bank_account": requisites.get("bank_account"),
                    "bank_name": requisites.get("bank_name"),
                })
            except Exception:
                counterparty_result = {"success": False, "counterparty_id": None, "created": False}

        items = requisites.get("items") or [
            {
                "product": order.product_type,
                "qty": order.quantity,
                "amount": order.total_amount,
                "unit_price": order.unit_price,
            }
        ]
        from app.services.invoice_pdf import items_total

        summed = items_total(items)
        try:
            stored_total = float(order.total_amount or 0)
        except (TypeError, ValueError):
            stored_total = 0.0
        if summed > 0 and stored_total <= 0:
            order.total_amount = summed
            if len(items) == 1 and items[0].get("unit_price") is not None:
                try:
                    order.unit_price = float(items[0]["unit_price"])
                except (TypeError, ValueError):
                    pass
        payable = float(order.total_amount or 0) or summed
        invoice_result = {
            "success": True,
            "invoice_number": f"INV-{order.id}",
            "invoice_id": None,
            "mock": True,
        }
        if counterparty_result.get("counterparty_id"):
            try:
                invoice_result = await self.onec.create_invoice(
                    counterparty_result["counterparty_id"],
                    {
                        "order_id": order.id,
                        "total_amount": payable,
                        "description": f"Замовлення Silicon #{order.id} — {order.product_type}",
                        "comment": (
                            f"Створено Telegram-ботом. Замовлення #{order.id}. "
                            f"{order.product_type or ''}"
                        ).strip(),
                        "buyer": buyer,
                        "items": items,
                    },
                )
            except Exception as exc:
                invoice_result = {
                    "success": False,
                    "invoice_number": None,
                    "invoice_id": None,
                    "mock": False,
                    "posted_to_1c": False,
                    "error": str(exc),
                }

        if not counterparty_result.get("counterparty_id") and not self.onec._mock():
            invoice_result = {
                "success": False,
                "invoice_number": None,
                "invoice_id": None,
                "mock": False,
                "posted_to_1c": False,
                "error": counterparty_result.get("error") or "Контрагента в 1С немає",
            }

        posted = bool(
            invoice_result.get("success")
            and invoice_result.get("invoice_id")
            and not invoice_result.get("mock")
        )
        local_number = f"INV-{order.id}"
        if posted:
            order.invoice_number = str(invoice_result.get("invoice_number") or local_number)
        elif not order.invoice_number:
            order.invoice_number = local_number
        if not posted and invoice_result.get("error") and self.onec.configured() and not self.onec._mock():
            note = f"1С не записала рахунок: {invoice_result.get('error')}"
            order.comment = note[:500]
        invoice_result["posted_to_1c"] = posted
        invoice_result["invoice_number"] = order.invoice_number or local_number
        if counterparty_result.get("counterparty_id"):
            client.onec_counterparty_id = counterparty_result["counterparty_id"]
        if buyer.get("edrpou"):
            client.edrpou = str(buyer["edrpou"])
        if buyer.get("company_name"):
            client.company_name = str(buyer["company_name"])

        from app.services.invoice_pdf import save_invoice_pdf

        pdf_path = None
        invoice_id = invoice_result.get("invoice_id")
        if invoice_id and not invoice_result.get("mock"):
            try:
                pdf_path = await self.onec.download_invoice_pdf(
                    str(invoice_id), order_id=order.id
                )
            except Exception:
                logger.exception("1C invoice pdf download failed")
                pdf_path = None
        if pdf_path is None:
            pdf_path = save_invoice_pdf(
                invoice_number=str(invoice_result["invoice_number"]),
                buyer=buyer,
                items=items,
                total_amount=payable,
                order_id=order.id,
            )
        await db.commit()

        return {
            "success": True,
            "invoice_number": invoice_result["invoice_number"],
            "invoice_id": invoice_result.get("invoice_id"),
            "counterparty_id": counterparty_result["counterparty_id"],
            "counterparty_created": counterparty_result.get("created", False),
            "invoice_pdf_path": str(pdf_path),
            "invoice_pdf_filename": pdf_path.name,
            "pdf_from_1c": pdf_path.name.endswith("_1c.pdf"),
            "posted_to_1c": posted,
        }

    async def _mark_payment_success(
        self, db: AsyncSession, payment: Payment, order: Order, *, notify: bool = True
    ) -> None:
        if payment.status == PaymentStatus.SUCCESS:
            return
        payment.status = PaymentStatus.SUCCESS
        order.payment_status = PaymentStatus.SUCCESS
        order.status = OrderStatus.PAID
        client_result = await db.execute(select(Client).where(Client.id == order.client_id))
        client = client_result.scalar_one_or_none()
        if client and client.onebox_deal_id:
            await self.crm.update_deal(client.onebox_deal_id, {"payment_status": "paid", "status": "paid"})
        if notify and client and str(client.chat_id).isdigit():
            try:
                await self.telegram.send_payment_confirmation(client.chat_id, payment.amount)
            except Exception:
                logger.exception("payment notify failed")

    async def refresh_payment_from_liqpay(
        self, db: AsyncSession, payment: Payment, *, notify: bool = True
    ) -> Payment:
        """Самі питаємо LiqPay, не чекаючи лише webhook."""
        if not payment or payment.status == PaymentStatus.SUCCESS:
            return payment
        order_result = await db.execute(select(Order).where(Order.id == payment.order_id))
        order = order_result.scalar_one_or_none()
        if not order:
            return payment
        info = self.liqpay.fetch_status(str(payment.liqpay_payment_id or order.liqpay_order_id or ""))
        if info.get("success") and self.liqpay.is_paid_status(info.get("status")):
            await self._mark_payment_success(db, payment, order, notify=notify)
            await db.commit()
            await db.refresh(payment)
        return payment

    async def watch_pending_payments(self, db: AsyncSession) -> int:
        """Поки не минув ліміт очікування — перевіряємо LiqPay самі."""
        settings = get_settings()
        window = int(getattr(settings, "liqpay_watch_seconds", 900) or 900)
        cutoff = datetime.utcnow() - timedelta(seconds=window)
        result = await db.execute(
            select(Payment).where(
                Payment.status == PaymentStatus.PENDING,
                Payment.created_at >= cutoff,
            )
        )
        marked = 0
        for payment in result.scalars().all():
            before = payment.status
            await self.refresh_payment_from_liqpay(db, payment)
            if payment.status == PaymentStatus.SUCCESS and before != PaymentStatus.SUCCESS:
                marked += 1
        return marked

    async def handle_liqpay_webhook(self, db: AsyncSession, data: str, signature: str) -> dict:
        log = WebhookLog(source="liqpay", payload=data, status="received")
        db.add(log)

        if not self.liqpay.verify_webhook(data, signature):
            log.status = "invalid_signature"
            await db.commit()
            return {"success": False, "error": "Invalid signature"}

        payload = self.liqpay.decode_webhook(data)
        liqpay_order_id = payload.get("order_id")
        status = payload.get("status")

        payment_result = await db.execute(
            select(Payment).where(Payment.liqpay_payment_id == liqpay_order_id)
        )
        payment = payment_result.scalar_one_or_none()
        if not payment:
            log.status = "payment_not_found"
            await db.commit()
            return {"success": False, "error": "Payment not found"}

        order_result = await db.execute(select(Order).where(Order.id == payment.order_id))
        order = order_result.scalar_one()

        if self.liqpay.is_paid_status(status):
            await self._mark_payment_success(db, payment, order, notify=True)
            log.status = "success"
        else:
            payment.status = PaymentStatus.FAILURE
            order.payment_status = PaymentStatus.FAILURE
            log.status = "failure"

        await db.commit()
        return {"success": True, "status": status}

    async def handle_order_completed(self, db: AsyncSession, deal_id: str) -> dict:
        """Обробка статусу «Виконано» з CRM — надсилання ТТН клієнту."""
        log = WebhookLog(source="onebox", payload=json.dumps({"deal_id": deal_id, "status": "completed"}), status="received")
        db.add(log)

        client_result = await db.execute(select(Client).where(Client.onebox_deal_id == deal_id))
        client = client_result.scalar_one_or_none()
        if not client:
            log.status = "client_not_found"
            await db.commit()
            return {"success": False, "error": "Client not found for deal"}

        order_result = await db.execute(
            select(Order).where(Order.client_id == client.id).order_by(Order.id.desc())
        )
        order = order_result.scalars().first()
        if not order:
            log.status = "order_not_found"
            await db.commit()
            return {"success": False, "error": "Order not found"}

        if not order.ttn_number:
            ttn_result = await self.create_ttn(db, order.id)
            if not ttn_result.get("success"):
                log.status = "ttn_failed"
                await db.commit()
                return ttn_result
            await db.refresh(order)

        order.status = OrderStatus.COMPLETED
        try:
            if str(client.chat_id).isdigit():
                await self.telegram.send_ttn_notification(
                    client.chat_id, order.ttn_number, order.ttn_url or ""
                )
        except Exception:
            pass
        log.status = "notified"
        await db.commit()
        return {"success": True, "ttn_number": order.ttn_number, "chat_id": client.chat_id}
