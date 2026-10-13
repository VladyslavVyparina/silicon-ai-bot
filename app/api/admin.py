"""Адмін-панель Silicon: статистика, чати, команда, кабінет."""
from __future__ import annotations

import re
import secrets
from pathlib import Path

from datetime import datetime, timezone
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import AdminUser, Client, ManagerTicket, Order
from app.database.session import get_db
from app.services import chat_history
from app.services.admin_auth import (
    authenticate_admin,
    create_admin_token,
    decode_admin_token,
    ensure_owner_exists,
    generate_password,
    get_admin_by_id,
    hash_password,
    list_admins,
    verify_admin_token,
    verify_password,
)
from app.services.email_service import send_manager_invite_email
from app.services.telegram_bot import TelegramBot
from app.services.timefmt import format_kyiv

ROOT = Path(__file__).resolve().parent.parent.parent
TEMPLATES_DIR = ROOT / "templates"
UPLOADS_DIR = ROOT / "uploads" / "avatars"
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.filters["kyiv"] = format_kyiv
router = APIRouter(prefix="/admin", tags=["Admin"])

# Одноразовий показ пароля після створення менеджера (без SMTP)
_invite_flash: dict[int, dict] = {}


def _put_invite_flash(owner_id: int, data: dict) -> None:
    _invite_flash[owner_id] = data


def _pop_invite_flash(owner_id: int) -> dict | None:
    return _invite_flash.pop(owner_id, None)


def _require_token(token: str | None) -> dict:
    data = decode_admin_token(token)
    if not data:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return data


async def _current_user(db: AsyncSession, token: str | None) -> AdminUser:
    data = _require_token(token)
    user = await get_admin_by_id(db, int(data["admin_id"]))
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return user


def _avatar_url(user: AdminUser | None) -> str | None:
    if not user or not user.avatar_path:
        return None
    name = Path(user.avatar_path).name
    local = UPLOADS_DIR / name
    if not local.exists():
        return None
    stamp = ""
    if user.updated_at:
        try:
            stamp = f"?v={int(user.updated_at.timestamp())}"
        except (OSError, OverflowError, ValueError):
            stamp = f"?v={name}"
    return f"/uploads/avatars/{name}{stamp}"


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def admin_home(request: Request, token: str = "", db: AsyncSession = Depends(get_db)):
    await ensure_owner_exists(db)
    if not verify_admin_token(token):
        return templates.TemplateResponse(request, "admin_login.html", {"error": None})
    user = await _current_user(db, token)
    stats = await chat_history.get_stats(db)
    tickets = await chat_history.list_open_tickets(db)
    return templates.TemplateResponse(
        request,
        "admin_dashboard.html",
        {
            "token": token,
            "stats": stats,
            "tickets": tickets,
            "active": "dashboard",
            "admin": user,
            "avatar_url": _avatar_url(user),
        },
    )


