"""Збереження історії чатів і заявок менеджеру."""
from __future__ import annotations

import json

from sqlalchemy import delete, desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from datetime import datetime, timezone

from app.database.models import (
    AdminUser,
    ChatMessage,
    Client,
    ConversationState,
    DeliveryInfo,
    InvoicePaymentNotice,
    ManagerTicket,
    Order,
    Payment,
)
from app.config import get_settings
from app.services.telegram_bot import TelegramBot
from app.services.timefmt import format_kyiv


def telegram_display_name(client: Client | None) -> str:
    """Ім'я лише з Telegram (не CRM/замовлення)."""
    if not client:
        return ""
    parts = [client.tg_first_name or "", client.tg_last_name or ""]
    name = " ".join(p for p in parts if p).strip()
    if name:
        return name
    if client.tg_username:
        return f"@{client.tg_username}"
    return ""


def order_display_name(client: Client | None) -> str:
    """Ім'я з замовлення / CRM."""
    if not client:
        return ""
    parts = [client.first_name or "", client.last_name or ""]
    return " ".join(p for p in parts if p).strip()


async def save_message(
    db: AsyncSession,
    chat_id: str,
    sender: str,
    content: str,
    content_type: str = "text",
) -> ChatMessage:
    msg = ChatMessage(
        chat_id=str(chat_id),
        sender=sender,
        content=content,
        content_type=content_type,
    )
    db.add(msg)
    await db.commit()
    await db.refresh(msg)
    return msg


async def get_or_create_state(db: AsyncSession, chat_id: str) -> ConversationState:
    result = await db.execute(
        select(ConversationState).where(ConversationState.chat_id == str(chat_id))
    )
    state = result.scalar_one_or_none()
    if state:
        return state
    state = ConversationState(chat_id=str(chat_id), step="idle", data_json="{}", human_mode=False)
    db.add(state)
    await db.flush()
    return state


async def is_human_mode(db: AsyncSession, chat_id: str) -> bool:
    state = await get_or_create_state(db, chat_id)
    return bool(state.human_mode)


async def set_human_mode(db: AsyncSession, chat_id: str, enabled: bool) -> None:
    state = await get_or_create_state(db, chat_id)
    state.human_mode = enabled
    if enabled:
        state.step = "human_mode"
    elif state.step == "human_mode":
        state.step = "idle"
    await db.commit()


async def build_ticket_question(db: AsyncSession, chat_id: str, trigger: str) -> str:
    """Контекст для заявки: тригер + останні повідомлення + відомі поля замовлення."""
    trigger_clean = (trigger or "").strip() or "Клієнт просить менеджера"
    lines: list[str] = [f"Останнє повідомлення клієнта: {trigger_clean}", ""]

    # відомі дані зі сценарію
    state = await get_or_create_state(db, chat_id)
    try:
        data = json.loads(state.data_json or "{}")
    except json.JSONDecodeError:
        data = {}
    known = []
    for label, key in (
        ("Товар", "product_type"),
        ("Кількість", "quantity"),
        ("Сума", "total_amount"),
        ("Прізвище", "last_name"),
        ("Ім'я", "first_name"),
        ("Телефон", "phone"),
        ("Місто", "city"),
    ):
        if data.get(key):
            known.append(f"{label}: {data[key]}")
    if known:
        lines.append("Відомі дані зі сценарію:")
        lines.extend(known)
        lines.append("")

    msgs = await list_messages(db, chat_id, after_id=0, limit=200)
    recent = msgs[-16:]
    if recent:
        lines.append("Контекст переписки:")
        for m in recent:
            if m.sender == "user":
                who = "Клієнт"
            elif m.sender == "admin":
                who = "Менеджер"
            else:
                who = "Бот"
            content = (m.content or "").strip().replace("\n", " ")
            if not content:
                continue
            lines.append(f"• {who}: {content[:350]}")

    text = "\n".join(lines).strip()
    return text[:6000] if text else trigger_clean


