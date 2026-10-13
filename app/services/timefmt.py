"""Час у адмінці — Київ (Europe/Kyiv). У БД зберігається UTC."""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    KYIV = ZoneInfo("Europe/Kyiv")
except ZoneInfoNotFoundError:
    KYIV = ZoneInfo("Europe/Kiev")


def to_kyiv(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(KYIV)


def format_kyiv(dt: datetime | None, fmt: str = "%d.%m.%Y %H:%M") -> str:
    local = to_kyiv(dt)
    if local is None:
        return ""
    return local.strftime(fmt)
