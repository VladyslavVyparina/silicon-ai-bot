"""Надсилання листів (запрошення менеджерів)."""
from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

from app.config import get_settings

logger = logging.getLogger(__name__)

# Для тестів: останні «надіслані» листи
_sent_emails: list[dict] = []


def reset_sent_emails() -> None:
    _sent_emails.clear()


def get_sent_emails() -> list[dict]:
    return list(_sent_emails)


def send_email(to_email: str, subject: str, body: str) -> dict:
    settings = get_settings()
    payload = {"to": to_email, "subject": subject, "body": body}
    _sent_emails.append(payload)

    if settings.mock_external_apis or not settings.smtp_host:
        logger.info("Email mock → %s: %s", to_email, subject)
        return {"success": True, "mock": True, **payload}

    from_addr = settings.smtp_from or settings.smtp_user or "noreply@silicon.local"
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_email
    msg.set_content(body)

    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as smtp:
            if settings.smtp_use_tls:
                smtp.starttls()
            if settings.smtp_user and settings.smtp_password:
                smtp.login(settings.smtp_user, settings.smtp_password)
            smtp.send_message(msg)
        return {"success": True, "mock": False, **payload}
    except Exception as exc:
        logger.exception("SMTP send failed")
        return {"success": False, "error": str(exc), **payload}


def send_manager_invite_email(
    *,
    email: str,
    password: str,
    inviter_email: str,
    full_name: str | None = None,
) -> dict:
    settings = get_settings()
    panel_url = settings.admin_public_url.rstrip("/") + "/admin"
    greeting = f"Вітаємо{', ' + full_name if full_name else ''}!"
    subject = "Доступ до адмін-панелі Silicon"
    body = (
        f"{greeting}\n\n"
        f"Вам надано доступ менеджера до адмін-панелі Silicon (бот Олег).\n"
        f"Запросив: {inviter_email}\n\n"
        f"Email / логін: {email}\n"
        f"Пароль: {password}\n\n"
        f"Увійти: {panel_url}\n\n"
        f"Рекомендуємо змінити пароль у кабінеті після першого входу.\n"
        f"— Silicon"
    )
    return send_email(email, subject, body)