async def create_manager_ticket(
    db: AsyncSession,
    chat_id: str,
    question: str,
    *,
    mute_bot: bool = False,
) -> ManagerTicket:
    """Створює (або оновлює відкриту) заявку менеджеру.

    mute_bot=True — клієнт явно просив людину / менеджер перехопив чат:
    AI мовчить (human_mode). За замовчуванням лише тікет + нотифікація,
    бот далі відповідає на нові питання (м'який escalate з [ESCALATE]).
    """
    client_result = await db.execute(select(Client).where(Client.chat_id == str(chat_id)))
    client = client_result.scalar_one_or_none()

    # не плодити дублікати — оновлюємо існуючу відкриту заявку
    existing = (
        await db.execute(
            select(ManagerTicket)
            .where(
                ManagerTicket.chat_id == str(chat_id),
                ManagerTicket.status.in_(["open", "in_progress"]),
            )
            .order_by(desc(ManagerTicket.id))
            .limit(1)
        )
    ).scalar_one_or_none()

    q = await build_ticket_question(db, chat_id, question)
    if existing:
        existing.question = q
        existing.status = "open"
        existing.is_read = False
        if client and not existing.client_id:
            existing.client_id = client.id
        ticket = existing
    else:
        ticket = ManagerTicket(
            chat_id=str(chat_id),
            client_id=client.id if client else None,
            question=q,
            status="open",
            is_read=False,
        )
        db.add(ticket)

    await db.flush()

    state = await get_or_create_state(db, chat_id)
    if mute_bot:
        state.human_mode = True
        state.step = "human_mode"
    await db.commit()
    await db.refresh(ticket)

    # Telegram notify managers (best-effort)
    try:
        settings = get_settings()
        notify_id = settings.admin_notify_chat_id
        if notify_id:
            name = telegram_display_name(client) or "—"
            phone = client.phone if client else ""
            base = settings.admin_public_url.rstrip("/")
            short = (question or "").strip()[:200] or "запит"
            text = (
                "🔔 Новий запит менеджеру (Silicon / Олег)\n\n"
                f"Telegram: {name}\n"
                f"Chat ID: {chat_id}\n"
                f"Телефон: {phone or '—'}\n"
                f"Тригер: {short}\n\n"
                f"Відкрити чат: {base}/admin/live-chats?chat_id={chat_id}"
            )
            await TelegramBot().send_message(notify_id, text)
    except Exception:
        logger = __import__("logging").getLogger(__name__)
        logger.exception("manager notify failed")

    return ticket


async def list_messages(
    db: AsyncSession, chat_id: str, after_id: int = 0, limit: int = 200
) -> list[ChatMessage]:
    q = (
        select(ChatMessage)
        .where(ChatMessage.chat_id == str(chat_id), ChatMessage.id > after_id)
        .order_by(ChatMessage.id)
        .limit(limit)
    )
    result = await db.execute(q)
    return list(result.scalars().all())


async def get_stats(db: AsyncSession) -> dict:
    clients = (await db.execute(select(func.count(Client.id)))).scalar() or 0
    # Unique chat participants from messages
    chats = (
        await db.execute(select(func.count(func.distinct(ChatMessage.chat_id))))
    ).scalar() or 0
    messages = (await db.execute(select(func.count(ChatMessage.id)))).scalar() or 0
    open_tickets = (
        await db.execute(
            select(func.count(ManagerTicket.id)).where(ManagerTicket.status == "open")
        )
    ).scalar() or 0
    unread_tickets = (
        await db.execute(
            select(func.count(ManagerTicket.id)).where(
                ManagerTicket.status == "open", ManagerTicket.is_read == False  # noqa: E712
            )
        )
    ).scalar() or 0
    human_chats = (
        await db.execute(
            select(func.count(ConversationState.id)).where(
                ConversationState.human_mode == True  # noqa: E712
            )
        )
    ).scalar() or 0
    orders = (await db.execute(select(func.count(Order.id)))).scalar() or 0
    invoice_notices = (
        await db.execute(
            select(func.count(InvoicePaymentNotice.id)).where(
                InvoicePaymentNotice.status == "new"
            )
        )
    ).scalar() or 0
    closed_tickets = (
        await db.execute(
            select(func.count(ManagerTicket.id)).where(ManagerTicket.status == "closed")
        )
    ).scalar() or 0
    latest_unread = (
        await db.execute(
            select(ManagerTicket)
            .where(
                ManagerTicket.status.in_(["open", "in_progress"]),
                ManagerTicket.is_read == False,  # noqa: E712
            )
            .order_by(desc(ManagerTicket.id))
            .limit(1)
        )
    ).scalar_one_or_none()
    latest_payload = None
    if latest_unread:
        latest_payload = {
            "id": latest_unread.id,
            "chat_id": latest_unread.chat_id,
            "question": (latest_unread.question or "")[:240],
        }

    # статистика по менеджерах: скільки закрили
    managers = list(
        (
            await db.execute(
                select(AdminUser).where(AdminUser.is_active == True)  # noqa: E712
            )
        ).scalars().all()
    )
    manager_stats = []
    for m in managers:
        closed_by = (
            await db.execute(
                select(func.count(ManagerTicket.id)).where(
                    ManagerTicket.closed_by_id == m.id,
                    ManagerTicket.status == "closed",
                )
            )
        ).scalar() or 0
        claimed = (
            await db.execute(
                select(func.count(ManagerTicket.id)).where(
                    ManagerTicket.assigned_to_id == m.id,
                    ManagerTicket.status.in_(["open", "in_progress"]),
                )
            )
        ).scalar() or 0
        manager_stats.append({
            "id": m.id,
            "name": m.full_name or m.email,
            "role": m.role,
            "closed": closed_by,
            "active": claimed,
        })
    manager_stats.sort(key=lambda x: (-x["closed"], x["name"]))

    return {
        "clients": clients,
        "bot_users": max(clients, chats),
        "messages": messages,
        "open_tickets": open_tickets,
        "unread_tickets": unread_tickets,
        "closed_tickets": closed_tickets,
        "human_mode_chats": human_chats,
        "orders": orders,
        "invoice_notices": invoice_notices,
        "latest_unread_ticket": latest_payload,
        "latest_ticket_id": latest_unread.id if latest_unread else 0,
        "manager_stats": manager_stats,
    }


