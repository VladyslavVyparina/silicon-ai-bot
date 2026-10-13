"""Авторизація адмін-панелі: користувачі, токени, паролі."""
from __future__ import annotations

import hashlib
import secrets
import string
from typing import Any

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database.models import AdminUser

TOKEN_MAX_AGE = 60 * 60 * 24 * 7  # 7 днів


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().admin_secret_key, salt="silicon-admin")


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 120_000)
    return f"{salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt, hashed = stored.split("$", 1)
    except ValueError:
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 120_000)
    return secrets.compare_digest(digest.hex(), hashed)


def generate_password(length: int = 12) -> str:
    alphabet = string.ascii_letters + string.digits
    # гарантуємо хоча б одну цифру і літеру
    chars = [
        secrets.choice(string.ascii_lowercase),
        secrets.choice(string.ascii_uppercase),
        secrets.choice(string.digits),
    ]
    chars += [secrets.choice(alphabet) for _ in range(max(0, length - 3))]
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)


def create_admin_token(user: AdminUser | dict[str, Any]) -> str:
    if isinstance(user, dict):
        payload = {
            "role": user.get("role", "manager"),
            "admin_id": user.get("id") or user.get("admin_id"),
            "email": user.get("email"),
            "login": user.get("login"),
        }
    else:
        payload = {
            "role": user.role,
            "admin_id": user.id,
            "email": user.email,
            "login": user.login,
        }
    return _serializer().dumps(payload)


def decode_admin_token(token: str | None) -> dict[str, Any] | None:
    if not token:
        return None
    try:
        data = _serializer().loads(token, max_age=TOKEN_MAX_AGE)
        if not data.get("admin_id"):
            return None
        return data
    except (BadSignature, SignatureExpired, TypeError, ValueError):
        return None


def verify_admin_token(token: str | None) -> bool:
    return decode_admin_token(token) is not None


async def get_admin_by_id(db: AsyncSession, admin_id: int) -> AdminUser | None:
    result = await db.execute(select(AdminUser).where(AdminUser.id == admin_id))
    return result.scalar_one_or_none()


async def get_admin_by_login_or_email(db: AsyncSession, identifier: str) -> AdminUser | None:
    ident = (identifier or "").strip().lower()
    if not ident:
        return None
    result = await db.execute(
        select(AdminUser).where(
            or_(AdminUser.email == ident, AdminUser.login == ident),
            AdminUser.is_active == True,  # noqa: E712
        )
    )
    return result.scalar_one_or_none()


async def authenticate_admin(db: AsyncSession, identifier: str, password: str) -> AdminUser | None:
    user = await get_admin_by_login_or_email(db, identifier)
    if not user:
        return None
    if not verify_password(password, user.password_hash):
        return None
    return user


async def ensure_owner_exists(db: AsyncSession) -> AdminUser:
    """Створює власника з .env, якщо в БД ще немає admin-користувачів."""
    result = await db.execute(select(AdminUser).limit(1))
    existing = result.scalar_one_or_none()
    if existing:
        owner = await db.execute(select(AdminUser).where(AdminUser.role == "owner").limit(1))
        found = owner.scalar_one_or_none()
        if found:
            return found
        return existing

    settings = get_settings()
    email = (settings.admin_owner_email or "owner@silicon.local").strip().lower()
    # Логін = email
    user = AdminUser(
        email=email,
        login=email,
        password_hash=hash_password(settings.admin_password),
        role="owner",
        full_name="Власник Silicon",
        is_active=True,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def list_admins(db: AsyncSession) -> list[AdminUser]:
    result = await db.execute(select(AdminUser).order_by(AdminUser.id))
    return list(result.scalars().all())


# backward-compat для старих тестів
def check_password(password: str) -> bool:
    return bool(password) and password == get_settings().admin_password
