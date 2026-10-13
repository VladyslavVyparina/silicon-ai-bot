# Silicon sales bot

Telegram sales bot for silicon.com.ua. It consults on promotional products, prices the order, collects delivery, issues an invoice, and takes payment.

## Problem

Customers asked for lanyards, badges, bracelets, and similar goods in chat, often mixing size, print, and quantity in one message. Managers were quoting from a price list, then retyping company details into an invoice and a delivery note. The bot had to do that conversation without inventing prices or losing the payer’s legal address.

## What was built

- A catalog consultant that answers from product rules and a price file before it ever calls a general model.
- A quote calculator for sized and unsized goods, including multi-line orders and corrections (“20 mm, not 15”).
- Checkout that asks for delivery (Nova Poshta), then payment: LiqPay card link, bank invoice, or cash in the office.
- Invoice collection: company name, EDRPOU, VAT number only when the payer has one, and the full legal address. A repeat order asks whether to reuse the saved payer.
- Invoice PDF for the customer and a document in 1C (counterparty by EDRPOU, then invoice). The journal number comes back from 1C.
- CRM deal creation, a “completed order” hook that can create a shipment note, and SendPulse notices for payment and tracking.

## How it works

`POST /api/chat/message` is the main entry. The conversation state machine handles quoting, contacts, delivery, and payment. Pricing is data, not a prompt. When the customer asks for an invoice, the bot confirms the requisites in chat and only then builds the file. If 1C accepts the document, the bot sends the PDF 1C returns and keeps that number. Card payment uses a short `/pay/{id}` link so the chat does not expose the server address.

Main routes:

| Method | Path | Role |
| --- | --- | --- |
| POST | `/api/chat/message` | Chat turn |
| POST | `/api/pricing/calculate` | Quote |
| POST | `/api/orders/invoice` | Invoice from requisites |
| POST | `/api/orders/{id}/payment` | LiqPay link |
| POST | `/api/orders/{id}/ttn` | Shipment note |
| POST | `/api/webhooks/liqpay` | Payment webhook |
| POST | `/api/webhooks/onebox/order-completed` | CRM completed status |

## Stack

Python, FastAPI, Telegram, SQLite, 1C HTTP service, LiqPay, Nova Poshta, Onebox CRM, SendPulse.

## Run locally

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --reload --port 8000
pytest -v
```

Set `MOCK_EXTERNAL_APIS=false` only when the 1C, payment, and delivery keys are yours.

## What this public copy leaves out

`.env`, customer databases, generated invoices, call recordings, and the 1C access note. Those stay in the private working copy.