async def list_chat_previews(db: AsyncSession) -> list[dict]:
    # Latest message per chat
    subq = (
        select(
            ChatMessage.chat_id,
            func.max(ChatMessage.id).label("max_id"),
        )
        .group_by(ChatMessage.chat_id)
        .subquery()
    )
    result = await db.execute(
        select(ChatMessage)
        .join(subq, ChatMessage.id == subq.c.max_id)
        .order_by(desc(ChatMessage.id))
        .limit(200)
    )
    messages = list(result.scalars().all())

    clients_result = await db.execute(select(Client))
    clients = {c.chat_id: c for c in clients_result.scalars().all()}

    tickets_result = await db.execute(
        select(ManagerTicket)
        .where(ManagerTicket.status.in_(["open", "in_progress"]))
        .order_by(desc(ManagerTicket.id))
    )
    tickets_by_chat: dict[str, ManagerTicket] = {}
    for t in tickets_result.scalars().all():
        if t.chat_id not in tickets_by_chat:
            tickets_by_chat[t.chat_id] = t

    states_result = await db.execute(select(ConversationState))
    states = {s.chat_id: s for s in states_result.scalars().all()}

    admins = {
        a.id: a
        for a in (
            await db.execute(select(AdminUser))
        ).scalars().all()
    }

    previews = []
    for m in messages:
        c = clients.get(m.chat_id)
        t = tickets_by_chat.get(m.chat_id)
        st = states.get(m.chat_id)
        display = telegram_display_name(c) or f"Chat {m.chat_id}"
        assignee = None
        if t and t.assigned_to_id and t.assigned_to_id in admins:
            a = admins[t.assigned_to_id]
            assignee = {"id": a.id, "name": a.full_name or a.email}
        previews.append({
            "chat_id": m.chat_id,
            "full_name": display,
            "tg_name": telegram_display_name(c),
            "order_name": order_display_name(c),
            "tg_username": c.tg_username if c else None,
            "tg_photo_url": c.tg_photo_url if c else None,
            "phone": c.phone if c else None,
            "email": c.email if c else None,
            "city": c.city if c else None,
            "last_snippet": (m.content or "")[:140],
            "last_sender": m.sender,
            "last_at": format_kyiv(m.created_at) if m.created_at else None,
            "human_mode": bool(st.human_mode) if st else False,
            "open_ticket": bool(t),
            "ticket_id": t.id if t else None,
            "ticket_question": t.question if t else None,
            "ticket_unread": (not t.is_read) if t else False,
            "ticket_status": t.status if t else None,
            "assigned_to": assignee,
            "client_id": c.id if c else None,
        })
    return previews


