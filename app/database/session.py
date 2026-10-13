from collections.abc import AsyncGenerator
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import PROJECT_ROOT, get_settings
from app.database.models import Base

settings = get_settings()

# Гарантуємо каталог data/ для SQLite-файлу
(Path(PROJECT_ROOT) / "data").mkdir(parents=True, exist_ok=True)

engine = create_async_engine(settings.database_url, echo=settings.debug)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_migrate_sqlite)


def _migrate_sqlite(sync_conn) -> None:
    """Додає нові колонки в існуючу SQLite БД без Alembic."""
    from sqlalchemy import text

    def cols(table: str) -> set[str]:
        rows = sync_conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
        return {r[1] for r in rows}

    try:
        existing = cols("delivery_info")
    except Exception:
        return

    alters = {
        "delivery_type": "ALTER TABLE delivery_info ADD COLUMN delivery_type VARCHAR(32) DEFAULT 'warehouse'",
        "recipient_last_name": "ALTER TABLE delivery_info ADD COLUMN recipient_last_name VARCHAR(128)",
        "recipient_first_name": "ALTER TABLE delivery_info ADD COLUMN recipient_first_name VARCHAR(128)",
        "recipient_patronymic": "ALTER TABLE delivery_info ADD COLUMN recipient_patronymic VARCHAR(128)",
        "street_address": "ALTER TABLE delivery_info ADD COLUMN street_address VARCHAR(512)",
    }
    for name, sql in alters.items():
        if name not in existing:
            sync_conn.execute(text(sql))

    try:
        state_cols = cols("conversation_states")
        if "human_mode" not in state_cols:
            sync_conn.execute(
                text("ALTER TABLE conversation_states ADD COLUMN human_mode BOOLEAN DEFAULT 0")
            )
    except Exception:
        pass

    try:
        client_cols = cols("clients")
        for name, sql in {
            "tg_username": "ALTER TABLE clients ADD COLUMN tg_username VARCHAR(128)",
            "tg_photo_url": "ALTER TABLE clients ADD COLUMN tg_photo_url VARCHAR(512)",
            "tg_first_name": "ALTER TABLE clients ADD COLUMN tg_first_name VARCHAR(128)",
            "tg_last_name": "ALTER TABLE clients ADD COLUMN tg_last_name VARCHAR(128)",
        }.items():
            if name not in client_cols:
                sync_conn.execute(text(sql))
        # одноразово: якщо tg-ім'я порожнє — скопіювати зі старих first/last
        client_cols = cols("clients")
        if "tg_first_name" in client_cols:
            sync_conn.execute(
                text(
                    "UPDATE clients SET tg_first_name = first_name "
                    "WHERE (tg_first_name IS NULL OR tg_first_name = '') "
                    "AND first_name IS NOT NULL AND first_name != ''"
                )
            )
            sync_conn.execute(
                text(
                    "UPDATE clients SET tg_last_name = last_name "
                    "WHERE (tg_last_name IS NULL OR tg_last_name = '') "
                    "AND last_name IS NOT NULL AND last_name != ''"
                )
            )
    except Exception:
        pass

    try:
        pay_cols = cols("payments")
        for name, sql in {
            "liqpay_data": "ALTER TABLE payments ADD COLUMN liqpay_data TEXT",
            "liqpay_signature": "ALTER TABLE payments ADD COLUMN liqpay_signature TEXT",
        }.items():
            if name not in pay_cols:
                sync_conn.execute(text(sql))
    except Exception:
        pass

    try:
        ticket_cols = cols("manager_tickets")
        for name, sql in {
            "assigned_to_id": "ALTER TABLE manager_tickets ADD COLUMN assigned_to_id INTEGER",
            "closed_by_id": "ALTER TABLE manager_tickets ADD COLUMN closed_by_id INTEGER",
            "claimed_at": "ALTER TABLE manager_tickets ADD COLUMN claimed_at DATETIME",
            "closed_at": "ALTER TABLE manager_tickets ADD COLUMN closed_at DATETIME",
        }.items():
            if name not in ticket_cols:
                sync_conn.execute(text(sql))
    except Exception:
        pass



async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with async_session() as session:
        yield session