@router.post("/login")
async def admin_login(
    identifier: str = Form(...),
    password: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    await ensure_owner_exists(db)
    user = await authenticate_admin(db, identifier, password)
    if not user:
        return RedirectResponse(url="/admin/login?error=1", status_code=303)
    token = create_admin_token(user)
    return RedirectResponse(url=f"/admin?token={token}", status_code=303)


@router.get("/login", response_class=HTMLResponse)
async def admin_login_get(request: Request, error: str | None = None, db: AsyncSession = Depends(get_db)):
    await ensure_owner_exists(db)
    return templates.TemplateResponse(
        request,
        "admin_login.html",
        {"error": "Невірний email/логін або пароль" if error else None},
    )


@router.get("/live-chats", response_class=HTMLResponse)
async def live_chats_page(
    request: Request,
    token: str = "",
    chat_id: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    user = await _current_user(db, token)
    return templates.TemplateResponse(
        request,
        "admin_live_chats.html",
        {
            "token": token,
            "selected_chat_id": chat_id,
            "active": "chats",
            "admin": user,
            "avatar_url": _avatar_url(user),
        },
    )


@router.get("/live-chats/data")
async def live_chats_data(token: str = "", db: AsyncSession = Depends(get_db)):
    await _current_user(db, token)
    users = await chat_history.list_chat_previews(db)
    return {"users": users}


@router.get("/live-chats/{chat_id}/messages")
async def live_chat_messages(
    chat_id: str,
    token: str = "",
    after: int = 0,
    db: AsyncSession = Depends(get_db),
):
    await _current_user(db, token)
    msgs = await chat_history.list_messages(db, chat_id, after_id=after)
    human = await chat_history.is_human_mode(db, chat_id)
    return {
        "chat_id": chat_id,
        "human_mode": human,
        "messages": [
            {
                "id": m.id,
                "kind": m.sender if m.sender in ("user", "bot", "admin") else "user",
                "from_admin": m.sender == "admin",
                "content": m.content,
                "content_type": m.content_type,
                "created_at": format_kyiv(m.created_at),
            }
            for m in msgs
        ],
    }


@router.post("/live-chats/{chat_id}/send")
async def live_chat_send(
    chat_id: str,
    token: str = Form(...),
    message: str = Form(""),
    media_type: str = Form("text"),
    attachment: UploadFile | None = File(None),
    db: AsyncSession = Depends(get_db),
):
    user = await _current_user(db, token)
    lock = await chat_history.ensure_can_access_chat(db, chat_id, user)
    if lock:
        raise HTTPException(status_code=403, detail=lock)
    text = (message or "").strip()
    media_type = (media_type or "text").strip().lower()
    has_file = attachment is not None and bool(attachment.filename)

    if not text and not has_file:
        raise HTTPException(status_code=400, detail="Empty message")

    await chat_history.set_human_mode(db, chat_id, True)
    result = await db.execute(
        select(ManagerTicket).where(
            ManagerTicket.chat_id == str(chat_id),
            ManagerTicket.status.in_(["open", "in_progress"]),
        )
    )
    for t in result.scalars().all():
        t.is_read = True
        t.status = "in_progress"
        if not t.assigned_to_id:
            t.assigned_to_id = user.id
            from datetime import datetime, timezone
            t.claimed_at = datetime.now(timezone.utc)
    await db.commit()

    bot = TelegramBot()
    send_result: dict = {"success": True}
    content_type = "text"
    history_text = text

    if has_file and media_type in ("photo", "video", "document"):
        raw = await attachment.read()
        fname = attachment.filename or "file"
        if media_type == "photo":
            send_result = await bot.send_photo(chat_id, raw, fname, caption=text)
            content_type = "photo"
            history_text = text or f"[фото] {fname}"
        elif media_type == "video":
            send_result = await bot.send_video(chat_id, raw, fname, caption=text)
            content_type = "video"
            history_text = text or f"[відео] {fname}"
        else:
            send_result = await bot.send_document(chat_id, raw, fname, caption=text)
            content_type = "document"
            history_text = text or f"[файл] {fname}"
    else:
        if not text:
            raise HTTPException(status_code=400, detail="Empty message")
        send_result = await bot.send_message(chat_id, text)

    await chat_history.save_message(db, chat_id, "admin", history_text, content_type=content_type)
    return JSONResponse({"success": True, "telegram": send_result})


@router.post("/live-chats/{chat_id}/takeover")
async def live_chat_takeover(chat_id: str, token: str = Form(...), db: AsyncSession = Depends(get_db)):
    user = await _current_user(db, token)
    lock = await chat_history.ensure_can_access_chat(db, chat_id, user)
    if lock:
        raise HTTPException(status_code=403, detail=lock)
    await chat_history.set_human_mode(db, chat_id, True)
    from datetime import datetime, timezone

    result = await db.execute(
        select(ManagerTicket).where(
            ManagerTicket.chat_id == str(chat_id),
            ManagerTicket.status.in_(["open", "in_progress"]),
        )
    )
    for t in result.scalars().all():
        t.is_read = True
        t.status = "in_progress"
        if not t.assigned_to_id:
            t.assigned_to_id = user.id
            t.claimed_at = datetime.now(timezone.utc)
    await db.commit()

    from app.services.conversation_flow import manager_greeting_text

    greeting = manager_greeting_text(user.full_name)
    bot = TelegramBot()
    send_result = await bot.send_message(chat_id, greeting)
    await chat_history.save_message(db, chat_id, "admin", greeting)
    return JSONResponse({
        "success": True,
        "human_mode": True,
        "greeting_sent": True,
        "telegram": send_result,
    })


@router.post("/live-chats/{chat_id}/release")
async def live_chat_release(chat_id: str, token: str = Form(...), db: AsyncSession = Depends(get_db)):
    user = await _current_user(db, token)
    lock = await chat_history.ensure_can_access_chat(db, chat_id, user)
    if lock:
        raise HTTPException(status_code=403, detail=lock)
    await chat_history.set_human_mode(db, chat_id, False)
    from datetime import datetime, timezone

    result = await db.execute(
        select(ManagerTicket).where(
            ManagerTicket.chat_id == str(chat_id),
            ManagerTicket.status.in_(["open", "in_progress"]),
        )
    )
    for t in result.scalars().all():
        t.status = "closed"
        t.is_read = True
        t.closed_by_id = user.id
        t.closed_at = datetime.now(timezone.utc)
    await db.commit()
    return JSONResponse({"success": True, "human_mode": False})


@router.post("/live-chats/{chat_id}/delete")
async def live_chat_delete(chat_id: str, token: str = Form(...), db: AsyncSession = Depends(get_db)):
    """Видалити чат (історію повідомлень) з адмінки. Контакт у базі лишається."""
    user = await _current_user(db, token)
    lock = await chat_history.ensure_can_access_chat(db, chat_id, user)
    if lock:
        raise HTTPException(status_code=403, detail=lock)
    result = await chat_history.purge_chat(db, chat_id, include_contact=False)
    return JSONResponse({"success": True, **result})


@router.post("/live-chats/{chat_id}/delete-contact")
async def live_chat_delete_contact(chat_id: str, token: str = Form(...), db: AsyncSession = Depends(get_db)):
    """Видалити контакт із бази бота разом із чатом, замовленнями і заявками."""
    user = await _current_user(db, token)
    lock = await chat_history.ensure_can_access_chat(db, chat_id, user)
    if lock:
        raise HTTPException(status_code=403, detail=lock)
    result = await chat_history.purge_chat(db, chat_id, include_contact=True)
    return JSONResponse({"success": True, **result})


@router.get("/tickets")
async def tickets_page(request: Request, token: str = "", db: AsyncSession = Depends(get_db)):
    user = await _current_user(db, token)
    tickets = await chat_history.list_open_tickets(db)
    all_result = await db.execute(
        select(ManagerTicket).order_by(ManagerTicket.id.desc()).limit(200)
    )
    all_tickets = list(all_result.scalars().all())
    open_groups = await chat_history.group_tickets_by_person(db, tickets)
    all_groups = await chat_history.group_tickets_by_person(db, all_tickets)
    stats = await chat_history.get_stats(db)
    return templates.TemplateResponse(
        request,
        "admin_tickets.html",
        {
            "token": token,
            "tickets": tickets,
            "all_tickets": all_tickets,
            "open_groups": open_groups,
            "all_groups": all_groups,
            "stats": stats,
            "active": "tickets",
            "admin": user,
            "avatar_url": _avatar_url(user),
            "flash_err": request.query_params.get("err"),
            "flash_msg": request.query_params.get("msg"),
        },
    )


@router.get("/invoice-payments", response_class=HTMLResponse)
async def invoice_payments_page(
    request: Request, token: str = "", db: AsyncSession = Depends(get_db)
):
    user = await _current_user(db, token)
    stats = await chat_history.get_stats(db)
    notices = await chat_history.list_invoice_payment_notices(db)
    return templates.TemplateResponse(
        request,
        "admin_invoice_payments.html",
        {
            "token": token,
            "stats": stats,
            "notices": notices,
            "active": "invoice_payments",
            "admin": user,
            "avatar_url": _avatar_url(user),
            "flash_err": request.query_params.get("err"),
            "flash_msg": request.query_params.get("msg"),
        },
    )


@router.post("/invoice-payments/{notice_id}/done")
async def invoice_payment_done(
    notice_id: int, token: str = Form(...), db: AsyncSession = Depends(get_db)
):
    user = await _current_user(db, token)
    notice = await chat_history.mark_invoice_notice_done(db, notice_id, user)
    if not notice:
        raise HTTPException(status_code=404)
    return RedirectResponse(
        url=f"/admin/invoice-payments?token={token}&msg={quote('Позначено як оброблено')}",
        status_code=303,
    )


def _filter_people(people: list[dict], query: str) -> list[dict]:
    q = (query or "").strip().lower()
    if not q:
        return people
    out = []
    for p in people:
        blob = " ".join(
            str(p.get(k) or "")
            for k in (
                "tg_name",
                "order_name",
                "tg_username",
                "phone",
                "email",
                "city",
                "company_name",
                "edrpou",
                "chat_id",
            )
        ).lower()
        if q in blob:
            out.append(p)
    return out


def _people_xlsx_bytes(people: list[dict], orders: list) -> bytes:
    from io import BytesIO

    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Клієнти"
    ws.append(
        [
            "Telegram ім'я",
            "Нік",
            "Ім'я з замовлення",
            "Телефон",
            "Email",
            "Місто",
            "Компанія",
            "ЄДРПОУ",
            "Chat ID",
            "Канал",
            "Повідомлень",
            "Замовлень",
            "Перший контакт",
            "Остання активність",
            "Останнє повідомлення",
        ]
    )
    for p in people:
        ws.append(
            [
                p.get("tg_name") or "",
                (f"@{p['tg_username']}" if p.get("tg_username") else ""),
                p.get("order_name") or "",
                p.get("phone") or "",
                p.get("email") or "",
                p.get("city") or "",
                p.get("company_name") or "",
                p.get("edrpou") or "",
                p.get("chat_id") or "",
                p.get("channel") or "",
                p.get("messages") or 0,
                p.get("orders") or 0,
                format_kyiv(p.get("created_at")),
                format_kyiv(p.get("last_at")),
                (p.get("last_snippet") or "").replace("\n", " "),
            ]
        )
    wo = wb.create_sheet("Замовлення")
    wo.append(
        [
            "ID",
            "Chat ID",
            "Товар",
            "Кількість",
            "Сума",
            "Статус",
            "Оплата",
            "ТТН",
            "Рахунок",
            "Створено",
        ]
    )
    for order in orders:
        client = getattr(order, "client", None)
        wo.append(
            [
                order.id,
                client.chat_id if client else "",
                order.product_type or "",
                order.quantity or "",
                order.total_amount or "",
                getattr(order.status, "value", order.status) or "",
                getattr(order.payment_status, "value", order.payment_status) or "",
                order.ttn_number or "",
                order.invoice_number or "",
                format_kyiv(order.created_at),
            ]
        )
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


@router.get("/clients", response_class=HTMLResponse)
async def clients_page(
    request: Request,
    token: str = "",
    q: str = "",
    db: AsyncSession = Depends(get_db),
):
    user = await _current_user(db, token)
    stats = await chat_history.get_stats(db)
    people = _filter_people(await chat_history.list_bot_people(db), q)
    return templates.TemplateResponse(
        request,
        "admin_clients.html",
        {
            "token": token,
            "stats": stats,
            "people": people,
            "query": q,
            "active": "clients",
            "admin": user,
            "avatar_url": _avatar_url(user),
        },
    )


@router.get("/clients/export.xlsx")
async def clients_export_xlsx(token: str = "", q: str = "", db: AsyncSession = Depends(get_db)):
    await _current_user(db, token)
    people = _filter_people(await chat_history.list_bot_people(db), q)
    orders = list(
        (
            await db.execute(
                select(Order)
                .options(selectinload(Order.client))
                .order_by(Order.id.desc())
                .limit(2000)
            )
        ).scalars().all()
    )
    if q:
        chat_ids = {p.get("chat_id") for p in people}
        orders = [o for o in orders if getattr(o.client, "chat_id", None) in chat_ids]
    payload = _people_xlsx_bytes(people, orders)
    filename = "silicon-baza.xlsx"
    return Response(
        content=payload,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/tickets/{ticket_id}/claim")
async def claim_ticket(ticket_id: int, token: str = Form(...), db: AsyncSession = Depends(get_db)):
    user = await _current_user(db, token)
    ticket, err = await chat_history.claim_ticket(db, ticket_id, user)
    if not ticket:
        raise HTTPException(status_code=404)
    if err:
        return RedirectResponse(
            url=f"/admin/tickets?token={token}&err={quote(err)}",
            status_code=303,
        )
    return RedirectResponse(
        url=f"/admin/live-chats?token={token}&chat_id={ticket.chat_id}",
        status_code=303,
    )


@router.post("/tickets/{ticket_id}/close")
async def close_ticket(ticket_id: int, token: str = Form(...), db: AsyncSession = Depends(get_db)):
    user = await _current_user(db, token)
    ticket, err = await chat_history.close_ticket(db, ticket_id, user)
    if not ticket:
        raise HTTPException(status_code=404)
    if err:
        return RedirectResponse(
            url=f"/admin/tickets?token={token}&err={quote(err)}",
            status_code=303,
        )
    return RedirectResponse(
        url=f"/admin/tickets?token={token}&msg={quote('Заявку закрито')}",
        status_code=303,
    )


@router.get("/tickets/{ticket_id}")
async def ticket_detail(ticket_id: int, request: Request, token: str = "", db: AsyncSession = Depends(get_db)):
    user = await _current_user(db, token)
    ticket = (
        await db.execute(select(ManagerTicket).where(ManagerTicket.id == ticket_id))
    ).scalar_one_or_none()
    if not ticket:
        raise HTTPException(status_code=404)
    lock = None
    if ticket.assigned_to_id and ticket.assigned_to_id != user.id and user.role != "owner":
        lock = await chat_history.ensure_can_access_chat(db, ticket.chat_id, user)
    payload = await chat_history.ticket_payload(db, ticket)
    messages = await chat_history.list_messages(db, ticket.chat_id, after_id=0, limit=500)
    # історія до закриття: якщо closed_at — обрізати після нього
    if ticket.closed_at:
        messages = [m for m in messages if not m.created_at or m.created_at <= ticket.closed_at]
    return templates.TemplateResponse(
        request,
        "admin_ticket_detail.html",
        {
            "token": token,
            "ticket": ticket,
            "payload": payload,
            "messages": messages,
            "lock": lock,
            "active": "tickets",
            "admin": user,
            "avatar_url": _avatar_url(user),
            "flash_msg": request.query_params.get("msg"),
            "flash_err": request.query_params.get("err"),
        },
    )


@router.post("/tickets/{ticket_id}/read")
async def mark_ticket_read(ticket_id: int, token: str = Form(...), db: AsyncSession = Depends(get_db)):
    user = await _current_user(db, token)
    ticket, err = await chat_history.claim_ticket(db, ticket_id, user)
    if not ticket:
        raise HTTPException(status_code=404)
    if err:
        return RedirectResponse(
            url=f"/admin/tickets?token={token}&err={quote(err)}",
            status_code=303,
        )
    return RedirectResponse(
        url=f"/admin/live-chats?token={token}&chat_id={ticket.chat_id}",
        status_code=303,
    )


@router.get("/stats")
async def stats_api(token: str = "", db: AsyncSession = Depends(get_db)):
    await _current_user(db, token)
    return await chat_history.get_stats(db)


@router.get("/client/{chat_id}")
async def client_detail(chat_id: str, token: str = "", db: AsyncSession = Depends(get_db)):
    await _current_user(db, token)
    client = (await db.execute(select(Client).where(Client.chat_id == str(chat_id)))).scalar_one_or_none()

    # Підтягнути Telegram-ім'я / нік / фото (не чіпаємо CRM first/last)
    need_name = client is None or not (client.tg_first_name or client.tg_username)
    need_photo = True
    if client and client.tg_photo_url and str(client.tg_photo_url).startswith("/uploads/"):
        from pathlib import Path as _P
        local = _P(__file__).resolve().parent.parent / str(client.tg_photo_url).lstrip("/")
        need_photo = not local.exists()
    elif client and client.tg_photo_url:
        need_photo = True
    else:
        need_photo = True
    if need_name or need_photo:
        bot = TelegramBot()
        profile = await bot.enrich_client_profile(chat_id, force_photo=need_photo)
        if any(profile.get(k) for k in ("tg_first_name", "tg_last_name", "tg_username", "tg_photo_url")):
            if not client:
                client = Client(chat_id=str(chat_id), channel="telegram")
                db.add(client)
            if profile.get("tg_first_name"):
                client.tg_first_name = profile["tg_first_name"]
            if profile.get("tg_last_name") is not None:
                client.tg_last_name = profile["tg_last_name"]
            if profile.get("tg_username") is not None:
                client.tg_username = profile["tg_username"]
            if profile.get("tg_photo_url"):
                client.tg_photo_url = profile["tg_photo_url"]
            await db.commit()
            await db.refresh(client)

    orders = []
    if client:
        orders_result = await db.execute(
            select(Order).where(Order.client_id == client.id).order_by(Order.id.desc()).limit(10)
        )
        orders = [
            {
                "id": o.id,
                "product_type": o.product_type,
                "quantity": o.quantity,
                "total_amount": o.total_amount,
                "status": o.status.value if o.status else None,
            }
            for o in orders_result.scalars().all()
        ]
    human = await chat_history.is_human_mode(db, chat_id)
    return {
        "chat_id": chat_id,
        "human_mode": human,
        "client": {
            "id": client.id,
            "tg_first_name": client.tg_first_name,
            "tg_last_name": client.tg_last_name,
            "tg_name": chat_history.telegram_display_name(client),
            "order_first_name": client.first_name,
            "order_last_name": client.last_name,
            "order_name": chat_history.order_display_name(client),
            "tg_username": client.tg_username,
            "tg_photo_url": client.tg_photo_url,
            "phone": client.phone,
            "email": client.email,
            "city": client.city,
            "company_name": client.company_name,
            "edrpou": client.edrpou,
            "onebox_deal_id": client.onebox_deal_id,
        }
        if client
        else None,
        "orders": orders,
    }


# ---------- Команда (власник додає менеджерів по email) ----------


@router.get("/team", response_class=HTMLResponse)
async def team_page(request: Request, token: str = "", msg: str | None = None, err: str | None = None, db: AsyncSession = Depends(get_db)):
    user = await _current_user(db, token)
    if user.role != "owner":
        raise HTTPException(status_code=403, detail="Лише власник може керувати командою")
    admins = await list_admins(db)
    invite_creds = _pop_invite_flash(user.id)
    return templates.TemplateResponse(
        request,
        "admin_team.html",
        {
            "token": token,
            "active": "team",
            "admin": user,
            "avatar_url": _avatar_url(user),
            "admins": admins,
            "msg": msg,
            "err": err,
            "invite_creds": invite_creds,
        },
    )


@router.post("/team/invite")
async def team_invite(
    token: str = Form(...),
    email: str = Form(...),
    full_name: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    owner = await _current_user(db, token)
    if owner.role != "owner":
        raise HTTPException(status_code=403, detail="Forbidden")

    email_norm = (email or "").strip().lower()
    name = (full_name or "").strip()
    if not name:
        return RedirectResponse(
            url=f"/admin/team?token={token}&err={quote('Вкажіть імʼя менеджера')}",
            status_code=303,
        )
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email_norm):
        return RedirectResponse(
            url=f"/admin/team?token={token}&err={quote('Некоректний email')}",
            status_code=303,
        )

    existing = await db.execute(
        select(AdminUser).where(
            (AdminUser.email == email_norm) | (AdminUser.login == email_norm)
        )
    )
    if existing.scalar_one_or_none():
        return RedirectResponse(
            url=f"/admin/team?token={token}&err={quote('Користувач з цим email вже існує')}",
            status_code=303,
        )

    password = generate_password(12)
    # Логін = email, на який відправляється доступ
    manager = AdminUser(
        email=email_norm,
        login=email_norm,
        password_hash=hash_password(password),
        role="manager",
        full_name=name,
        is_active=True,
    )
    db.add(manager)
    await db.commit()
    await db.refresh(manager)

    mail = send_manager_invite_email(
        email=email_norm,
        password=password,
        inviter_email=owner.email,
        full_name=name,
    )
    # Завжди показуємо доступ власнику в панелі (SMTP не обов'язковий)
    _put_invite_flash(owner.id, {
        "full_name": name,
        "email": email_norm,
        "password": password,
        "email_sent": bool(mail.get("success") and not mail.get("mock")),
        "email_mock": bool(mail.get("mock")),
        "email_error": mail.get("error"),
    })
    if mail.get("success") and not mail.get("mock"):
        note = f"Менеджера {name} додано. Скопіюйте дані для входу нижче та надішліть менеджеру."
    else:
        note = f"Менеджера {name} додано. Скопіюйте дані для входу нижче та надішліть менеджеру."
    return RedirectResponse(
        url=f"/admin/team?token={token}&msg={quote(note)}",
        status_code=303,
    )


@router.post("/team/{admin_id}/deactivate")
async def team_deactivate(admin_id: int, token: str = Form(...), db: AsyncSession = Depends(get_db)):
    owner = await _current_user(db, token)
    if owner.role != "owner":
        raise HTTPException(status_code=403, detail="Forbidden")
    target = await get_admin_by_id(db, admin_id)
    if not target or target.role == "owner":
        return RedirectResponse(url=f"/admin/team?token={token}&err=Неможливо+деактивувати", status_code=303)
    target.is_active = False
    await db.commit()
    return RedirectResponse(url=f"/admin/team?token={token}&msg=Менеджера+деактивовано", status_code=303)


@router.post("/team/{admin_id}/delete")
async def team_delete(admin_id: int, token: str = Form(...), db: AsyncSession = Depends(get_db)):
    owner = await _current_user(db, token)
    if owner.role != "owner":
        raise HTTPException(status_code=403, detail="Forbidden")
    target = await get_admin_by_id(db, admin_id)
    if not target or target.role == "owner" or target.id == owner.id:
        return RedirectResponse(
            url=f"/admin/team?token={token}&err={quote('Неможливо видалити цього користувача')}",
            status_code=303,
        )
    name = target.full_name or target.email
    await db.delete(target)
    await db.commit()
    return RedirectResponse(
        url=f"/admin/team?token={token}&msg={quote(f'Менеджера {name} видалено')}",
        status_code=303,
    )


# ---------- Кабінет: ім'я, фото, пароль ----------


@router.get("/profile", response_class=HTMLResponse)
async def profile_page(
    request: Request,
    token: str = "",
    msg: str | None = None,
    err: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    user = await _current_user(db, token)
    return templates.TemplateResponse(
        request,
        "admin_profile.html",
        {
            "token": token,
            "active": "profile",
            "admin": user,
            "avatar_url": _avatar_url(user),
            "msg": msg,
            "err": err,
        },
    )


@router.post("/profile/update")
async def profile_update(
    token: str = Form(...),
    full_name: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    user = await _current_user(db, token)
    name = (full_name or "").strip()
    if not name:
        return RedirectResponse(
            url=f"/admin/profile?token={token}&err={quote('Вкажіть імʼя')}",
            status_code=303,
        )
    user.full_name = name
    # логін завжди = email
    user.login = user.email
    await db.commit()
    return RedirectResponse(
        url=f"/admin/profile?token={token}&msg={quote('Імʼя оновлено')}",
        status_code=303,
    )


@router.post("/profile/password")
async def profile_password(
    token: str = Form(...),
    current_password: str = Form(...),
    new_password: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    user = await _current_user(db, token)
    if not verify_password(current_password, user.password_hash):
        return RedirectResponse(url=f"/admin/profile?token={token}&err=Невірний+поточний+пароль", status_code=303)
    if len(new_password) < 6:
        return RedirectResponse(url=f"/admin/profile?token={token}&err=Новий+пароль+мінімум+6+символів", status_code=303)
    user.password_hash = hash_password(new_password)
    await db.commit()
    return RedirectResponse(
        url=f"/admin/profile?token={token}&msg={quote('Пароль успішно змінено')}",
        status_code=303,
    )


@router.post("/profile/avatar")
async def profile_avatar(
    token: str = Form(...),
    avatar: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
):
    user = await _current_user(db, token)
    content_type = (avatar.content_type or "").lower()
    if not content_type.startswith("image/"):
        return RedirectResponse(url=f"/admin/profile?token={token}&err=Потрібен+файл+зображення", status_code=303)

    ext = {
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
    }.get(content_type, ".jpg")
    filename = f"admin_{user.id}_{secrets.token_hex(6)}{ext}"
    dest = UPLOADS_DIR / filename
    data = await avatar.read()
    if len(data) > 5 * 1024 * 1024:
        return RedirectResponse(url=f"/admin/profile?token={token}&err=Файл+більше+5MB", status_code=303)
    dest.write_bytes(data)
    user.avatar_path = filename
    user.updated_at = datetime.now(timezone.utc)
    await db.commit()
    return RedirectResponse(url=f"/admin/profile?token={token}&msg=Фото+оновлено", status_code=303)


# ---------- AI налаштування (лише owner) ----------


@router.get("/ai-settings", response_class=HTMLResponse)
async def ai_settings_page(
    request: Request,
    token: str = "",
    msg: str | None = None,
    err: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    from app.services import bot_settings as bs

    user = await _current_user(db, token)
    if user.role != "owner":
        raise HTTPException(status_code=403, detail="Лише адмін (власник) може змінювати AI")
    bs.ensure_dirs()
    current_model = await bs.get_setting(db, bs.KEY_MODEL, bs.get_openai_model_sync())
    prompt = bs.get_prompt_sync()
    return templates.TemplateResponse(
        request,
        "admin_ai_settings.html",
        {
            "token": token,
            "active": "ai",
            "admin": user,
            "avatar_url": _avatar_url(user),
            "models": bs.OPENAI_CHAT_MODELS,
            "current_model": current_model,
            "prompt": prompt,
            "knowledge_files": bs.list_knowledge_files(),
            "msg": msg or request.query_params.get("msg"),
            "err": err or request.query_params.get("err"),
        },
    )


@router.post("/ai-settings/model")
async def ai_settings_model(
    token: str = Form(...),
    model: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    from app.services import bot_settings as bs

    user = await _current_user(db, token)
    if user.role != "owner":
        raise HTTPException(status_code=403, detail="Forbidden")
    try:
        await bs.save_model(db, model)
    except ValueError as exc:
        return RedirectResponse(
            url=f"/admin/ai-settings?token={token}&err={quote(str(exc))}",
            status_code=303,
        )
    return RedirectResponse(
        url=f"/admin/ai-settings?token={token}&msg={quote('Модель збережено')}",
        status_code=303,
    )


@router.post("/ai-settings/prompt")
async def ai_settings_prompt(
    token: str = Form(...),
    prompt: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    from app.services import bot_settings as bs

    user = await _current_user(db, token)
    if user.role != "owner":
        raise HTTPException(status_code=403, detail="Forbidden")
    await bs.save_prompt(db, prompt)
    return RedirectResponse(
        url=f"/admin/ai-settings?token={token}&msg={quote('Промпт збережено')}",
        status_code=303,
    )


@router.post("/ai-settings/knowledge")
async def ai_settings_knowledge_upload(
    token: str = Form(...),
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
):
    from app.services import bot_settings as bs

    user = await _current_user(db, token)
    if user.role != "owner":
        raise HTTPException(status_code=403, detail="Forbidden")
    raw = await file.read()
    if len(raw) > 8 * 1024 * 1024:
        return RedirectResponse(
            url=f"/admin/ai-settings?token={token}&err={quote('Файл більше 8MB')}",
            status_code=303,
        )
    name = bs.save_knowledge_file(file.filename or "knowledge.txt", raw)
    return RedirectResponse(
        url=f"/admin/ai-settings?token={token}&msg={quote(f'Завантажено {name}')}",
        status_code=303,
    )


@router.post("/ai-settings/knowledge/delete")
async def ai_settings_knowledge_delete(
    token: str = Form(...),
    filename: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    from app.services import bot_settings as bs

    user = await _current_user(db, token)
    if user.role != "owner":
        raise HTTPException(status_code=403, detail="Forbidden")
    ok = bs.delete_knowledge_file(filename)
    if not ok:
        return RedirectResponse(
            url=f"/admin/ai-settings?token={token}&err={quote('Файл не знайдено')}",
            status_code=303,
        )
    return RedirectResponse(
        url=f"/admin/ai-settings?token={token}&msg={quote('Файл видалено')}",
        status_code=303,
    )