async def list_bot_people(db: AsyncSession) -> list[dict]:
    """Усі люди з бота: клієнти CRM + чати, яких ще немає в clients."""
    clients = list((await db.execute(select(Client).order_by(desc(Client.id)))).scalars().all())
    by_chat = {c.chat_id: c for c in clients}

    last_sub = (
        select(
            ChatMessage.chat_id,
            func.max(ChatMessage.id).label("max_id"),
        )
        .group_by(ChatMessage.chat_id)
        .subquery()
    )
    last_rows = list(
        (
            await db.execute(
                select(ChatMessage).join(last_sub, ChatMessage.id == last_sub.c.max_id)
            )
        ).scalars().all()
    )
    last_by_chat = {m.chat_id: m for m in last_rows}

    msg_counts = {
        row[0]: int(row[1] or 0)
        for row in (
            await db.execute(
                select(ChatMessage.chat_id, func.count(ChatMessage.id)).group_by(ChatMessage.chat_id)
            )
        ).all()
    }
    order_counts = {
        row[0]: int(row[1] or 0)
        for row in (
            await db.execute(select(Order.client_id, func.count(Order.id)).group_by(Order.client_id))
        ).all()
        if row[0] is not None
    }

    extra_ids = [cid for cid in last_by_chat if cid not in by_chat]
    people: list[dict] = []

    def row_for(chat_id: str, client: Client | None) -> dict:
        last = last_by_chat.get(chat_id)
        return {
            "id": client.id if client else None,
            "chat_id": chat_id,
            "channel": (client.channel if client else "telegram") or "telegram",
            "tg_name": telegram_display_name(client),
            "order_name": order_display_name(client),
            "tg_username": client.tg_username if client else None,
            "tg_photo_url": client.tg_photo_url if client else None,
            "phone": client.phone if client else None,
            "email": client.email if client else None,
            "city": client.city if client else None,
            "company_name": client.company_name if client else None,
            "edrpou": client.edrpou if client else None,
            "messages": msg_counts.get(chat_id, 0),
            "orders": order_counts.get(client.id, 0) if client else 0,
            "created_at": client.created_at if client else (last.created_at if last else None),
            "last_at": last.created_at if last else None,
            "last_snippet": (last.content or "")[:180] if last else "",
        }

    for c in clients:
        people.append(row_for(c.chat_id, c))
    for cid in extra_ids:
        people.append(row_for(cid, None))
    def _sort_key(p: dict):
        v = p.get("last_at") or p.get("created_at")
        if v is None:
            return 0.0
        if hasattr(v, "timestamp"):
            try:
                return float(v.timestamp())
            except (OSError, OverflowError, TypeError, ValueError):
                return 0.0
        return 0.0

    people.sort(key=_sort_key, reverse=True)
    return people


async def list_open_tickets(db: AsyncSession) -> list[ManagerTicket]:
    result = await db.execute(
        select(ManagerTicket)
        .where(ManagerTicket.status.in_(["open", "in_progress"]))
        .order_by(desc(ManagerTicket.id))
    )
    return list(result.scalars().all())


async def claim_ticket(
    db: AsyncSession, ticket_id: int, admin: AdminUser
) -> tuple[ManagerTicket | None, str | None]:
    """Взяти заявку. Помилка, якщо вже взяв інший менеджер."""
    ticket = (
        await db.execute(select(ManagerTicket).where(ManagerTicket.id == ticket_id))
    ).scalar_one_or_none()
    if not ticket:
        return None, "Заявку не знайдено"
    if ticket.status == "closed":
        return ticket, "Заявка вже закрита"
    if ticket.assigned_to_id and ticket.assigned_to_id != admin.id:
        other = await db.get(AdminUser, ticket.assigned_to_id)
        name = (other.full_name or other.email) if other else "інший менеджер"
        return ticket, f"Заявку вже взяв {name}"
    ticket.assigned_to_id = admin.id
    ticket.claimed_at = datetime.now(timezone.utc)
    ticket.status = "in_progress"
    ticket.is_read = True
    await set_human_mode(db, ticket.chat_id, True)
    await db.commit()
    await db.refresh(ticket)
    return ticket, None


async def close_ticket(
    db: AsyncSession, ticket_id: int, admin: AdminUser
) -> tuple[ManagerTicket | None, str | None]:
    ticket = (
        await db.execute(select(ManagerTicket).where(ManagerTicket.id == ticket_id))
    ).scalar_one_or_none()
    if not ticket:
        return None, "Заявку не знайдено"
    if ticket.assigned_to_id and ticket.assigned_to_id != admin.id and admin.role != "owner":
        return ticket, "Закрити може лише менеджер, який взяв заявку, або власник"
    ticket.status = "closed"
    ticket.is_read = True
    ticket.closed_by_id = admin.id
    ticket.closed_at = datetime.now(timezone.utc)
    # якщо немає інших відкритих по чату — вимкнути human mode
    other = (
        await db.execute(
            select(ManagerTicket).where(
                ManagerTicket.chat_id == ticket.chat_id,
                ManagerTicket.status.in_(["open", "in_progress"]),
                ManagerTicket.id != ticket.id,
            )
        )
    ).scalars().first()
    if not other:
        await set_human_mode(db, ticket.chat_id, False)
    await db.commit()
    await db.refresh(ticket)
    return ticket, None


