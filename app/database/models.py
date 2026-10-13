import enum
from datetime import datetime

from sqlalchemy import DateTime, Enum, Float, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class OrderStatus(str, enum.Enum):
    NEW = "new"
    CONSULTATION = "consultation"
    AWAITING_PAYMENT = "awaiting_payment"
    PAID = "paid"
    IN_PRODUCTION = "in_production"
    COMPLETED = "completed"
    SHIPPED = "shipped"
    CANCELLED = "cancelled"


class PaymentStatus(str, enum.Enum):
    PENDING = "pending"
    SUCCESS = "success"
    FAILURE = "failure"
    ERROR = "error"


class Client(Base):
    __tablename__ = "clients"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    channel: Mapped[str] = mapped_column(String(32), default="telegram")
    # Ім'я з замовлення / CRM (не показуємо як Telegram-ім'я в адмінці)
    first_name: Mapped[str | None] = mapped_column(String(128))
    last_name: Mapped[str | None] = mapped_column(String(128))
    # Ім'я лише з Telegram-профілю
    tg_first_name: Mapped[str | None] = mapped_column(String(128))
    tg_last_name: Mapped[str | None] = mapped_column(String(128))
    tg_username: Mapped[str | None] = mapped_column(String(128))
    tg_photo_url: Mapped[str | None] = mapped_column(String(512))
    phone: Mapped[str | None] = mapped_column(String(32))
    email: Mapped[str | None] = mapped_column(String(256))
    city: Mapped[str | None] = mapped_column(String(128))
    onebox_contact_id: Mapped[str | None] = mapped_column(String(64))
    onebox_deal_id: Mapped[str | None] = mapped_column(String(64))
    onec_counterparty_id: Mapped[str | None] = mapped_column(String(64))
    edrpou: Mapped[str | None] = mapped_column(String(16))
    company_name: Mapped[str | None] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    orders: Mapped[list["Order"]] = relationship(back_populates="client")
    delivery_info: Mapped["DeliveryInfo | None"] = relationship(back_populates="client", uselist=False)


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"))
    product_type: Mapped[str | None] = mapped_column(String(128))
    product_params: Mapped[str | None] = mapped_column(Text)
    quantity: Mapped[int | None] = mapped_column(Integer)
    unit_price: Mapped[float | None] = mapped_column(Float)
    total_amount: Mapped[float | None] = mapped_column(Float)
    production_days: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[OrderStatus] = mapped_column(Enum(OrderStatus), default=OrderStatus.NEW)
    comment: Mapped[str | None] = mapped_column(Text)
    ttn_number: Mapped[str | None] = mapped_column(String(64))
    ttn_url: Mapped[str | None] = mapped_column(String(512))
    invoice_number: Mapped[str | None] = mapped_column(String(64))
    liqpay_order_id: Mapped[str | None] = mapped_column(String(128))
    payment_status: Mapped[PaymentStatus] = mapped_column(
        Enum(PaymentStatus), default=PaymentStatus.PENDING
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    client: Mapped["Client"] = relationship(back_populates="orders")
    payments: Mapped[list["Payment"]] = relationship(back_populates="order")


class DeliveryInfo(Base):
    __tablename__ = "delivery_info"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"), unique=True)
    delivery_type: Mapped[str] = mapped_column(String(32), default="warehouse")  # warehouse|postomat|address
    recipient_last_name: Mapped[str | None] = mapped_column(String(128))
    recipient_first_name: Mapped[str | None] = mapped_column(String(128))
    recipient_patronymic: Mapped[str | None] = mapped_column(String(128))
    recipient_name: Mapped[str] = mapped_column(String(256))
    recipient_phone: Mapped[str] = mapped_column(String(32))
    city: Mapped[str] = mapped_column(String(128))
    city_ref: Mapped[str | None] = mapped_column(String(64))
    warehouse: Mapped[str | None] = mapped_column(String(256))
    warehouse_ref: Mapped[str | None] = mapped_column(String(64))
    street_address: Mapped[str | None] = mapped_column(String(512))
    weight_kg: Mapped[float] = mapped_column(Float, default=1.0)
    seats_amount: Mapped[int] = mapped_column(Integer, default=1)
    description: Mapped[str | None] = mapped_column(String(256))

    client: Mapped["Client"] = relationship(back_populates="delivery_info")


class ConversationState(Base):
    """Стан діалогу клієнта (сценарій Олега)."""

    __tablename__ = "conversation_states"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    step: Mapped[str] = mapped_column(String(64), default="idle")
    data_json: Mapped[str | None] = mapped_column(Text)
    human_mode: Mapped[bool] = mapped_column(default=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class ChatMessage(Base):
    """Історія повідомлень клієнт ↔ бот ↔ менеджер."""

    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[str] = mapped_column(String(128), index=True)
    sender: Mapped[str] = mapped_column(String(16))  # user | bot | admin
    content: Mapped[str] = mapped_column(Text)
    content_type: Mapped[str] = mapped_column(String(32), default="text")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)


class ManagerTicket(Base):
    """Заявка «зв'язати з менеджером» для адмін-панелі."""

    __tablename__ = "manager_tickets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[str] = mapped_column(String(128), index=True)
    client_id: Mapped[int | None] = mapped_column(ForeignKey("clients.id"), nullable=True)
    question: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="open")  # open | in_progress | closed
    is_read: Mapped[bool] = mapped_column(default=False)
    assigned_to_id: Mapped[int | None] = mapped_column(ForeignKey("admin_users.id"), nullable=True)
    closed_by_id: Mapped[int | None] = mapped_column(ForeignKey("admin_users.id"), nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    client: Mapped["Client | None"] = relationship()
    assigned_to: Mapped["AdminUser | None"] = relationship(
        foreign_keys=[assigned_to_id],
    )
    closed_by: Mapped["AdminUser | None"] = relationship(
        foreign_keys=[closed_by_id],
    )


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"))
    amount: Mapped[float] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String(8), default="UAH")
    liqpay_payment_id: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[PaymentStatus] = mapped_column(Enum(PaymentStatus), default=PaymentStatus.PENDING)
    payment_url: Mapped[str | None] = mapped_column(String(1024))
    liqpay_data: Mapped[str | None] = mapped_column(Text)
    liqpay_signature: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    order: Mapped["Order"] = relationship(back_populates="payments")


