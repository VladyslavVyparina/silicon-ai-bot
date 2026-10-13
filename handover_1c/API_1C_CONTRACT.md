# 1C HTTP contract for the Silicon bot

The base URL is `ONEC_API_URL`, with no trailing slash. Authentication is HTTP Basic (`ONEC_API_USER` / `ONEC_API_PASSWORD`).

## 1. Find a counterparty

`GET {BASE}/counterparties/search?edrpou=12345678`

or `?name=` plus the URL-encoded company name.

Response: a JSON array, which may be empty.

```json
[{"id": "1c-001", "name": "Example LLC", "edrpou": "12345678", "ipn": "", "address": ""}]
```

## 2. Create a counterparty

`POST {BASE}/counterparties`

```json
{
  "edrpou": "55667788",
  "name": "New Client LLC",
  "ipn": "9988776655",
  "address": "full legal address",
  "phone": "+380501112233",
  "email": "a@b.c"
}
```

Response:

```json
{"id": "1c-xxx", "name": "New Client LLC", "edrpou": "55667788"}
```

If the code already exists, return the existing id (HTTP 409 with that id is what the bot already understands).

## 3. Create an invoice

`POST {BASE}/invoices`

```json
{
  "counterparty_id": "1c-001",
  "amount": 1150,
  "description": "Silicon order",
  "items": []
}
```

Response:

```json
{"id": "document-guid", "number": "0000-000001", "amount": 1150}
```

The PDF the customer receives is `GET {BASE}/invoice/{id}/pdf` on the live service the bot calls. Stamp, signature, supplier bank details, and the buyer address are filled by the 1C print form, not by this file.

If you can publish only OData, send an entity map or a thin HTTP layer that implements these three methods.