async def ensure_can_access_chat(
    db: AsyncSession, chat_id: str, admin: AdminUser
) -> str | None:
    """None якщо можна; інакше текст помилки (чужа взята заявка)."""
    if admin.role == "owner":
        return None
    tickets = list(
        (
            await db.execute(
                select(ManagerTicket).where(
                    ManagerTicket.chat_id == str(chat_id),
                    ManagerTicket.status.in_(["open", "in_progress"]),
                )
            )
        ).scalars().all()
    )
    for t in tickets:
        if t.assigned_to_id and t.assigned_to_id != admin.id:
            other = await db.get(AdminUser, t.assigned_to_id)
            name = (other.full_name or other.email) if other else "інший менеджер"
            return f"Чат зайнятий менеджером {name}"
    return None


async def purge_chat(
    db: AsyncSession,
    chat_id: str,
    *,
    include_contact: bool = False,
) -> dict:
    """Прибрати чат (історія + стан + заявки). Якщо include_contact — і контакт з бази бота."""
    cid = str(chat_id)
    n_msg = (await db.execute(delete(ChatMessage).where(ChatMessage.chat_id == cid))).rowcount or 0
    n_state = (
        await db.execute(delete(ConversationState).where(ConversationState.chat_id == cid))
    ).rowcount or 0
    n_tickets = (
        await db.execute(delete(ManagerTicket).where(ManagerTicket.chat_id == cid))
    ).rowcount or 0

    n_payments = 0
    n_orders = 0
    n_delivery = 0
    n_client = 0
    if include_contact:
        client = (
            await db.execute(select(Client).where(Client.chat_id == cid))
        ).scalar_one_or_none()
        if client:
            orders = list(
                (
                    await db.execute(select(Order).where(Order.client_id == client.id))
                ).scalars().all()
            )
            order_ids = [o.id for o in orders]
            if order_ids:
                n_payments = (
                    await db.execute(delete(Payment).where(Payment.order_id.in_(order_ids)))
                ).rowcount or 0
            n_orders = (
                await db.execute(delete(Order).where(Order.client_id == client.id))
            ).rowcount or 0
            n_delivery = (
                await db.execute(delete(DeliveryInfo).where(DeliveryInfo.client_id == client.id))
            ).rowcount or 0
            n_client = (
                await db.execute(delete(Client).where(Client.id == client.id))
            ).rowcount or 0

    await db.commit()
    return {
        "chat_id": cid,
        "messages": int(n_msg),
        "states": int(n_state),
        "tickets": int(n_tickets),
        "payments": int(n_payments),
        "orders": int(n_orders),
        "delivery": int(n_delivery),
        "clients": int(n_client),
        "include_contact": include_contact,
    }


async def ticket_payload(db: AsyncSession, ticket: ManagerTicket) -> dict:
    client = None
    if ticket.client_id:
        client = await db.get(Client, ticket.client_id)
    if not client:
        client = (
            await db.execute(select(Client).where(Client.chat_id == ticket.chat_id))
        ).scalar_one_or_none()
    assignee = None
    if ticket.assigned_to_id:
        a = await db.get(AdminUser, ticket.assigned_to_id)
        if a:
            assignee = {"id": a.id, "name": a.full_name or a.email}
    closer = None
    if ticket.closed_by_id:
        a = await db.get(AdminUser, ticket.closed_by_id)
        if a:
            closer = {"id": a.id, "name": a.full_name or a.email}
    return {
        "id": ticket.id,
        "chat_id": ticket.chat_id,
        "question": ticket.question,
        "status": ticket.status,
        "is_read": ticket.is_read,
        "created_at": format_kyiv(ticket.created_at) if ticket.created_at else None,
        "claimed_at": format_kyiv(ticket.claimed_at) if ticket.claimed_at else None,
        "closed_at": format_kyiv(ticket.closed_at) if ticket.closed_at else None,
        "tg_name": telegram_display_name(client),
        "order_name": order_display_name(client),
        "tg_username": client.tg_username if client else None,
        "phone": client.phone if client else None,
        "assigned_to": assignee,
        "closed_by": closer,
    }


