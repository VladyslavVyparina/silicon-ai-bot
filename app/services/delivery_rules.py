"""Правила збору даних доставки Новою поштою."""
from __future__ import annotations

from typing import Any

DELIVERY_TYPE_WAREHOUSE = "warehouse"
DELIVERY_TYPE_POSTOMAT = "postomat"
DELIVERY_TYPE_ADDRESS = "address"


def build_recipient_name(payload: dict[str, Any]) -> str:
    if payload.get("recipient_name") and not (
        payload.get("recipient_last_name") or payload.get("recipient_first_name")
    ):
        return str(payload["recipient_name"]).strip()

    parts = [
        payload.get("recipient_last_name") or "",
        payload.get("recipient_first_name") or "",
    ]
    if payload.get("delivery_type") == DELIVERY_TYPE_ADDRESS:
        parts.append(payload.get("recipient_patronymic") or "")
    name = " ".join(p.strip() for p in parts if p and str(p).strip())
    return name or str(payload.get("recipient_name") or "").strip()


def validate_delivery_payload(payload: dict[str, Any]) -> tuple[bool, list[str]]:
    """
    Відділення/поштомат: прізвище, ім'я, телефон, місто, № відділення.
    Адресна: прізвище, ім'я, по батькові, телефон, місто, адреса.
    """
    errors: list[str] = []
    dtype = payload.get("delivery_type") or DELIVERY_TYPE_WAREHOUSE

    last = (payload.get("recipient_last_name") or "").strip()
    first = (payload.get("recipient_first_name") or "").strip()
    phone = (payload.get("recipient_phone") or "").strip()
    city = (payload.get("city") or "").strip()

    # Backward compatible: full recipient_name without split
    full = (payload.get("recipient_name") or "").strip()
    if not last and not first and full:
        parts = full.split()
        if len(parts) >= 2:
            last, first = parts[0], parts[1]
            if dtype == DELIVERY_TYPE_ADDRESS and len(parts) >= 3 and not payload.get("recipient_patronymic"):
                payload["recipient_patronymic"] = " ".join(parts[2:])
        elif len(parts) == 1:
            first = parts[0]
            last = parts[0]

    if not last:
        errors.append("прізвище")
    if not first:
        errors.append("ім'я")
    if not phone:
        errors.append("телефон")
    if not city:
        errors.append("місто")

    if dtype == DELIVERY_TYPE_ADDRESS:
        if not (payload.get("recipient_patronymic") or "").strip():
            errors.append("по батькові")
        if not (payload.get("street_address") or "").strip():
            errors.append("адреса доставки")
    else:
        if not (payload.get("warehouse") or payload.get("warehouse_ref") or "").strip():
            label = "номер поштомату" if dtype == DELIVERY_TYPE_POSTOMAT else "номер відділення"
            errors.append(label)

    # Write back normalized split names
    payload["recipient_last_name"] = last
    payload["recipient_first_name"] = first
    payload["recipient_name"] = build_recipient_name(payload)

    return (len(errors) == 0, errors)
