from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Корінь проєкту (…/ai_silicon). БД тримаємо в data/ — поза кодом, щоб rsync не затирав.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "silicon_bot.db"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "Silicon AI Bot Integrator"
    debug: bool = False
    # Абсолютний шлях за замовчуванням — не залежить від CWD і не лежить у корені поруч із кодом
    database_url: str = f"sqlite+aiosqlite:///{_DEFAULT_DB_PATH}"

    # OpenAI
    openai_api_key: str = ""
    openai_model: str = "gpt-5"

    # Telegram
    telegram_bot_token: str = ""

    # SendPulse
    sendpulse_client_id: str = ""
    sendpulse_client_secret: str = ""
    sendpulse_bot_id: str = ""

    # Onebox CRM
    onebox_api_url: str = "https://api.onebox.systems"
    onebox_api_key: str = ""
    onebox_webhook_secret: str = ""
    onebox_login: str = ""
    onebox_rest_api_password: str = ""

    # LiqPay
    liqpay_public_key: str = ""
    liqpay_private_key: str = ""
    liqpay_sandbox: bool = True
    # Скільки секунд після створення посилання самі питаємо LiqPay, чи пройшла оплата
    liqpay_watch_seconds: int = 900
    liqpay_watch_interval_seconds: int = 20
    # Публічний webhook LiqPay (якщо порожньо — {PUBLIC_BASE_URL}/api/webhooks/liqpay)
    liqpay_server_url: str = ""

    # Nova Poshta
    nova_poshta_api_key: str = ""
    nova_poshta_sender_ref: str = ""
    nova_poshta_contact_sender_ref: str = ""
    nova_poshta_city_sender_ref: str = ""
    nova_poshta_sender_address_ref: str = ""

    # 1C
    onec_api_url: str = ""
    onec_api_user: str = ""
    onec_api_password: str = ""

    # Реквізити продавця для PDF-рахунку
    invoice_seller_name: str = "ФОП Афонін Дмитро Вікторович"
    invoice_seller_address: str = (
        "вулиця Київська, буд. 22, с. Бриків, Шумський район, Тернопільська обл., 47140"
    )
    invoice_seller_phone: str = "0958814263"
    invoice_seller_ipn: str = "3052600213"
    invoice_seller_bank: str = 'КБ "ПРИВАТБАНК" М.ДНІПРО'
    invoice_seller_iban: str = "UA393052990000026006003304573"

    # Mock mode for testing without real API keys
    mock_external_apis: bool = True

    # Admin panel
    admin_password: str = "silicon-admin"
    admin_owner_email: str = "owner@silicon.local"
    admin_secret_key: str = "change-me-silicon-admin-secret"
    # Адмін-панель (може бути внутрішнім URL). Для клієнтських посилань оплати — PUBLIC_BASE_URL.
    admin_public_url: str = "http://127.0.0.1:8000"
    # Публічний URL для посилань /pay/... (можна IP: http://173.242.60.134:8000)
    public_base_url: str = ""
    admin_notify_chat_id: str = ""  # Telegram chat for manager alerts

    # SMTP for inviting managers
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_use_tls: bool = True

    consultant_instructions_path: str = "app/data/consultant_instructions.txt"
    pricing_data_path: str = "app/data/pricing.json"

    # Production terms (days)
    default_production_days: int = 14
    urgent_production_days: int = 7


@lru_cache
def get_settings() -> Settings:
    return Settings()


def get_public_base_url() -> str:
    """Базовий URL для клієнтських посилань оплати. Домен не обовʼязковий — IP:порт теж ок."""
    s = get_settings()
    base = (s.public_base_url or s.admin_public_url or "").strip().rstrip("/")
    return base or "http://127.0.0.1:8000"


def is_https_url(url: str | None) -> bool:
    return str(url or "").strip().lower().startswith("https://")