async def group_tickets_by_person(db: AsyncSession, tickets: list[ManagerTicket]) -> list[dict]:
    """Групує заявки однієї людини (chat_id / телефон) в кучки."""
    clients = {
        c.chat_id: c
        for c in (await db.execute(select(Client))).scalars().all()
    }
    groups: dict[str, dict] = {}
    order: list[str] = []
    for t in tickets:
        c = clients.get(t.chat_id)
        key = t.chat_id
        if c and c.phone:
            key = f"phone:{c.phone}"
        if key not in groups:
            groups[key] = {
                "key": key,
                "chat_id": t.chat_id,
                "tg_name": telegram_display_name(c),
                "phone": c.phone if c else None,
                "tickets": [],
            }
            order.append(key)
        groups[key]["tickets"].append(await ticket_payload(db, t))
    return [groups[k] for k in order]


def _parse_order_id(raw) -> int | None:
    try:
        if raw in (None, ""):
            return None
        return int(raw)
    except (TypeError, ValueError):
        return None


async def upsert_invoice_payment_notice(
    db: AsyncSession,
    *,
    chat_id: str,
    order_id=None,
    invoice_number: str | None = None,
    amount=None,
    company_name: str | None = None,
    message: str | None = None,
    screenshot_path: str | None = None,
) -> InvoicePaymentNotice:
    result = await db.execute(
        select(InvoicePaymentNotice)
        .where(
            InvoicePaymentNotice.chat_id == str(chat_id),
            InvoicePaymentNotice.status == "new",
        )
        .order_by(desc(InvoicePaymentNotice.id))
        .limit(1)
    )
    notice = result.scalar_one_or_none()
    amt = None
    try:
        if amount not in (None, ""):
            amt = float(amount)
    except (TypeError, ValueError):
        amt = None
    if notice:
        if invoice_number:
            notice.invoice_number = invoice_number
        if amt is not None:
            notice.amount = amt
        if company_name:
            notice.company_name = company_name
        if message:
            notice.message = message
        if screenshot_path:
            notice.screenshot_path = screenshot_path
        oid = _parse_order_id(order_id)
        if oid:
            notice.order_id = oid
    else:
        notice = InvoicePaymentNotice(
            chat_id=str(chat_id),
            order_id=_parse_order_id(order_id),
            invoice_number=invoice_number,
            amount=amt,
            company_name=company_name,
            message=message,
            screenshot_path=screenshot_path,
            status="new",
        )
        db.add(notice)
    await db.commit()
    await db.refresh(notice)
    return notice


async def list_invoice_payment_notices(
    db: AsyncSession, *, include_done: bool = True
) -> list[dict]:
    q = select(InvoicePaymentNotice).order_by(desc(InvoicePaymentNotice.id)).limit(150)
    if not include_done:
        q = q.where(InvoicePaymentNotice.status == "new")
    rows = list((await db.execute(q)).scalars().all())
    chat_ids = {r.chat_id for r in rows}
    clients: dict[str, Client] = {}
    if chat_ids:
        cr = await db.execute(select(Client).where(Client.chat_id.in_(chat_ids)))
        clients = {c.chat_id: c for c in cr.scalars().all()}
    out: list[dict] = []
    for r in rows:
        c = clients.get(r.chat_id)
        out.append(
            {
                "id": r.id,
                "chat_id": r.chat_id,
                "order_id": r.order_id,
                "invoice_number": r.invoice_number,
                "amount": r.amount,
                "company_name": r.company_name,
                "message": r.message,
                "screenshot_path": r.screenshot_path,
                "status": r.status,
                "created_at": r.created_at,
                "tg_name": telegram_display_name(c),
                "phone": c.phone if c else None,
            }
        )
    return out


async def mark_invoice_notice_done(
    db: AsyncSession, notice_id: int, user: AdminUser
) -> InvoicePaymentNotice | None:
    notice = (
        await db.execute(
            select(InvoicePaymentNotice).where(InvoicePaymentNotice.id == notice_id)
        )
    ).scalar_one_or_none()
    if not notice:
        return None
    notice.status = "done"
    notice.done_at = datetime.now(timezone.utc)
    notice.done_by_id = user.id
    await db.commit()
    return notice
