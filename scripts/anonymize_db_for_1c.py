#!/usr/bin/env python3
"""Копія SQLite БД з анонімізацією ПІБ/телефонів + тестовий адмін для 1С-стенду."""
from __future__ import annotations

import argparse
import hashlib
import secrets
import shutil
import sqlite3
import sys
from pathlib import Path

# Фіксований тестовий пароль адмінки стенду (для програміста 1С)
TEST_ADMIN_LOGIN = "1c_dev"
TEST_ADMIN_EMAIL = "1c-dev@silicon.local"
TEST_ADMIN_PASSWORD = "CHANGE_ME"


def _hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), 120_000
    )
    return f"{salt}${digest.hex()}"


def _table_cols(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {r[1] for r in rows}


def anonymize(src: Path, dst: Path) -> dict:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        dst.unlink()
    shutil.copy2(src, dst)

    conn = sqlite3.connect(str(dst))
    conn.row_factory = sqlite3.Row
    stats: dict[str, int] = {}

    # clients
    if "clients" in {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }:
        cols = _table_cols(conn, "clients")
        clients = conn.execute("SELECT id FROM clients").fetchall()
        for i, row in enumerate(clients, start=1):
            cid = row["id"]
            updates = {
                "first_name": f"Тест{i}",
                "last_name": f"Клієнт{i}",
                "phone": f"+38050{1000000 + i:07d}"[:13],
                "email": f"test{i}@example.local",
                "tg_first_name": f"TG{i}",
                "tg_last_name": "Test",
                "tg_username": f"test_user_{i}",
                "tg_photo_url": None,
                "chat_id": f"1c-test-chat-{i}",
            }
            # тестові ЄДРПОУ лишаємо / підставляємо відомі для mock 1С
            if "edrpou" in cols:
                if i == 1:
                    updates["edrpou"] = "12345678"
                    updates["company_name"] = "ТОВ Сілікон"
                    updates["onec_counterparty_id"] = "1c-001"
                elif i == 2:
                    updates["edrpou"] = "87654321"
                    updates["company_name"] = "ПП Тест Компані"
                    updates["onec_counterparty_id"] = "1c-002"
                else:
                    updates["edrpou"] = f"{90000000 + i}"
                    updates["company_name"] = f"ТОВ Тест {i}"
                    updates["onec_counterparty_id"] = None
            sets = ", ".join(f"{k}=?" for k in updates if k in cols)
            vals = [updates[k] for k in updates if k in cols] + [cid]
            conn.execute(f"UPDATE clients SET {sets} WHERE id=?", vals)
        stats["clients"] = len(clients)

    # delivery
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "delivery_info" in tables:
        cols = _table_cols(conn, "delivery_info")
        rows = conn.execute("SELECT id FROM delivery_info").fetchall()
        for i, row in enumerate(rows, start=1):
            updates = {
                "recipient_last_name": f"Отримувач{i}",
                "recipient_first_name": "Тест",
                "recipient_patronymic": "Іванович",
                "recipient_name": f"Отримувач{i} Тест Іванович",
                "recipient_phone": f"+38067{1000000 + i:07d}"[:13],
                "city": "Київ",
                "warehouse": "Відділення №1",
                "street_address": "вул. Тестова, 1",
            }
            sets = ", ".join(f"{k}=?" for k in updates if k in cols)
            vals = [updates[k] for k in updates if k in cols] + [row["id"]]
            if sets:
                conn.execute(f"UPDATE delivery_info SET {sets} WHERE id=?", vals)
        stats["delivery_info"] = len(rows)

    # conversation chat_ids + scrub JSON phones roughly
    if "conversation_states" in tables:
        rows = conn.execute("SELECT id, chat_id, data_json FROM conversation_states").fetchall()
        for i, row in enumerate(rows, start=1):
            conn.execute(
                "UPDATE conversation_states SET chat_id=?, data_json=?, human_mode=0, step='idle' WHERE id=?",
                (f"1c-test-chat-{i}", "{}", row["id"]),
            )
        stats["conversation_states"] = len(rows)

    if "chat_messages" in tables:
        # залишаємо структуру, чистимо текст
        n = conn.execute(
            "UPDATE chat_messages SET content='[анонімізовано для тестового стенду 1С]', chat_id='1c-test-chat-1'"
        ).rowcount
        stats["chat_messages"] = n

    if "manager_tickets" in tables:
        conn.execute(
            "UPDATE manager_tickets SET question='[тестова заявка]', chat_id='1c-test-chat-1', status='closed'"
        )
        stats["manager_tickets"] = 1

    if "webhooks_log" in tables:
        conn.execute("DELETE FROM webhooks_log")
        stats["webhooks_log_deleted"] = 1

    if "payments" in tables:
        cols = _table_cols(conn, "payments")
        if "payment_url" in cols:
            conn.execute("UPDATE payments SET payment_url=NULL, liqpay_data=NULL, liqpay_signature=NULL")
        stats["payments_scrubbed"] = 1

    if "admin_users" in tables:
        conn.execute("DELETE FROM admin_users")
        conn.execute(
            """
            INSERT INTO admin_users (email, login, password_hash, role, full_name, is_active)
            VALUES (?, ?, ?, 'owner', '1C Developer', 1)
            """,
            (TEST_ADMIN_EMAIL, TEST_ADMIN_LOGIN, _hash_password(TEST_ADMIN_PASSWORD)),
        )
        stats["admin_users"] = 1

    # прибрати секрети з app_settings
    if "app_settings" in tables:
        conn.execute(
            "DELETE FROM app_settings WHERE key LIKE '%key%' OR key LIKE '%secret%' OR key LIKE '%token%' OR key LIKE '%password%'"
        )

    conn.commit()
    conn.execute("VACUUM")
    conn.close()
    return stats


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--src", type=Path, required=True)
    p.add_argument("--dst", type=Path, required=True)
    args = p.parse_args()
    if not args.src.is_file():
        print(f"Немає файлу БД: {args.src}", file=sys.stderr)
        return 1
    stats = anonymize(args.src, args.dst)
    print(f"OK → {args.dst}")
    for k, v in stats.items():
        print(f"  {k}: {v}")
    print(f"ADMIN login={TEST_ADMIN_LOGIN} password={TEST_ADMIN_PASSWORD}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
