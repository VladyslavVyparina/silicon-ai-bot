"""Локальний прогін кількох діалогів Олега (без Telegram)."""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("MOCK_EXTERNAL_APIS", "true")

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.database.models import Base  # noqa: E402
from app.services.conversation_flow import process_message  # noqa: E402

AI_MARKERS = (
    "•",
    "як експерт",
    "чудове питання",
    "з радістю допоможу",
    "штучний інтелект",
    "згідно з базою",
    "передаю в crm",
    "передаю в 1с",
    "pricelist",
    "не впевнений, що саме порахувати",
)


async def chat(db, chat_id: str, message: str) -> dict:
    resp = await process_message(db, chat_id, message)
    return {"action": resp.action, "text": resp.response or ""}


def check(name: str, text: str, problems: list[str]) -> None:
    lower = text.lower()
    for bad in AI_MARKERS:
        if bad in lower:
            problems.append(f"{name}: заборонене «{bad}»")
    if "🙂" in text or "😊" in text:
        problems.append(f"{name}: емодзі")


async def run() -> int:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    problems: list[str] = []
    async with Session() as db:
        print("\n===== 1. Воронка + сторонні питання =====")
        steps = [
            ("привіт", None),
            ("які товари і ціни?", ["силікон", "порахувати"]),
            ("силіконові 20 мм", ["кількість", "20"]),
            ("а доставка як?", ["пошт", "самовивіз"]),
            ("151 штука", ["151", "оформлю"]),
            ("а можна швидше?", ["термін", "завантаж"]),
            ("оплату як?", ["карт", "рахунок"]),
            ("а якщо 200?", ["200"]),
            ("ще світловідбиваючі 100 штук", ["світловідб", "разом"]),
        ]
        cid = "sim-funnel"
        for msg, must in steps:
            r = await chat(db, cid, msg)
            print(f"\nКлієнт: {msg}\n[{r['action']}]\n{r['text']}")
            check(msg, r["text"], problems)
            if must:
                low = r["text"].lower()
                if not any(m in low for m in must):
                    problems.append(f"«{msg}»: немає жодного з {must}")

        print("\n===== 2. Питання поза воронкою =====")
        cid = "sim-faq"
        steps = [
            ("хто ви?", ["silicon"]),
            ("робите RFID?", ["rfid"]),
            ("для фестивалю що краще?", ["tyvek", "rfid"]),
            ("який макет потрібен?", ["ai", "pdf", "eps"]),
            ("еко-чашки 50 штук", ["індивідуал", "чашк"]),
        ]
        for msg, must in steps:
            r = await chat(db, cid, msg)
            print(f"\nКлієнт: {msg}\n[{r['action']}]\n{r['text']}")
            check(msg, r["text"], problems)
            if must:
                low = r["text"].lower()
                if not any(m in low for m in must):
                    problems.append(f"«{msg}»: немає жодного з {must}")

        print("\n===== 3. Оформлення: питання не стає ім'ям =====")
        cid = "sim-crm"
        await chat(db, cid, "силіконові браслети 12 мм 100 штук")
        await chat(db, cid, "так")
        r = await chat(db, cid, "а доставка як?")
        print(f"\nКлієнт: а доставка як?\n[{r['action']}]\n{r['text']}")
        check("crm-delivery", r["text"], problems)
        if r["action"] != "collect_order_data":
            problems.append(f"після «а доставка як?» action={r['action']}, очікував collect_order_data")
        if "ім" not in r["text"].lower() and "прізвище" not in r["text"].lower():
            problems.append("після питання про доставку не повернув до імені")
        r2 = await chat(db, cid, "Іван Петренко")
        print(f"\nКлієнт: Іван Петренко\n[{r2['action']}]\n{r2['text']}")
        if "телефон" not in r2["text"].lower():
            problems.append("після імені не спитав телефон")

    print("\n===== РЕЗУЛЬТАТ =====")
    if problems:
        print("PROBLEMS:")
        for p in problems:
            print("-", p)
        return 1
    print("PROBLEMS: none")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
