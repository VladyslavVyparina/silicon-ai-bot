from pydantic import BaseModel, Field


class ConsultRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=2000)
    chat_id: str | None = None


class ConsultResponse(BaseModel):
    response: str
    source: str
    escalate_to_manager: bool = False


class PricingRequest(BaseModel):
    product_type: str
    quantity: int = Field(..., ge=1)
    size_mm: str | None = None
    urgent: bool = False


class PricingResponse(BaseModel):
    success: bool
    product_name: str | None = None
    quantity: int | None = None
    unit_price: float | None = None
    total_amount: float | None = None
    currency: str = "UAH"
    production_days: int | None = None
    message: str | None = None
    error: str | None = None


class ClientRegisterRequest(BaseModel):
    chat_id: str
    channel: str = "telegram"
    first_name: str | None = None
    last_name: str | None = None
    phone: str | None = None
    email: str | None = None
    city: str | None = None
    edrpou: str | None = None
    company_name: str | None = None


class OrderCreateRequest(BaseModel):
    chat_id: str
    product_type: str
    quantity: int = Field(..., ge=1)
    unit_price: float | None = None
    total_amount: float | None = None
    production_days: int | None = None
    comment: str | None = None
    params: dict | None = None
    client: ClientRegisterRequest | None = None


class DeliveryRequest(BaseModel):
    chat_id: str
    delivery_type: str = "warehouse"  # warehouse | postomat | address
    recipient_name: str | None = None
    recipient_last_name: str | None = None
    recipient_first_name: str | None = None
    recipient_patronymic: str | None = None
    recipient_phone: str
    city: str
    city_ref: str | None = None
    warehouse: str | None = None
    warehouse_ref: str | None = None
    street_address: str | None = None
    weight_kg: float = 1.0
    seats_amount: int = 1
    description: str | None = None


class InvoiceRequest(BaseModel):
    order_id: int
    edrpou: str | None = None
    company_name: str | None = None
    ipn: str | None = None
    company_address: str | None = None
    bank_account: str | None = None
    bank_name: str | None = None


class ChatDocument(BaseModel):
    filename: str
    path: str


class ChatMessageRequest(BaseModel):
    chat_id: str
    message: str
    channel: str = "telegram"


class ChatMessageResponse(BaseModel):
    response: str
    action: str | None = None
    data: dict | None = None
    documents: list[ChatDocument] | None = None
    # Додаткові повідомлення, які треба надіслати окремо (напр. посилання на товари —
    # кожне окремим повідомленням, щоб у Telegram відображалось прев'ю з фото).
    extra_messages: list[str] | None = None


class WebhookOneboxRequest(BaseModel):
    deal_id: str
    status: str
    secret: str | None = None