class CatalogItem(Base):
    __tablename__ = "catalog"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(256), index=True)
    category: Mapped[str] = mapped_column(String(128), index=True)
    description: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(String(512))
    price_info: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Template(Base):
    __tablename__ = "templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    keywords: Mapped[str] = mapped_column(String(512), index=True)
    response_text: Mapped[str] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(default=True)


class WebhookLog(Base):
    __tablename__ = "webhooks_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(64))
    payload: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class AdminUser(Base):
    """Власник / менеджер адмін-панелі."""

    __tablename__ = "admin_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(256), unique=True, index=True)
    login: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(256))
    role: Mapped[str] = mapped_column(String(32), default="manager")  # owner | manager
    full_name: Mapped[str | None] = mapped_column(String(256))
    avatar_path: Mapped[str | None] = mapped_column(String(512))
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class InvoicePaymentNotice(Base):
    """Клієнт повідомив про оплату за PDF-рахунком / реквізитами — черга для адміна."""

    __tablename__ = "invoice_payment_notices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[str] = mapped_column(String(128), index=True)
    order_id: Mapped[int | None] = mapped_column(Integer)
    invoice_number: Mapped[str | None] = mapped_column(String(64))
    amount: Mapped[float | None] = mapped_column(Float)
    company_name: Mapped[str | None] = mapped_column(String(256))
    message: Mapped[str | None] = mapped_column(Text)
    screenshot_path: Mapped[str | None] = mapped_column(String(512))
    status: Mapped[str] = mapped_column(String(32), default="new")  # new | done
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    done_at: Mapped[datetime | None] = mapped_column(DateTime)
    done_by_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("admin_users.id"))


class AppSetting(Base):
    """Ключ-значення для runtime-налаштувань бота (модель GPT тощо)."""

    __tablename__ = "app_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    value: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
